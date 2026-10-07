"""Preview jobs, resume scanning, and apply matched actions in batches."""
import time
import uuid
from googleapiclient.errors import HttpError

from rules_engine import Rule
from services import action_service, search_service, storage_service as storage


def create(rule, account, provider="gmail"):
    rule.validate()
    job_id = uuid.uuid4().hex[:12]
    state = {"id": job_id, "account": account, "provider": provider, "rule": rule.to_dict(), "ids": [],
             "page": None, "listed": False, "next": 0, "matches": [],
             "handled": [], "changed": 0, "skipped": 0}
    path = storage.ROOT / "data" / "jobs" / f"{job_id}.json"
    storage.write_json(path, state)
    return path, state


def preview(gmail, rule, path, state):
    while not state["listed"]:
        page = search_service.list_page(gmail, rule, state["page"])
        known = set(state["ids"])
        state["ids"].extend(m["id"] for m in page.get("messages", []) if m["id"] not in known)
        state["page"] = page.get("nextPageToken")
        state["listed"] = not bool(state["page"])
        storage.write_json(path, state)
    total = len(state["ids"])
    print(f'Scanning from {state["next"]}/{total}. Job: {state["id"]}', flush=True)
    while state["next"] < total:
        end = min(state["next"] + gmail.settings["batch_size"], total)
        while state["next"] < end:
            message_id = state["ids"][state["next"]]
            try:
                match = search_service.inspect(gmail, rule, message_id)
            except HttpError as error:
                if error.resp.status != 404:
                    raise
                match = None
            if match:
                state["matches"].append(match)
                print(f'MATCH: {match["sender"]} | {match["subject"]}', flush=True)
            state["next"] += 1
            storage.write_json(path, state)
        print(f'Checked {state["next"]}/{total}; matches: {len(state["matches"])}.', flush=True)
        if state["next"] < total:
            time.sleep(gmail.settings["batch_pause"])
    print(f'Preview complete: {len(state["matches"])} match(es). No mailbox changes.')


def apply(gmail, rule, path, state):
    if not state["listed"] or state["next"] != len(state["ids"]):
        raise ValueError("Finish the preview before applying this job.")
    handled = set(state["handled"])
    pending = [m for m in state["matches"] if m["id"] not in handled]
    label_id = None
    if pending and {"label", "move"}.intersection(rule.actions):
        label_id = action_service.destination(gmail, rule.label)
    for position, match in enumerate(pending, 1):
        message_id = match["id"]
        try:
            # Re-check filters: restored, relabeled, or changed emails are skipped.
            current = search_service.inspect(gmail, rule, message_id)
            if current:
                action_service.apply(gmail, rule, message_id, label_id)
                state["changed"] += 1
                result = "applied"
            else:
                state["skipped"] += 1
                result = "no longer matches"
        except HttpError as error:
            if error.resp.status != 404:
                raise
            state["skipped"] += 1
            result = "already removed"
        state["handled"].append(message_id)
        storage.write_json(path, state)
        storage.log(state["id"], message_id, result, rule.actions)
        print(f'{result}: {match["subject"]}', flush=True)
        if position % gmail.settings["batch_size"] == 0 and position < len(pending):
            time.sleep(gmail.settings["batch_pause"])
    print(f'Finished. Changed: {state["changed"]}; skipped: {state["skipped"]}.')
