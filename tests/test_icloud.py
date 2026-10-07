import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rules_engine import Rule, date_bounds
from services import account_service, storage_service
from services.icloud_service import ICloud, decode_folder, encode_folder, message_key, parse_folder

SETTINGS = {"batch_size": 50, "batch_pause": 0, "request_delay": 0, "retry_attempts": 2}
RAW = (b'From: Mango <news@email.mango>\r\nSubject: Our collection\r\n'
       b'Content-Type: text/html; charset=utf-8\r\n\r\n<p>Our BEST-SELLERS</p>')


class FakeIMAP:
    def __init__(self, capabilities=b'IMAP4rev1 MOVE UIDPLUS'):
        self.capabilities = capabilities
        self.commands = []
        self.folder = None
        self.validity = b'10'
        self.flags = b''
        self.raw = RAW
        self.logged_out = False

    def login(self, username, password):
        self.commands.append(('LOGIN', username))
        return 'OK', [b'authenticated']

    def capability(self): return 'OK', [self.capabilities]

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\Trash) "/" "Deleted Messages"',
                      b'(\\Archive) "/" "Archive"',
                      b'(\\Sent) "/" "Sent Messages"',
                      b'(\\Drafts) "/" "Drafts"',
                      b'(\\Junk) "/" "Junk"',
                      b'(\\Noselect) "/" "Parent"']

    def select(self, folder, readonly=False):
        self.folder = folder.strip('"')
        self.commands.append(('SELECT', folder, readonly))
        return 'OK', [b'2']

    def response(self, name): return name, [self.validity]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == 'SEARCH': return 'OK', [b'1 2']
        if command == 'FETCH':
            metadata = b'1 (UID ' + args[0].encode() + b' FLAGS (' + self.flags + b') INTERNALDATE "06-Oct-2026 12:00:00 -0700" BODY[] {100}'
            return 'OK', [(metadata, self.raw), b')']
        return 'OK', [b'done']

    def create(self, folder):
        self.commands.append(('CREATE', folder))
        return 'OK', [b'done']

    def logout(self): self.logged_out = True


class ICloudTests(unittest.TestCase):
    def setUp(self):
        self.imap = FakeIMAP()
        self.sleep = patch('services.icloud_service.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.mailbox = ICloud('test@icloud.com', 'test', 'private-password', SETTINGS, factory=lambda: self.imap)

    def test_folder_discovery_and_unicode(self):
        self.assertEqual(self.mailbox.roles['trash'], 'Deleted Messages')
        self.assertEqual(len(self.mailbox.folders), 6)
        name = 'Promotions été & fêtes'
        self.assertEqual(decode_folder(encode_folder(name)), name)
        parsed = parse_folder(b'(\\HasNoChildren) "/" "My \\"Folder\\""')
        self.assertEqual(parsed['name'], 'My "Folder"')
        with self.assertRaises(ValueError): encode_folder('Bad\r\nFolder')

    def test_preview_uses_peek_and_filters_sender_body(self):
        rule = Rule(scope='inbox', senders=['news@email.mango'], words=['BEST-SELLERS'])
        listed = self.mailbox.list_page(rule)
        key = listed['messages'][0]['id']
        self.assertIsNotNone(self.mailbox.inspect(rule, key))
        fetches = [c for c in self.imap.commands if c[0] == 'FETCH']
        self.assertEqual(len(fetches), 2)
        self.assertTrue(all('BODY.PEEK' in c[2] for c in fetches))
        self.assertFalse(any(c[0] in {'STORE', 'MOVE', 'EXPUNGE'} for c in self.imap.commands))
        self.assertTrue(all(c[2] for c in self.imap.commands if c[0] == 'SELECT'))
        self.assertIsNone(self.mailbox.inspect(Rule(senders=['other@example.com']), key))

    def test_negative_timezone_internaldate_and_period_exception(self):
        key = message_key('INBOX', '10', '1')
        start, end = date_bounds('2026-10-06', '2026-10-06')
        rule = Rule(start=start, end=end)
        self.assertIsNotNone(self.mailbox.inspect(rule, key))
        rule.exceptions = [{'senders': ['news@email.mango'], 'start': start, 'end': end}]
        self.assertIsNone(self.mailbox.inspect(rule, key))

    def test_received_scope_skips_sent_trash_drafts(self):
        result = self.mailbox.list_page(Rule(scope='received'))
        folders = result['nextPageToken']['folders']
        self.assertEqual(set(folders), {'INBOX', 'Archive', 'Junk'})

    def test_star_read_and_move_commands(self):
        rule = Rule(actions=['star', 'read', 'move'], label='News').validate()
        self.mailbox.apply(rule, message_key('INBOX', '10', '1'), 'News')
        stores = [c for c in self.imap.commands if c[0] == 'STORE']
        self.assertIn(('STORE', '1', '+FLAGS.SILENT', '(\\Flagged)'), stores)
        self.assertIn(('STORE', '1', '+FLAGS.SILENT', '(\\Seen)'), stores)
        self.assertIn(('MOVE', '1', '"News"'), self.imap.commands)

    def test_trash_moves_to_actual_folder(self):
        self.mailbox.apply(Rule(actions=['trash']), message_key('INBOX', '10', '1'))
        self.assertIn(('MOVE', '1', '"Deleted Messages"'), self.imap.commands)

    def test_uidplus_server_supports_moves_without_move_extension(self):
        server = FakeIMAP(b'IMAP4rev1 UIDPLUS')
        mailbox = ICloud('test@icloud.com', 'test', 'private-password', SETTINGS, factory=lambda: server)
        self.assertTrue({'trash', 'archive', 'move'}.issubset(mailbox.supported_actions))
        mailbox.apply(Rule(actions=['trash']), message_key('INBOX', '10', '1'))
        writes = [c for c in server.commands if c[0] in {'COPY', 'STORE', 'EXPUNGE', 'MOVE'}]
        self.assertEqual(writes, [('COPY', '1', '"Deleted Messages"'),
                                  ('STORE', '1', '+FLAGS.SILENT', '(\\Deleted)'),
                                  ('EXPUNGE', '1')])

    def test_failed_copy_never_deletes_source(self):
        server = FakeIMAP(b'IMAP4rev1 UIDPLUS')
        mailbox = ICloud('test@icloud.com', 'test', 'private-password', SETTINGS, factory=lambda: server)
        original = server.uid
        def fail_copy(command, *args):
            if command == 'COPY': return 'NO', [b'Folder full']
            return original(command, *args)
        server.uid = fail_copy
        with self.assertRaises(RuntimeError):
            mailbox.apply(Rule(actions=['trash']), message_key('INBOX', '10', '1'))
        self.assertFalse(any(c[0] in {'STORE', 'EXPUNGE'} for c in server.commands))

    def test_icloud_action_names_and_folder_picker(self):
        import main
        self.assertEqual(main.action_title('star', self.mailbox), 'Flag')
        self.assertEqual(main.action_title('trash', self.mailbox), 'Delete (move to Trash)')
        with patch.object(main, 'choose', return_value='3'):
            self.assertEqual(main.select_folder(self.mailbox), 'Archive')
        with patch.object(main, 'choose', return_value='0'), patch.object(main, 'ask', return_value='Shopping'):
            self.assertEqual(main.select_folder(self.mailbox), 'Shopping')

    def test_permanent_delete_expunges_only_target_uid(self):
        self.mailbox.apply(Rule(scope='trash', actions=['permanent_delete']), message_key('Deleted Messages', '10', '1'))
        self.assertIn(('EXPUNGE', '1'), self.imap.commands)
        self.assertFalse(any(c[0] == 'EXPUNGE' and len(c) != 2 for c in self.imap.commands))
        with self.assertRaises(ValueError):
            self.mailbox.apply(Rule(scope='trash', actions=['permanent_delete']), message_key('INBOX', '10', '1'))

    def test_uidvalidity_change_stops_resume(self):
        self.imap.validity = b'99'
        with self.assertRaisesRegex(RuntimeError, 'IDs changed'):
            self.mailbox.inspect(Rule(), message_key('INBOX', '10', '1'))
        self.assertFalse(any(c[0] == 'FETCH' for c in self.imap.commands))

    def test_unsupported_actions_never_mutate(self):
        self.assertNotIn('important', self.mailbox.supported_actions)
        self.assertNotIn('label', self.mailbox.supported_actions)
        self.imap.capabilities = b'IMAP4rev1'
        limited = ICloud('test@icloud.com', 'test', 'private-password', SETTINGS, factory=lambda: self.imap)
        with self.assertRaises(ValueError):
            limited.apply(Rule(scope='trash', actions=['permanent_delete']), message_key('Deleted Messages', '10', '1'))
        self.assertFalse(any(c[0] in {'STORE', 'MOVE', 'EXPUNGE'} for c in self.imap.commands))

    def test_password_not_saved_in_account_profile(self):
        values = iter(['test@icloud.com', 'Personal'])
        ask = lambda *a: next(values)
        with tempfile.TemporaryDirectory() as folder, patch.object(storage_service, 'ROOT', Path(folder)), patch.object(account_service, 'ICloud', return_value=self.mailbox), patch.object(account_service, 'getpass', return_value='private-password'):
            account_service.connect(SETTINGS, ask, lambda *a: '2')
            profile = (Path(folder) / 'accounts.json').read_text()
            self.assertNotIn('private-password', profile)
            self.assertNotIn('password', profile)
        self.mailbox.close()
        self.assertTrue(self.imap.logged_out)

    def test_job_cannot_cross_providers(self):
        import main
        old_gmail_job = {'account': 'test@icloud.com', 'provider': 'gmail'}
        with self.assertRaisesRegex(ValueError, 'different account or provider'):
            main.run_job(self.mailbox, Rule(), Path('unused'), old_gmail_job)

    def test_reconnect_retries_read_only_operation(self):
        import imaplib
        tries = []
        def read():
            tries.append(1)
            if len(tries) == 1: raise imaplib.IMAP4.abort('connection dropped')
            return 'ok'
        self.assertEqual(self.mailbox.read(read), 'ok')
        self.assertEqual(len(tries), 2)

    def test_email_validation_reprompts_without_username_question(self):
        questions = []
        values = iter(['icloud.com', 'test@icloud.com', 'Personal'])
        def ask(question, *args):
            questions.append(question)
            return next(values)
        with tempfile.TemporaryDirectory() as folder, patch.object(storage_service, 'ROOT', Path(folder)), patch.object(account_service, 'ICloud', return_value=self.mailbox) as client, patch.object(account_service, 'getpass', return_value='private-password'):
            account_service.connect(SETTINGS, ask, lambda *a: '2')
            self.assertEqual(client.call_args.args[1], 'test@icloud.com')
            self.assertFalse(any('IMAP username' in q for q in questions))
            self.assertEqual(len(questions), 3)

    def test_provider_connection_precedes_action_menu(self):
        import main
        events = []
        def connect(*args):
            events.append('sign in')
            return self.mailbox
        def choose(*args):
            events.append('action menu')
            return '0'
        with patch.object(main.account_service, 'connect', side_effect=connect), patch.object(main, 'choose', side_effect=choose):
            main.main()
        self.assertEqual(events, ['sign in', 'action menu'])

    def test_short_username_fallback_is_automatic(self):
        import imaplib
        class ShortNameIMAP(FakeIMAP):
            def login(self, username, password):
                if '@' in username:
                    self.commands.append(('LOGIN', username))
                    raise imaplib.IMAP4.error('Invalid login')
                return super().login(username, password)
        server = ShortNameIMAP()
        mailbox = ICloud('test@icloud.com', 'test@icloud.com', 'private-password', SETTINGS, factory=lambda: server)
        self.assertEqual(mailbox.username, 'test')
        self.assertEqual([c[1] for c in server.commands if c[0] == 'LOGIN'], ['test@icloud.com', 'test'])


if __name__ == '__main__': unittest.main()
