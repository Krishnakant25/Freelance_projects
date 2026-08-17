"""
Shared test harness setup. Import this FIRST in every test file, before any
`app.*` import that touches the database.

WHY THIS EXISTS: the test suites previously ran against the real database at
config.DB_PATH, and eval/run_eval.py called `config.DB_PATH.unlink()` — so
running the tests silently deleted whatever tickets and KB articles the
operator (or a demo) had accumulated. That is the kind of bug that is
invisible until the moment it costs you real data, and it made the demo
database keep mysteriously emptying during development.

Every suite now points DB_PATH at a per-process temp file, so tests are
isolated from each other and can never touch real data.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252 and raise on emoji/unicode in test data.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="helpdesk_test_"))
_REAL_DB_PATH = config.DB_PATH

# Redirect ALL database access to a throwaway file for the life of this process.
config.DB_PATH = _TMP_DIR / "test.sqlite3"

# Tests must never make real network calls or send real alerts.
config.SLACK_WEBHOOK_URL = ""
config.LLM_PROVIDER = "none"
# Warmup is a startup concern, irrelevant (and slow) per-test.
config.WARMUP_ON_STARTUP = False
# The background scheduler must not run during tests — it would fire escalation
# sweeps concurrently with assertions and make failures non-deterministic.
config.SCHEDULER_ENABLED = False
# Point the key store at a temp file too, so tests neither read the operator's
# real keys.json nor create one.
_REAL_KEYS_PATH = config.API_KEYS_PATH
config.API_KEYS_PATH = _TMP_DIR / "test_keys.json"


def quiet_logs(level: str = "ERROR") -> None:
    """Suppresses INFO chatter from the app and HTTP libraries so test output
    stays readable. ERROR-level messages still show, which matters because
    the alerting fallback path logs at ERROR by design."""
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(getattr(logging, level, logging.ERROR))


def test_db_path() -> Path:
    return config.DB_PATH


def real_db_path() -> Path:
    """The production/demo DB path — exposed only so a test can assert it is
    NOT being touched."""
    return _REAL_DB_PATH


def real_keys_path() -> Path:
    return _REAL_KEYS_PATH


def create_test_key(name: str, roles: list[str]) -> str:
    """Creates a key in the ISOLATED test key store and returns the raw key."""
    import json

    from app import auth

    raw = auth.generate_key()
    path = config.API_KEYS_PATH
    data = {"keys": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["keys"].append({"name": name, "key_hash": auth.hash_key(raw), "roles": roles})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    auth.reload_keystore()
    return raw


def clear_test_keys() -> None:
    from app import auth

    if config.API_KEYS_PATH != _REAL_KEYS_PATH and config.API_KEYS_PATH.exists():
        config.API_KEYS_PATH.unlink()
    auth.reload_keystore()


def reset_db() -> None:
    """Drops and recreates the temp database. Safe: this can only ever affect
    the temp file, never the real one."""
    from app import db, kb

    if config.DB_PATH != _REAL_DB_PATH and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    # WAL leaves sidecar files behind; clear them so state can't leak between suites.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(config.DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    db.init_db()
    kb.invalidate_cache()


def cleanup() -> None:
    import shutil

    shutil.rmtree(_TMP_DIR, ignore_errors=True)
