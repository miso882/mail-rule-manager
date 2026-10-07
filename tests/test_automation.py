import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from googleapiclient.errors import HttpError
from rules_engine import Rule, date_bounds, matches
from services import action_service, job_service, search_service, storage_service
from services.gmail_service import Gmail


def message(sender="shop@example.com", subject="BEST SELLER", text="", timestamp=100, labels=None):
    return {"internalDate": str(timestamp * 1000), "labelIds": labels if labels is not None else ["INBOX"],
            "payload": {"mimeType": "text/html", "headers": [{"name": "From", "value": sender},
                       {"name": "Subject", "value": subject}],
                       "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


class RuleTests(unittest.TestCase):
    def test_sender_and_word_are_both_required(self):
        rule = Rule(senders=["shop@example.com"], words=["BEST SELLER"]).validate()
        self.assertTrue(matches(rule, message(sender="Club <SHOP@example.com>")))
        self.assertFalse(matches(rule, message(sender="other@example.com")))
        self.assertFalse(matches(rule, message(subject="Ordinary email")))

    def test_words_any_all_subject_and_html(self):
        rule = Rule(words=["Attention : stock limité", "best-selling"], word_mode="all").validate()
        email = message(subject="ATTENTION\u00a0: STOCK LIMITÉ", text="<p>Our <b>best-selling</b> products</p>")
        self.assertTrue(matches(rule, email))
        rule.search_in = "subject"
        self.assertFalse(matches(rule, email))
        rule.word_mode = "any"
        self.assertTrue(matches(rule, email))
        self.assertFalse(matches(Rule(words=["BEST SELLER"]), message(subject="BEST SELLERSHIP")))

    def test_sender_during_period_exception_uses_and(self):
        rule = Rule(exceptions=[{"senders": ["friend@example.com"], "start": 100, "end": 200}]).validate()
        self.assertFalse(matches(rule, message(sender="friend@example.com", timestamp=100)))
        self.assertTrue(matches(rule, message(sender="friend@example.com", timestamp=200)))
        self.assertTrue(matches(rule, message(sender="other@example.com", timestamp=150)))

    def test_sender_only_and_period_only_exceptions(self):
        rule = Rule(exceptions=[{"senders": ["friend@example.com"]}, {"start": 100, "end": 200}]).validate()
        self.assertFalse(matches(rule, message(sender="friend@example.com", timestamp=300)))
        self.assertFalse(matches(rule, message(timestamp=150)))
        self.assertTrue(matches(rule, message(timestamp=300)))

    def test_dates_inclusive_and_dst(self):
        start, end = date_bounds("2026-03-29", "2026-03-29")
        self.assertEqual(end - start, 23 * 3600)
        rule = Rule(start=start, end=end)
        self.assertTrue(matches(rule, message(timestamp=start)))
        self.assertFalse(matches(rule, message(timestamp=end)))
        self.assertIn(f"after:{start - 1}", rule.query())

    def test_trash_deletion_and_exceptions(self):
        rule = Rule(scope="trash", actions=["permanent_delete"], exceptions=[{"senders": ["friend@example.com"]}]).validate()
        self.assertTrue(matches(rule, message(labels=["TRASH"])))
        self.assertFalse(matches(rule, message(labels=["INBOX"])))
        self.assertFalse(matches(rule, message(sender="friend@example.com", labels=["TRASH"])))
        with self.assertRaises(ValueError): Rule(actions=["permanent_delete"]).validate()
        with self.assertRaises(ValueError): Rule(actions=["star", "unstar"]).validate()

    def test_attachment_text_excluded(self):
        email = message(subject="", text="BEST SELLER")
        email["payload"]["filename"] = "document.html"
        self.assertFalse(matches(Rule(words=["BEST SELLER"]), email))


class Request:
    def __init__(self, callback): self.callback = callback
    def execute(self): return self.callback()


class FakeAPI:
    def __init__(self): self.calls = []
    def users(self): return self
    def messages(self): return self
    def modify(self, **kwargs):
        self.calls.append(("modify", kwargs))
        return Request(lambda: {})
    def trash(self, **kwargs):
        self.calls.append(("trash", kwargs))
        return Request(lambda: {})
    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return Request(lambda: {})


class ServiceTests(unittest.TestCase):
    def test_combined_star_important_read(self):
        api = FakeAPI()
        gmail = SimpleNamespace(api=api, call=lambda r: r.execute())
        action_service.apply(gmail, Rule(actions=["star", "important", "read"]), "one")
        body = api.calls[0][1]["body"]
        self.assertEqual(set(body["addLabelIds"]), {"STARRED", "IMPORTANT"})
        self.assertEqual(body["removeLabelIds"], ["UNREAD"])

    def test_move_adds_destination_removes_inbox_spam(self):
        api = FakeAPI()
        gmail = SimpleNamespace(api=api, call=lambda r: r.execute())
        action_service.apply(gmail, Rule(actions=["move"], label="News"), "one", "Label_1")
        body = api.calls[0][1]["body"]
        self.assertEqual(body["addLabelIds"], ["Label_1"])
        self.assertEqual(set(body["removeLabelIds"]), {"INBOX", "SPAM"})

    def test_rate_limit_retried_but_permissions_not_retried(self):
        settings = {"request_delay": 0, "retry_attempts": 3}
        gmail = Gmail(None, settings)
        attempts = []
        def limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}')
            return {"ok": True}
        with patch("services.gmail_service.time.sleep") as sleep:
            self.assertTrue(gmail.call(Request(limited))["ok"])
            self.assertEqual(len(attempts), 2)
            self.assertTrue(any(c.args[0] >= 2 for c in sleep.call_args_list))
        def denied():
            raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), b'{"error":{"errors":[{"reason":"insufficientPermissions"}]}}')
        with patch("services.gmail_service.time.sleep") as sleep:
            with self.assertRaises(HttpError): gmail.call(Request(denied))
            self.assertEqual(sleep.call_count, 1)

    def test_resume_multiple_pages_preview_then_apply_without_duplicate_actions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(storage_service, "ROOT", Path(folder)):
            rule = Rule(actions=["star"])
            path, state = job_service.create(rule, "test@example.com")
            gmail = SimpleNamespace(settings={"batch_size": 50, "batch_pause": 0})
            interrupted = [False]
            def inspect(_gmail, _rule, message_id):
                if message_id == "7" and not interrupted[0]:
                    interrupted[0] = True
                    raise KeyboardInterrupt()
                return {"id": message_id, "sender": "shop@example.com", "subject": "Test"} if message_id in {"2", "103"} else None
            def page(_gmail, _rule, token):
                return {"messages": [{"id": str(i)} for i in range(30, 105)]} if token else {"messages": [{"id": str(i)} for i in range(30)], "nextPageToken": "page2"}
            with patch.object(search_service, "list_page", side_effect=page), patch.object(search_service, "inspect", side_effect=inspect), patch("services.job_service.time.sleep"):
                with self.assertRaises(KeyboardInterrupt): job_service.preview(gmail, rule, path, state)
                resumed = json.loads(path.read_text())
                self.assertEqual(resumed["next"], 7)
                job_service.preview(gmail, rule, path, resumed)
                self.assertEqual(resumed["next"], 105)
                self.assertEqual(len(resumed["matches"]), 2)
                with patch.object(action_service, "apply") as apply:
                    job_service.apply(gmail, rule, path, resumed)
                    self.assertEqual(apply.call_count, 2)
                    job_service.apply(gmail, rule, path, resumed)
                    self.assertEqual(apply.call_count, 2)
            self.assertEqual(resumed["changed"], 2)


if __name__ == "__main__":
    unittest.main()
