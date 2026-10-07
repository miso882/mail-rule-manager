"""Optional local .env configuration; never stores account passwords."""
import os
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE"}


def load_env(root=None):
    path = (root or ROOT) / ".env"
    if not path.exists(): return
    path.chmod(0o600)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("export "): line = line[7:]
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in KEYS: continue
        try:
            values = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f"Invalid quoting in .env on line {number}.") from None
        if not separator or len(values) > 1:
            raise ValueError(f"Use KEY=value in .env on line {number}; quote paths with spaces.")
        if values and values[0]: os.environ.setdefault(key, values[0])


def gmail_file(key, default, root=None):
    root = root or ROOT
    load_env(root)
    path = Path(os.environ.get(key) or default).expanduser()
    return path if path.is_absolute() else root / path
