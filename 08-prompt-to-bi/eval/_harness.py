"""
Shared test harness. Import FIRST, before any `app.*` module that touches a
database.

Same discipline as the other portfolio projects: tests point at temp files and
can never touch the real warehouse or the pinned-report store. In the helpdesk
project a test suite once deleted the operator's live database, which is the
reason this file exists everywhere now.
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

_TMP_DIR = Path(tempfile.mkdtemp(prefix="bi_test_"))
_REAL_WAREHOUSE = config.WAREHOUSE_PATH
_REAL_APP_DB = config.APP_DB_PATH

config.WAREHOUSE_PATH = _TMP_DIR / "warehouse.sqlite3"
config.APP_DB_PATH = _TMP_DIR / "app.sqlite3"
config.LLM_PROVIDER = "none"
config.CACHE_ENABLED = False  # caching would mask query changes between assertions

_seeded = False


def real_warehouse_path() -> Path:
    return _REAL_WAREHOUSE


def quiet_logs(level: str = "ERROR") -> None:
    import logging

    logging.getLogger("app").setLevel(getattr(logging, level, logging.ERROR))


def ensure_warehouse() -> None:
    """Seeds the temp warehouse once per process. Deterministic seed, so golden
    expected values are stable across runs."""
    global _seeded
    if _seeded and config.WAREHOUSE_PATH.exists():
        return
    from app import warehouse

    warehouse.seed()
    _seeded = True


def reset_app_db() -> None:
    from app import reports

    if config.APP_DB_PATH != _REAL_APP_DB and config.APP_DB_PATH.exists():
        config.APP_DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(config.APP_DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    reports.init_db()


def cleanup() -> None:
    import shutil

    shutil.rmtree(_TMP_DIR, ignore_errors=True)
