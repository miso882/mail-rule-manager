"""Account selection; iCloud passwords are never written to disk."""
from getpass import getpass
import re

from services.gmail_service import Gmail, connect_gmail
from services.icloud_service import ICloud
from services import storage_service as storage


def connect(settings, ask, choose):
    provider = choose("Choose your mail provider", {"1": "Gmail", "2": "iCloud Mail", "0": "Exit"})
    if provider == "0": return None
    if provider == "1": return Gmail(connect_gmail(), settings)
    profiles_path = storage.ROOT / "accounts.json"
    profiles = storage.read_json(profiles_path, [])
    profiles = [p for p in profiles if p.get("provider") == "icloud"]
    options = {str(i): f'{p["name"]} ({p["email"]})' for i, p in enumerate(profiles, 1)}
    options["0"] = "Add another iCloud account"
    selected = choose("iCloud account", options, "0") if profiles else "0"
    profile = profiles[int(selected) - 1].copy() if selected != "0" else {}
    if not profile:
        while True:
            email = ask("Full iCloud email address (example: name@icloud.com)").casefold()
            if re.fullmatch(r"[^@\s]+@(icloud\.com|me\.com|mac\.com)", email):
                break
            print("Enter the whole email address, including the part before @. Example: name@icloud.com.")
        profile = {"provider": "icloud", "email": email, "username": email,
                   "name": ask("Account nickname (just a label, not your login)", "My iCloud")}
    print("Use an Apple app-specific password from https://account.apple.com (Sign-In and Security).")
    password = getpass("iCloud app-specific password (hidden): ").strip()
    if not password: raise ValueError("App-specific password required.")
    mailbox = ICloud(profile["email"], profile["email"], password, settings)
    profile["username"] = mailbox.username
    profiles = [p for p in profiles if p["email"] != profile["email"]] + [profile]
    storage.write_json(profiles_path, profiles)
    print("iCloud connected. Saved account details; password is not saved.")
    print("Folders: " + ", ".join(f["name"] for f in mailbox.folders))
    if "trash" not in mailbox.roles:
        print("Trash folder not identified: Trash operations are unavailable for this connection.")
    return mailbox
