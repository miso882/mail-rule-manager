"""Private, atomic job checkpoints and an action history."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def private_directory(path):
    existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not change an existing arbitrary directory configured by the user.
    if not existed or any((ROOT / name) in path.parents for name in ("data", "secrets")):
        path.parent.chmod(0o700)
    if (ROOT / "data") in path.parents:
        (ROOT / "data").chmod(0o700)


def write_private_text(path, value):
    private_directory(path)
    descriptor, name = tempfile.mkstemp(prefix=".private-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_private_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def log(job_id, message_id, result, actions):
    path = ROOT / "data" / "history.jsonl"
    private_directory(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                "job": job_id, "message_id": message_id,
                                "result": result, "actions": actions}) + "\n")
