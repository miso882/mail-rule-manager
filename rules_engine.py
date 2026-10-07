"""User-defined filters, text matching, and date handling."""
from __future__ import annotations
import base64
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ACTIONS = {
    "trash": "Move to Trash", "archive": "Archive", "star": "Star / favorite",
    "unstar": "Remove star", "important": "Mark important",
    "unimportant": "Remove important", "read": "Mark read", "unread": "Mark unread",
    "label": "Add label", "move": "Move to label / folder",
    "permanent_delete": "Permanently delete from Trash",
}


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"–": "-", "—": "-", "‑": "-"}))
    return re.sub(r"\s*:\s*", ":", re.sub(r"\s+", " ", text)).strip()


def date_bounds(start="", end="", timezone="Europe/Paris"):
    """Inclusive calendar dates, represented as [start, next-day end)."""
    zone = ZoneInfo(timezone)
    lower = int(datetime.combine(date.fromisoformat(start), time.min, zone).timestamp()) if start else None
    upper = int(datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, zone).timestamp()) if end else None
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("End date must be on or after start date.")
    return lower, upper


@dataclass
class Rule:
    name: str = "Custom rule"
    senders: list = field(default_factory=list)
    words: list = field(default_factory=list)
    word_mode: str = "any"
    search_in: str = "both"
    scope: str = "received"
    read_status: str = "any"
    start: int | None = None
    end: int | None = None
    actions: list = field(default_factory=lambda: ["trash"])
    label: str = ""
    exceptions: list = field(default_factory=list)

    def validate(self):
        self.senders = sorted(set(s.strip().casefold() for s in self.senders if s.strip()))
        self.words = list(dict.fromkeys(w.strip() for w in self.words if w.strip()))
        if any(not re.fullmatch(r'[^@\s{}"<>:]+@[^@\s{}"<>:]+', s) for s in self.senders):
            raise ValueError("Enter sender email addresses, not display names.")
        if self.word_mode not in {"any", "all"} or self.search_in not in {"subject", "body", "both"}:
            raise ValueError("Invalid word matching option.")
        if self.scope not in {"inbox", "received", "all", "trash"} or self.read_status not in {"any", "read", "unread"}:
            raise ValueError("Invalid mailbox filter.")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Invalid date range.")
        if not self.actions or any(a not in ACTIONS for a in self.actions):
            raise ValueError("Select a supported action.")
        exclusive = {"trash", "permanent_delete"}.intersection(self.actions)
        if exclusive and len(self.actions) != 1:
            raise ValueError("Trash and permanent deletion must each be used alone.")
        for a, b in [("read", "unread"), ("star", "unstar"), ("important", "unimportant")]:
            if a in self.actions and b in self.actions:
                raise ValueError(f"Choose either {a} or {b}.")
        if "permanent_delete" in self.actions and self.scope != "trash":
            raise ValueError("Permanent deletion is restricted to Trash.")
        if {"label", "move"}.intersection(self.actions) and not self.label.strip():
            raise ValueError("A destination label is required.")
        for exception in self.exceptions:
            if set(exception) - {"senders", "start", "end"}:
                raise ValueError("Unknown exception filter.")
            senders = sorted(set(s.strip().casefold() for s in exception.get("senders", []) if s.strip()))
            if any(not re.fullmatch(r'[^@\s{}"<>:]+@[^@\s{}"<>:]+', s) for s in senders):
                raise ValueError("Exception senders must be email addresses.")
            exception["senders"] = senders
            start, end = exception.get("start"), exception.get("end")
            if not senders and start is None and end is None:
                raise ValueError("An exception needs a sender or period.")
            if start is not None and end is not None and start >= end:
                raise ValueError("Invalid exception date range.")
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value).validate()

    def query(self):
        parts = {
            "inbox": ["in:inbox", "-in:trash", "-in:drafts"],
            "received": ["-in:trash", "-in:drafts", "-in:sent"],
            "all": ["-in:trash", "-in:drafts"],
            "trash": ["in:trash", "-in:drafts"],
        }[self.scope].copy()
        if self.senders:
            parts.append("{" + " ".join(f"from:{s}" for s in self.senders) + "}")
        if self.start is not None:
            parts.append(f"after:{self.start - 1}")
        if self.end is not None:
            parts.append(f"before:{self.end}")
        if self.read_status != "any":
            parts.append(f"is:{self.read_status}")
        # Words are checked locally to support HTML and normalized spacing.
        return " ".join(parts)


def headers(message):
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


class VisibleHTML(HTMLParser):
    blocks = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in self.blocks:
            self.chunks.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in self.blocks:
            self.chunks.append(" ")

    def handle_data(self, text):
        if not self.hidden:
            self.chunks.append(text)


def body_texts(part, load_attachment=None):
    h = {v["name"].lower(): v["value"] for v in part.get("headers", [])}
    if part.get("filename") or h.get("content-disposition", "").lower().startswith("attachment"):
        return
    mime, body = part.get("mimeType", ""), part.get("body", {})
    if mime in {"text/plain", "text/html"}:
        data = body.get("data")
        if not data and body.get("attachmentId") and load_attachment:
            data = load_attachment(body["attachmentId"])
        if data:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            match = re.search(r'charset\s*=\s*["\']?([^\s;"\']+)', h.get("content-type", ""), re.I)
            try:
                text = raw.decode(match.group(1) if match else "utf-8", errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            if mime == "text/html":
                parser = VisibleHTML()
                parser.feed(text)
                text = "".join(parser.chunks)
            yield text
    for child in part.get("parts", []):
        yield from body_texts(child, load_attachment)


def matches(rule, message, load_attachment=None):
    labels = set(message.get("labelIds", []))
    if "DRAFT" in labels:
        return False
    if rule.scope == "trash":
        if "TRASH" not in labels:
            return False
    elif "TRASH" in labels:
        return False
    if rule.scope == "inbox" and "INBOX" not in labels:
        return False
    if rule.scope == "received" and "SENT" in labels:
        return False
    if rule.read_status == "unread" and "UNREAD" not in labels:
        return False
    if rule.read_status == "read" and "UNREAD" in labels:
        return False
    h = headers(message)
    sender = parseaddr(h.get("from", ""))[1].casefold()
    if rule.senders and sender not in rule.senders:
        return False
    timestamp = int(message.get("internalDate", 0)) / 1000
    if rule.start is not None and timestamp < rule.start:
        return False
    if rule.end is not None and timestamp >= rule.end:
        return False
    # Within one exception, sender AND period must match. Any exception protects it.
    for exception in rule.exceptions:
        senders = exception.get("senders", [])
        start, end = exception.get("start"), exception.get("end")
        if ((not senders or sender in senders)
                and (start is None or timestamp >= start)
                and (end is None or timestamp < end)):
            return False
    if not rule.words:
        return True
    texts = []
    if rule.search_in in {"subject", "both"}:
        texts.append(str(make_header(decode_header(h.get("subject", "")))))
    if rule.search_in in {"body", "both"}:
        texts.extend(body_texts(message.get("payload", {}), load_attachment))
    normalized = [normalize(t) for t in texts]
    hits = [any(re.search(r"(?<!\w)" + re.escape(normalize(w)) + r"(?!\w)", t) for t in normalized) for w in rule.words]
    return all(hits) if rule.word_mode == "all" else any(hits)
