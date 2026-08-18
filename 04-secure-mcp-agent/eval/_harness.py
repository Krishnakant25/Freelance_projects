"""
Shared test harness. Import FIRST, before any `app.*` module.

Redirects the workspace, audit log, and tool manifest to a per-process temp
directory — so tests can never touch real data, and (importantly for this
project) the audit-tampering tests operate on a throwaway log.
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

_TMP = Path(tempfile.mkdtemp(prefix="mcp_test_"))
_REAL_WORKSPACE = config.WORKSPACE_DIR
_REAL_AUDIT = config.AUDIT_LOG_PATH

config.WORKSPACE_DIR = _TMP / "workspace"
config.AUDIT_LOG_PATH = _TMP / "audit.jsonl"
config.TOOL_MANIFEST_PATH = _TMP / "tool_manifest.json"
config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
config.ALLOW_AUDIT_RESET = True

# A "secret" placed OUTSIDE the workspace. Several tests assert the agent
# cannot reach it — which is the whole point of capability-scoped filesystem
# roots, so it needs to genuinely exist somewhere reachable on disk.
SECRET_DIR = _TMP / "private"
SECRET_DIR.mkdir(parents=True, exist_ok=True)
SECRET_FILE = SECRET_DIR / ".env"
SECRET_FILE.write_text("AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")


def real_workspace() -> Path:
    return _REAL_WORKSPACE


def real_audit_path() -> Path:
    return _REAL_AUDIT


def tmp_root() -> Path:
    return _TMP


def quiet_logs(level: str = "CRITICAL") -> None:
    import logging

    logging.getLogger("app").setLevel(getattr(logging, level, logging.CRITICAL))
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)


def reset_workspace() -> None:
    import shutil

    if config.WORKSPACE_DIR.exists():
        shutil.rmtree(config.WORKSPACE_DIR, ignore_errors=True)
    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def reset_audit() -> None:
    from app import audit

    audit.reset_for_tests()


def write_workspace_file(name: str, content: str) -> Path:
    path = config.WORKSPACE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def cleanup() -> None:
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
