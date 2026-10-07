"""Message actions, including Gmail labels and permanent Trash deletion."""


def destination(gmail, name):
    if getattr(gmail, "provider", "gmail") == "icloud":
        return gmail.destination(name)
    labels = gmail.call(gmail.api.users().labels().list(userId="me")).get("labels", [])
    for label in labels:
        if label["name"] == name:
            if label.get("type") == "system":
                raise ValueError("Choose a custom destination label; use the menu for Starred/Important.")
            return label["id"]
    result = gmail.call(gmail.api.users().labels().create(
        userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ))
    return result["id"]


def apply(gmail, rule, message_id, label_id=None):
    if getattr(gmail, "provider", "gmail") == "icloud":
        return gmail.apply(rule, message_id, label_id)
    messages = gmail.api.users().messages()
    if "trash" in rule.actions:
        gmail.call(messages.trash(userId="me", id=message_id))
        return
    if "permanent_delete" in rule.actions:
        gmail.call(messages.delete(userId="me", id=message_id))
        return
    additions, removals = set(), set()
    for action in rule.actions:
        if action == "star": additions.add("STARRED")
        elif action == "unstar": removals.add("STARRED")
        elif action == "important": additions.add("IMPORTANT")
        elif action == "unimportant": removals.add("IMPORTANT")
        elif action == "read": removals.add("UNREAD")
        elif action == "unread": additions.add("UNREAD")
        elif action == "archive": removals.add("INBOX")
        elif action == "label": additions.add(label_id)
        elif action == "move":
            additions.add(label_id)
            removals.add("INBOX")
            removals.add("SPAM")
    if additions & removals:
        raise ValueError("The destination label conflicts with another action.")
    gmail.call(messages.modify(userId="me", id=message_id, body={
        "addLabelIds": sorted(additions), "removeLabelIds": sorted(removals),
    }))
