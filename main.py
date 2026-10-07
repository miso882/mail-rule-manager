"""Interactive entry point. Run: python main.py"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rules_engine import ACTIONS, Rule, date_bounds
from services import account_service, job_service, storage_service as storage

DEFAULT_SETTINGS = {"timezone": "Europe/Paris", "batch_size": 50,
                    "batch_pause": 5, "request_delay": 0.35, "retry_attempts": 10}


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    value = input(prompt + suffix + ": ").strip()
    return value or default


def choose(prompt, choices, default="1"):
    while True:
        print("\n" + prompt)
        for key, label in choices.items():
            print(f"  {key}. {label}")
        answer = ask("Choose", default)
        if answer in choices:
            return answer
        print("Choose one of the listed numbers.")


def items(prompt, defaults=None):
    value = ask(prompt + " (comma separated; '-' means none)", ", ".join(defaults or []))
    return [] if value == "-" else [v.strip() for v in value.split(",") if v.strip()]


def period(settings):
    zone = ZoneInfo(settings["timezone"])
    today = datetime.now(zone).date()
    while True:
        option = choose("Period (email date; local calendar dates)", {
            "1": "All dates", "2": "Date range (YYYY-MM-DD, endpoints included)",
            "3": "Last N calendar days, including today", "4": "Older than N calendar days",
            "5": "Before a date (that date excluded)",
        })
        try:
            if option == "1": return None, None
            if option == "2":
                return date_bounds(ask("Start date (blank = no lower limit)"),
                                   ask("End date (blank = no upper limit)"), settings["timezone"])
            if option == "5":
                lower, _ = date_bounds(ask("Before date"), "", settings["timezone"])
                if lower is None: raise ValueError("Enter a date.")
                return None, lower
            days = int(ask("Number of days"))
            if days < 1: raise ValueError("Use at least one day.")
            if option == "3":
                return date_bounds((today - timedelta(days=days - 1)).isoformat(),
                                   today.isoformat(), settings["timezone"])
            cutoff, _ = date_bounds((today - timedelta(days=days)).isoformat(), "", settings["timezone"])
            return None, cutoff
        except ValueError as error:
            print(f"Invalid period: {error}")


def exceptions(settings):
    result = []
    print("\nExceptions protect emails from the selected action.")
    while ask("Add an exception? y/n", "n").casefold() == "y":
        kind = choose("Protect emails matching", {
            "1": "A sender (all dates)", "2": "A period (all senders)",
            "3": "A sender only during a period",
        })
        senders = items("Protected sender addresses") if kind in {"1", "3"} else []
        if kind in {"1", "3"} and not senders:
            print("Enter at least one sender.")
            continue
        start, end = period(settings) if kind in {"2", "3"} else (None, None)
        if kind in {"2", "3"} and start is None and end is None:
            print("An exception period must have at least one date boundary.")
            continue
        result.append({"senders": senders, "start": start, "end": end})
    return result


def action_title(action, mailbox=None):
    if getattr(mailbox, "provider", "gmail") == "icloud":
        return {"star": "Flag", "unstar": "Remove flag", "move": "Move to folder",
                "trash": "Delete (move to Trash)"}.get(action, ACTIONS[action])
    return ACTIONS[action]


def select_actions(defaults=None, mailbox=None):
    supported = getattr(mailbox, "supported_actions", set(ACTIONS))
    available = [a for a in ACTIONS if a != "permanent_delete" and a in supported]
    print("\nActions (you can combine compatible actions)")
    for i, action in enumerate(available, 1):
        print(f"  {i}. {action_title(action, mailbox)}")
    default = ",".join(str(available.index(a) + 1) for a in (defaults or ["trash"]) if a in available)
    default = default or "1"
    while True:
        try:
            indexes = [int(v.strip()) for v in ask("Action numbers, comma separated", default).split(",")]
            if not indexes or any(i < 1 or i > len(available) for i in indexes): raise ValueError()
            return list(dict.fromkeys(available[i - 1] for i in indexes))
        except ValueError:
            print("Enter valid action numbers.")


def select_folder(mailbox, default=""):
    names = [folder["name"] for folder in mailbox.folders]
    options = {str(i): name for i, name in enumerate(names, 1)}
    options["0"] = "Create a new folder when applying"
    selection = choose("Destination folder", options,
                       str(names.index(default) + 1) if default in names else "0")
    if selection != "0": return names[int(selection) - 1]
    while True:
        name = ask("New folder name", default)
        if name: return name
        print("Enter a folder name.")


def wizard(settings, preset=None, trash=False, mailbox=None):
    rule = Rule.from_dict(preset) if preset else Rule()
    rule.name = ask("Rule name", "Empty Trash" if trash else rule.name)
    if trash:
        if "permanent_delete" not in getattr(mailbox, "supported_actions", set(ACTIONS)):
            raise ValueError("This iCloud server does not support message-specific permanent deletion.")
        rule.scope, rule.actions, rule.label = "trash", ["permanent_delete"], ""
    else:
        rule.actions = select_actions(rule.actions, mailbox)
        scope = choose("Where to search", {
            "1": "Received mail, including archived/spam (exclude Sent)",
            "2": "Inbox only", "3": "Whole mailbox, including Sent (exclude Trash/drafts)",
        }, {"received": "1", "inbox": "2", "all": "3"}.get(rule.scope, "1"))
        rule.scope = {"1": "received", "2": "inbox", "3": "all"}[scope]
        if "move" in rule.actions and getattr(mailbox, "provider", "gmail") == "icloud":
            rule.label = select_folder(mailbox, rule.label)
        else:
            rule.label = ask("Destination label / folder", rule.label) if {"label", "move"}.intersection(rule.actions) else ""
    rule.senders = items("Sender addresses to include (blank = any)", rule.senders)
    rule.words = items("Words or phrases to include (blank = no word filter)", rule.words)
    if rule.words:
        rule.word_mode = {"1": "any", "2": "all"}[choose("Words must match", {"1": "Any phrase", "2": "Every phrase"}, "1" if rule.word_mode == "any" else "2")]
        rule.search_in = {"1": "both", "2": "subject", "3": "body"}[choose("Search words in", {"1": "Subject or body", "2": "Subject only", "3": "Body only"}, {"both": "1", "subject": "2", "body": "3"}[rule.search_in])]
    rule.read_status = {"1": "any", "2": "unread", "3": "read"}[choose("Read status", {"1": "Any", "2": "Unread only", "3": "Read only"})]
    if not preset or (rule.start is None and rule.end is None) or ask("Keep the saved date range? y/n", "y").casefold() != "y":
        rule.start, rule.end = period(settings)
    if not rule.exceptions or ask("Keep the saved exceptions? y/n", "y").casefold() != "y":
        rule.exceptions = exceptions(settings)
    rule.validate()
    if hasattr(mailbox, "validate_rule"):
        mailbox.validate_rule(rule)
    return rule


def describe(rule, settings, mailbox=None):
    def display(value):
        return datetime.fromtimestamp(value, ZoneInfo(settings["timezone"])).isoformat() if value is not None else "unlimited"
    print(f"\nRule: {rule.name}")
    print("Actions: " + ", ".join(action_title(a, mailbox) for a in rule.actions))
    if rule.label: print("Destination: " + rule.label)
    print("Senders: " + (", ".join(rule.senders) or "Any"))
    print(f'Words ({rule.word_mode}, {rule.search_in}): ' + (", ".join(rule.words) or "None"))
    print(f"Scope: {rule.scope}; read status: {rule.read_status}")
    print(f"Period: {display(rule.start)} inclusive → {display(rule.end)} exclusive")
    for i, exclusion in enumerate(rule.exceptions, 1):
        print(f'Exception {i}: senders={", ".join(exclusion.get("senders", [])) or "Any"}; '
              f'{display(exclusion.get("start"))} → {display(exclusion.get("end"))}')


def run_job(gmail, rule, path, state):
    if state["account"] != gmail.account() or state.get("provider", "gmail") != getattr(gmail, "provider", "gmail"):
        raise ValueError("This saved job belongs to a different account or provider.")
    if hasattr(gmail, "validate_rule"):
        gmail.validate_rule(rule)
    describe(rule, gmail.settings, gmail)
    job_service.preview(gmail, rule, path, state)
    pending = [m for m in state["matches"] if m["id"] not in set(state["handled"])]
    if not pending:
        print("No pending matches to apply.")
        return
    print(f"\nPending: {len(pending)} emails. Examples:")
    for match in pending[:10]:
        print(f'  {match["sender"]} | {match["subject"]}')
    if "permanent_delete" in rule.actions:
        print("These matching emails in Trash will be permanently deleted. This cannot be undone.")
        approved = ask("Type DELETE to apply, or press Enter to keep the preview") == "DELETE"
    else:
        approved = ask("Apply these actions to the matching emails? y/n", "n").casefold() == "y"
    if approved:
        job_service.apply(gmail, rule, path, state)
    else:
        print(f'Preview saved. Use Resume a job later. Job: {state["id"]}')


def main():
    settings = storage.read_json(storage.ROOT / "settings.json", DEFAULT_SETTINGS)
    if not 1 <= settings["batch_size"] <= 50 or settings["request_delay"] < 0.2 or settings["retry_attempts"] < 1:
        raise ValueError("Use batch_size 1–50, request_delay at least 0.2, retry_attempts at least 1.")
    print("Email automation — Gmail and iCloud")
    print("Date filters use " + settings["timezone"] + ".")
    gmail = None
    while True:
        try:
            if gmail is None:
                gmail = account_service.connect(settings, ask, choose)
                if gmail is None: return
                print(f"Connected: {gmail.provider} / {gmail.account()}")
            option = choose("What would you like to do?", {
                "1": "Create a custom rule", "2": "Use / edit a saved rule",
                "3": "Empty Trash (with optional filters and exceptions)",
                "4": "Resume a saved job", "5": "View recent action history",
                "6": "Choose / switch mail account", "0": "Exit",
            }, "0")
            if option == "0":
                if gmail: gmail.close()
                return
            if option == "5":
                path = storage.ROOT / "data" / "history.jsonl"
                lines = path.read_text(encoding="utf-8").splitlines()[-20:] if path.exists() else []
                print("\n".join(lines) or "No actions recorded yet.")
                continue
            if option == "6":
                if gmail: gmail.close()
                gmail = None
                continue
            if option == "4":
                paths = sorted((storage.ROOT / "data" / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not paths:
                    print("No saved jobs.")
                    continue
                pairs = [(p, storage.read_json(p, {})) for p in paths]
                current_account = gmail.account()
                pairs = [(p, s) for p, s in pairs if s["account"] == current_account
                         and s.get("provider", "gmail") == gmail.provider]
                if not pairs:
                    print("No saved jobs for this account and provider.")
                    continue
                paths, states = zip(*pairs)
                number = choose("Saved jobs", {str(i): f'{s["rule"]["name"]} — scanned {s["next"]}/{len(s["ids"])} — changed {s["changed"]} — {s["id"]}' for i, s in enumerate(states, 1)})
                index = int(number) - 1
                run_job(gmail, Rule.from_dict(states[index]["rule"]), paths[index], states[index])
                continue
            presets_path = storage.ROOT / "rules.json"
            presets = storage.read_json(presets_path, [])
            preset = None
            if option == "2":
                if not presets:
                    print("No saved rules yet.")
                    continue
                number = choose("Saved rules", {str(i): p["name"] for i, p in enumerate(presets, 1)})
                preset = presets[int(number) - 1]
            rule = wizard(settings, preset, trash=option == "3" or bool(preset and "permanent_delete" in preset["actions"]), mailbox=gmail)
            describe(rule, settings, gmail)
            if ask("Save this rule for future runs? y/n", "y").casefold() == "y":
                presets = [p for p in presets if p["name"] != rule.name] + [rule.to_dict()]
                storage.write_json(presets_path, presets)
            if ask("Start the preview now? y/n", "y").casefold() == "y":
                path, state = job_service.create(rule, gmail.account(), gmail.provider)
                run_job(gmail, rule, path, state)
        except KeyboardInterrupt:
            print("\nSign-in canceled." if gmail is None else "\nPaused. Saved job progress can be resumed from the menu.")
        except EOFError:
            if gmail: gmail.close()
            return
        except Exception as error:
            if gmail is None:
                print(f"\nCould not connect: {error}\nChoose a provider and try signing in again.")
            else:
                print(f"\nStopped: {error}\nExisting job progress is saved. Use Resume a saved job to continue.")


if __name__ == "__main__":
    main()
