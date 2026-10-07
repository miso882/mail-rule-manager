import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import config_service, storage_service


class PrivateStorageTests(unittest.TestCase):
    def test_private_writes_and_history_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(storage_service, "ROOT", root):
                path = root / "data" / "jobs" / "test.json"
                storage_service.write_json(path, {"account": "test@example.com"})
                self.assertEqual(json.loads(path.read_text())["account"], "test@example.com")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / "data").stat().st_mode & 0o777, 0o700)
                storage_service.log("test", "1", "done", ["read"])
                self.assertEqual((root / "data" / "history.jsonl").stat().st_mode & 0o777, 0o600)
                self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_env_paths_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            env = root / ".env"
            env.write_text('GMAIL_TOKEN_FILE="secrets/my token.json" # local\nICLOUD_PASSWORD=ignored\n')
            path = config_service.gmail_file("GMAIL_TOKEN_FILE", "token.json", root)
            self.assertEqual(path, root / "secrets" / "my token.json")
            self.assertNotIn("ICLOUD_PASSWORD", os.environ)
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            os.environ["GMAIL_TOKEN_FILE"] = "/tmp/custom-token.json"
            self.assertEqual(config_service.gmail_file("GMAIL_TOKEN_FILE", "token.json", root), Path('/tmp/custom-token.json'))

    def test_invalid_env_never_echoes_value(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text('GMAIL_TOKEN_FILE="private-value')
            with self.assertRaises(ValueError) as caught:
                config_service.load_env(root)
            self.assertNotIn("private-value", str(caught.exception))


if __name__ == '__main__': unittest.main()
