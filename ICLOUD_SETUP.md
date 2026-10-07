# Connect iCloud Mail

1. Enable iCloud Mail for your Apple Account if you do not already have a
   working `@icloud.com`, `@me.com`, or `@mac.com` mailbox.
2. Your Apple Account needs two-factor authentication to generate an
   app-specific password.
3. Open [account.apple.com](https://account.apple.com/), sign in, and go to
   **Sign-In and Security → App-Specific Passwords**.
4. Generate a password named `Email Automation`.
5. Start the application:

   ```bash
   cd /Users/soumiamichel/automation_sandbox
   source .venv/bin/activate
   python main.py
   ```

6. The first menu asks for a provider. Choose **2 — iCloud Mail**.
7. Enter your complete primary iCloud Mail address, such as `name@icloud.com`,
   and an account nickname. There is no username question: the application uses
   the email address and tries Apple's short-username format automatically if needed.
8. Paste the **app-specific password**, not your regular Apple Account password,
   into the hidden terminal prompt. No characters appearing while you type is
   normal.
9. After connection, the action menu appears. Use the same rule, filter,
   exception, preview and resume menus. Choose **6** there to switch accounts.

Account names, email addresses and IMAP usernames are saved locally in
`accounts.json`. Passwords are kept in memory for the session and requested again
on the next connection. You can save several iCloud accounts and select them
from the account menu. Saved jobs are restricted to their account and provider.
Gmail uses its existing OAuth account; this update does not add multiple Gmail
OAuth accounts.

## Supported actions

- Read/unread.
- Flag / remove flag.
- Move to Trash, archive, and move to another folder when the server advertises
  MOVE or UIDPLUS and the appropriate destination folder is found. UIDPLUS
  servers use COPY followed by removal of only the selected source message.
- Empty Trash, preserving your filters and exceptions, when the server advertises
  UIDPLUS for message-specific permanent deletion.

iCloud uses folders rather than Gmail labels. Gmail's Add label and Important
actions are not offered for iCloud. A custom destination folder is created only
when applying an approved Move operation. Existing folders are listed after
connection and offered in the destination picker. You can also enter a new
folder name. If Trash or Archive cannot be identified, its action is unavailable.

Preview reads use BODY.PEEK in read-only folders and do not mark messages as read.
Permanent deletion uses UID EXPUNGE for specific approved message IDs; it never
issues a global EXPUNGE that could delete an excluded message. Saved IDs include
the folder's UIDVALIDITY so changes to the folder stop stale jobs safely.

IMAP read connection interruptions are retried. Write operations are not blindly
repeated after a connection failure: review the mailbox, then resume the job.
An interrupted COPY-based move can leave a destination copy and the original;
check both folders before resuming to avoid duplicate copies.
Multiple moves/archive destinations in a single iCloud rule are not supported.
This release does not add Outlook or arbitrary/custom-domain providers.

No live iCloud connection was verified during development; your first login and
preview verify your account and server capabilities.

Apple's official references:

- [iCloud Mail server settings](https://support.apple.com/en-us/102525): the app
  connects to `imap.mail.me.com` on port `993` with TLS.
- [Generate an app-specific password](https://support.apple.com/en-us/102654).
