"""
Shared test harness. Import this FIRST in every test file, before any `app.*`
import that touches the database.

Carries forward the lesson from the helpdesk project, where the test suite
called DB_PATH.unlink() and deleted the operator's real database: tests point
at a per-process temp file and can never touch real data.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="reception_test_"))
_REAL_DB_PATH = config.DB_PATH
config.DB_PATH = _TMP_DIR / "test.sqlite3"

# Tests must never place real calls or hit external services.
config.WARMUP_ON_STARTUP = False
config.HUMAN_TRANSFER_NUMBER = "+15550000000"
config.EMERGENCY_ONCALL_NUMBER = "+15550000911"


def real_db_path() -> Path:
    return _REAL_DB_PATH


def quiet_logs(level: str = "ERROR") -> None:
    import logging

    logging.getLogger("app").setLevel(getattr(logging, level, logging.ERROR))
    logging.getLogger("httpx").setLevel(logging.WARNING)


def reset_db() -> None:
    from app import db

    if config.DB_PATH != _REAL_DB_PATH and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(config.DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    db.init_db()


def cleanup() -> None:
    import shutil

    shutil.rmtree(_TMP_DIR, ignore_errors=True)
