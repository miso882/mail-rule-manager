"""iCloud IMAP provider. Passwords stay in memory; previews use BODY.PEEK."""
import base64
import imaplib
import json
import re
import ssl
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from rules_engine import headers, matches


def encode_folder(text):
    if any(c in text for c in "\r\n\x00"):
        raise ValueError("Invalid folder name.")
    result, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            result.append("&" + encoded + "-")
            pending.clear()
    for char in text:
        if 0x20 <= ord(char) <= 0x7e:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def decode_folder(text):
    def decode(match):
        chunk = match.group(1)
        if not chunk: return "&"
        return base64.b64decode(chunk.replace(",", "/") + "=" * (-len(chunk) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", decode, text)


def quote(value):
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Invalid IMAP value.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_folder(line):
    literal = None
    if isinstance(line, tuple):
        line, literal = line
    parsed = re.match(rb'\(([^)]*)\)\s+("(?:[^"\\]|\\.)*"|NIL)\s+(.+)', line)
    if not parsed:
        raise ValueError("Cannot parse an iCloud folder response.")
    flags = set(parsed.group(1).lower().split())
    name = literal if literal is not None else parsed.group(3)
    if name.startswith(b'"') and name.endswith(b'"'):
        name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
    wire = name.decode("ascii")
    return {"wire": wire, "name": decode_folder(wire), "flags": flags}


def message_key(folder, validity, uid):
    raw = json.dumps([folder, str(validity), str(uid)], ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode()


def split_key(key):
    folder, validity, uid = json.loads(base64.urlsafe_b64decode(key))
    if not validity.isdigit() or not uid.isdigit():
        raise ValueError("Invalid saved iCloud message ID.")
    quote(folder)
    return folder, validity, uid


def mime_payload(message):
    value = {"mimeType": message.get_content_type(), "headers": [
        {"name": k, "value": str(v)} for k, v in message.items()
    ], "filename": message.get_filename() or ""}
    if value["filename"] or message.get_content_disposition() == "attachment":
        return value
    if message.is_multipart():
        value["parts"] = [mime_payload(child) for child in message.iter_parts()]
    else:
        raw = message.get_payload(decode=True) or b""
        value["body"] = {"data": base64.urlsafe_b64encode(raw).decode()}
    return value


class ICloud:
    provider = "icloud"

    def __init__(self, email, username, password, settings, factory=None, folder_roles=None):
        self.email, self.username, self._password = email.casefold(), username, password
        self.settings = settings
        self._factory = factory or (lambda: imaplib.IMAP4_SSL(
            "imap.mail.me.com", 993, ssl_context=ssl.create_default_context(), timeout=30,
        ))
        self.client = None
        self._selected = None
        self._validity = None
        self._role_overrides = folder_roles or {}
        try:
            self._connect()
        except Exception:
            self.close()
            raise

    def _connect(self):
        self._selected = None
        self.client = self._factory()
        try:
            self._check(self.client.login(self.username, self._password))
        except imaplib.IMAP4.abort:
            raise
        except (imaplib.IMAP4.error, RuntimeError):
            # Apple permits a short username; try it automatically if full email fails.
            short = self.email.split("@", 1)[0]
            if self.username != short:
                try:
                    self._check(self.client.login(short, self._password))
                    self.username = short
                except imaplib.IMAP4.abort:
                    raise
                except (imaplib.IMAP4.error, RuntimeError):
                    self.close()
                    raise ValueError("iCloud sign-in failed. Check the full iCloud address and Apple app-specific password.") from None
            else:
                self.close()
                raise ValueError("iCloud sign-in failed. Check the full iCloud address and Apple app-specific password.") from None
        _, data = self._check(self.client.capability())
        self.capabilities = set(b" ".join(data).upper().split())
        _, lines = self._check(self.client.list())
        self.folders = [parse_folder(line) for line in lines if line]
        self.folders = [f for f in self.folders if b"\\noselect" not in f["flags"]]
        self.roles = {}
        names = {"trash": {"deleted messages", "trash"}, "archive": {"archive"},
                 "sent": {"sent messages", "sent"}, "drafts": {"drafts"}, "junk": {"junk", "spam"}}
        for role in names:
            special = ("\\" + ("drafts" if role == "drafts" else role)).encode()
            flagged = [f for f in self.folders if special in f["flags"]]
            fallback = [f for f in self.folders if f["name"].casefold() in names[role]]
            options = flagged or fallback
            if options: self.roles[role] = options[0]["wire"]
        for role, wire in self._role_overrides.items():
            if wire in {f["wire"] for f in self.folders}:
                self.roles[role] = wire
        self.supported_actions = {"star", "unstar", "read", "unread"}
        if b"MOVE" in self.capabilities or b"UIDPLUS" in self.capabilities:
            self.supported_actions.add("move")
            if "trash" in self.roles: self.supported_actions.add("trash")
            if "archive" in self.roles: self.supported_actions.add("archive")
        if b"UIDPLUS" in self.capabilities and "trash" in self.roles:
            self.supported_actions.add("permanent_delete")

    @staticmethod
    def _check(response):
        status, data = response
        if status != "OK":
            raise RuntimeError("iCloud rejected the operation: " + str(data))
        return status, data

    def close(self):
        if self.client:
            try: self.client.logout()
            except Exception: pass
        self.client = None
        self._selected = None

    def account(self):
        return self.email

    def read(self, operation):
        for attempt in range(self.settings["retry_attempts"]):
            time.sleep(self.settings["request_delay"])
            try:
                return operation()
            except (imaplib.IMAP4.abort, OSError):
                self.close()
                if attempt == self.settings["retry_attempts"] - 1: raise
                delay = min(60, 2 ** (attempt + 1))
                print(f"iCloud connection interrupted; reconnecting in {delay}s...", flush=True)
                time.sleep(delay)
                self._connect()

    def select(self, folder, write=False, expected=None):
        key = (folder, write)
        if self._selected != key:
            self._check(self.client.select(quote(folder), readonly=not write))
            status, data = self.client.response("UIDVALIDITY")
            if not data or not data[0]:
                raise RuntimeError("iCloud did not report folder UIDVALIDITY; cannot safely resume.")
            self._validity = data[0].decode().strip()
            self._selected = key
        if expected is not None and self._validity != expected:
            raise RuntimeError("iCloud folder IDs changed. Start a new preview; this saved job cannot safely continue.")
        return self._validity

    def list_page(self, rule, page=None):
        if page is None:
            folders = self.folders
            if rule.scope == "inbox":
                folders = [f for f in folders if f["wire"].casefold() == "inbox"]
            elif rule.scope == "trash":
                if "trash" not in self.roles: raise ValueError("No iCloud Trash folder identified.")
                folders = [f for f in folders if f["wire"] == self.roles["trash"]]
            else:
                excluded = {self.roles.get("trash"), self.roles.get("drafts")}
                if rule.scope == "received": excluded.add(self.roles.get("sent"))
                folders = [f for f in folders if f["wire"] not in excluded]
            page = {"folders": [f["wire"] for f in folders], "index": 0}
        folders, index = page["folders"], page["index"]
        if index >= len(folders): return {"messages": []}
        folder = folders[index]

        def search():
            validity = self.select(folder)
            criteria = ["ALL"]
            if rule.scope != "trash": criteria.extend(["NOT", "DELETED"])
            if rule.read_status != "any": criteria.append("UNSEEN" if rule.read_status == "unread" else "SEEN")
            # Wider server-side day bounds; exact timezone boundaries are checked locally.
            if rule.start is not None:
                day = datetime.fromtimestamp(rule.start, timezone.utc).date() - timedelta(days=1)
                criteria.extend(["SINCE", day.strftime("%d-%b-%Y")])
            if rule.end is not None:
                day = datetime.fromtimestamp(rule.end, timezone.utc).date() + timedelta(days=1)
                criteria.extend(["BEFORE", day.strftime("%d-%b-%Y")])
            _, data = self._check(self.client.uid("SEARCH", None, *criteria))
            return {"messages": [{"id": message_key(folder, validity, uid.decode())}
                                  for uid in (data[0] or b"").split()]}
        result = self.read(search)
        if index + 1 < len(folders):
            result["nextPageToken"] = {"folders": folders, "index": index + 1}
        return result

    def _fetch(self, key, full=False):
        folder, validity, uid = split_key(key)
        def fetch():
            self.select(folder, expected=validity)
            part = "BODY.PEEK[]" if full else "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]"
            _, data = self._check(self.client.uid("FETCH", uid, f"(UID FLAGS INTERNALDATE {part})"))
            records = [item for item in data if isinstance(item, tuple)]
            if not records: return None
            metadata, raw = records[0]
            flags = set(imaplib.ParseFlags(metadata))
            date_match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
            if not date_match: raise RuntimeError("iCloud message date missing; cannot apply date filters safely.")
            date_text = date_match.group(1).decode().replace("-", " ", 2)
            timestamp = parsedate_to_datetime(date_text).timestamp()
            labels = set()
            if folder.casefold() == "inbox": labels.add("INBOX")
            for role, label in (("trash", "TRASH"), ("drafts", "DRAFT"), ("sent", "SENT"), ("junk", "SPAM")):
                if folder == self.roles.get(role): labels.add(label)
            if b"\\Draft" in flags: labels.add("DRAFT")
            if b"\\Seen" not in flags: labels.add("UNREAD")
            if b"\\Flagged" in flags: labels.add("STARRED")
            if b"\\Deleted" in flags and folder != self.roles.get("trash"):
                return None
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            return {"internalDate": str(int(timestamp * 1000)), "labelIds": list(labels), "payload": mime_payload(parsed)}
        return self.read(fetch)

    def inspect(self, rule, key):
        message = self._fetch(key)
        if message is None or not matches(replace(rule, words=[]), message): return None
        if rule.words and rule.search_in in {"body", "both"}:
            message = self._fetch(key, full=True)
        if message is None or not matches(rule, message): return None
        h = headers(message)
        folder, _, _ = split_key(key)
        return {"id": key, "sender": h.get("from", "(unknown)"),
                "subject": h.get("subject", "(no subject)"), "folder": decode_folder(folder)}

    def destination(self, name):
        for folder in self.folders:
            if folder["name"] == name: return folder["wire"]
        wire = encode_folder(name)
        time.sleep(self.settings["request_delay"])
        self._check(self.client.create(quote(wire)))
        self.folders.append({"name": name, "wire": wire, "flags": set()})
        return wire

    def validate_rule(self, rule):
        unsupported = set(rule.actions) - self.supported_actions
        if unsupported:
            raise ValueError("Unsupported for this iCloud server: " + ", ".join(sorted(unsupported)))
        if {"archive", "move"}.issubset(rule.actions):
            raise ValueError("For iCloud, choose either Archive or Move to folder.")

    def apply(self, rule, key, destination=None):
        self.validate_rule(rule)
        folder, validity, uid = split_key(key)
        self.select(folder, write=True, expected=validity)
        for action in rule.actions:
            flags = {"read": ("+FLAGS.SILENT", "\\Seen"), "unread": ("-FLAGS.SILENT", "\\Seen"),
                     "star": ("+FLAGS.SILENT", "\\Flagged"), "unstar": ("-FLAGS.SILENT", "\\Flagged")}
            if action in flags:
                operation, flag = flags[action]
                time.sleep(self.settings["request_delay"])
                self._check(self.client.uid("STORE", uid, operation, f"({flag})"))
        move = next((a for a in rule.actions if a in {"trash", "archive", "move"}), None)
        if move:
            target = destination if move == "move" else self.roles[move]
            if target != folder:
                if b"MOVE" in self.capabilities:
                    time.sleep(self.settings["request_delay"])
                    self._check(self.client.uid("MOVE", uid, quote(target)))
                else:
                    # Standard move: copy successfully, then remove only this source UID.
                    # Never expunge an entire folder: other messages may be protected.
                    time.sleep(self.settings["request_delay"])
                    self._check(self.client.uid("COPY", uid, quote(target)))
                    time.sleep(self.settings["request_delay"])
                    self._check(self.client.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"))
                    time.sleep(self.settings["request_delay"])
                    self._check(self.client.uid("EXPUNGE", uid))
        if "permanent_delete" in rule.actions:
            if folder != self.roles.get("trash"):
                raise ValueError("Permanent deletion is allowed only in the iCloud Trash folder.")
            time.sleep(self.settings["request_delay"])
            self._check(self.client.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"))
            time.sleep(self.settings["request_delay"])
            self._check(self.client.uid("EXPUNGE", uid))
