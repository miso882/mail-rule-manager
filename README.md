# Email automation — Gmail and iCloud

A Python terminal app for managing mail with sender, text and date rules.
Choose an account, preview matching messages, then apply the selected actions.
No AI service is involved.

## First setup

Use Python 3.13 (the version used for verification). From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp settings.example.json settings.json
cp rules.example.json rules.json
cp .env.example .env
chmod 600 .env settings.json rules.json
python main.py
```

The copy commands are for a new checkout only. Keep existing settings and rules
when updating an installation.

For Gmail, enable the Gmail API in your Google Cloud project, configure the
OAuth consent screen and add your account as a test user if the app is in
Testing. Create a Desktop app OAuth client and save its downloaded JSON as
`credentials.json` in this folder. Run `chmod 600 credentials.json`. The first
Gmail sign-in opens a browser and creates a local `token.json`.

For iCloud, use your full mail address and an Apple app-specific password at
the hidden password prompt. See [ICLOUD_SETUP.md](ICLOUD_SETUP.md).

## Private files and optional configuration

`.env` is optional. It accepts `GMAIL_CREDENTIALS_FILE` and `GMAIL_TOKEN_FILE`
to choose the local Gmail file locations. For example:

```dotenv
GMAIL_CREDENTIALS_FILE=secrets/credentials.json
GMAIL_TOKEN_FILE=secrets/token.json
```

Move your existing files there before using these values. Relative paths resolve
from the project folder; absolute paths also work. Exported environment values
take priority over `.env`. Never put tokens or passwords in `.env.example`.

iCloud passwords stay in memory and are requested each time you connect.
Gmail OAuth credentials and tokens are stored locally, not encrypted; the app
limits their file access to the current user. New account profiles, rules,
checkpoints and history files are also created with owner-only access.
`.env` is made private when loaded. These permissions and Git exclusions do
not protect against software running as your user.

Git excludes `.env` files except `.env.example`, credentials, tokens,
`secrets/`, account profiles, personal rules/settings, job history, backups and
virtual environments. Only generic `.example` configuration files are shared.
Never paste a token or password into source code, screenshots or commits.

## Publish to GitHub

Create an empty repository on GitHub, then replace the URL below with yours:

```bash
git init -b main
git add .
git status --short
git diff --cached --stat
git commit -m "Add Gmail and iCloud email automation"
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

Review the staged files before committing. `.gitignore` does not remove files
already tracked or erase previous Git history. If a real token or password was
previously published, revoke it with the provider and remove it from history
before publishing again.

Run from your project folder with the virtual environment active:

```bash
python main.py
```

The menu lets you create a rule, reuse/edit a saved rule, empty Trash, resume a
job, view action history, or choose/switch mail accounts. Select Gmail or iCloud
before signing in; the action menu appears only after connection. There is no
manual iCloud username question. See [ICLOUD_SETUP.md](ICLOUD_SETUP.md) for Apple's app-specific
password setup. The existing Gmail `credentials.json` and `token.json`
stay next to `main.py` unless you configure other paths in `.env`.

## Structure

- `main.py`: terminal questions, rule summaries, preview/application choices.
- `rules_engine.py`: shared sender, word, date, read-state and exception checks.
- `services/gmail_service.py`: authentication, pacing and automatic retries.
- `services/icloud_service.py`: iCloud IMAP folders, search, flags and moves.
- `services/account_service.py`: provider and saved iCloud account selection.
- `services/search_service.py`: Gmail search and message inspection.
- `services/action_service.py`: Trash, archive, star, important, read/unread,
  label/folder and permanent-deletion operations.
- `services/job_service.py`: batches, preview, saved progress and application.
- `services/storage_service.py`: checkpoint files and history.
- `services/config_service.py`: optional local `.env` paths.
- `rules.json`: editable saved rules and the earlier examples as presets.
- `settings.json`: timezone, batch size, pauses and retry settings.
- `data/`: generated private job files and action history.
- `accounts.json`: generated iCloud account profiles; never contains passwords.
- `mail_client.py`: compatibility import for the earlier scripts.
- `legacy/`: backups of earlier Python files made during installation.

## Filters and exceptions

Multiple sender addresses mean ANY sender. Words can mean ANY phrase or ALL
phrases, searched in subject, body, or both. Sender, date, read-state and word
filters are combined with AND. Empty sender/word inputs mean unrestricted.
For a saved rule, Enter keeps the displayed sender/word values; `-` clears them.
Emails in drafts are skipped. Normal actions exclude Trash; permanent deletion
is restricted to matching emails in Trash.

Exceptions protect messages from every action, including emptying Trash:

- A sender, regardless of date.
- A period, regardless of sender.
- A sender AND a period. This protects that sender only inside that period.

If multiple exceptions are added, matching ANY exception protects the message.
For example, archive all received mail except `friend@example.com` during
2026-09-01 through 2026-09-30. Mail from other senders during September and mail
from that sender outside September can still be archived.

Dates use Europe/Paris, not Gmail's default search-date timezone. Start/end
calendar dates are inclusive. Saved relative-period choices become fixed date
boundaries; the wizard lets you choose a new period when reusing the rule.
The period refers to Gmail's message date, not the date it entered Trash.

## Actions and runs

The menu uses Star / favorite for Gmail and Flag for iCloud. iCloud uses folders,
so Add label and Important are offered only for Gmail. Add label keeps the message's current
location; Move to label also removes it from Inbox/Spam. Missing destination
labels are created only after you approve applying the matched actions.
Compatible actions can be combined, such as Star + Important + Mark read.
Trash and permanent deletion are each standalone actions.

Every run first previews the matching messages and then asks whether to apply.
Permanent deletion requires typing `DELETE` after the preview because it
cannot be undone. No rule runs automatically in the background.

Scanning and application use batches of at most 50. Calls are paced, transient
quota/server/network errors retry with increasing waits, and each processed
message saves progress. Ctrl+C pauses a job; choose Resume a saved job to
continue it. A completed job uses its original mailbox snapshot. Start a new
run using a saved rule to include newer messages.

Word matching checks decoded plain text and HTML text, including large text
body parts. It skips attachments and does not perform OCR on images or PDFs.
Full body scans of thousands of messages can take a long time. Sender/date
filters narrow the scan without relying on Gmail's word-index matching.

`data/` contains message IDs and preview sender/subject information. The
`.gitignore` excludes credentials, tokens, account profiles, backups and progress/history files.
Run one instance at a time. No tests access or modify your live mailbox.

## Verification

```bash
python -m unittest discover -s tests -v
```
