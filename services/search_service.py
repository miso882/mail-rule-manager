"""Search Gmail and retrieve message content only when needed."""
from rules_engine import headers, matches


def list_page(gmail, rule, page=None):
    if getattr(gmail, "provider", "gmail") == "icloud":
        return gmail.list_page(rule, page)
    return gmail.call(gmail.api.users().messages().list(
        userId="me", q=rule.query(), includeSpamTrash=True,
        maxResults=500, pageToken=page,
    ))


def inspect(gmail, rule, message_id):
    if getattr(gmail, "provider", "gmail") == "icloud":
        return gmail.inspect(rule, message_id)
    full = bool(rule.words) and rule.search_in in {"body", "both"}
    parameters = {"userId": "me", "id": message_id, "format": "full" if full else "metadata"}
    if not full:
        parameters["metadataHeaders"] = ["From", "Subject"]
    message = gmail.call(gmail.api.users().messages().get(**parameters))

    def attachment(attachment_id):
        return gmail.call(gmail.api.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id,
        )).get("data")

    if not matches(rule, message, attachment):
        return None
    h = headers(message)
    return {"id": message_id, "sender": h.get("from", "(unknown)"), "subject": h.get("subject", "(no subject)")}
