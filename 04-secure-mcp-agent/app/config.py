import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        import warnings

        warnings.warn(f"{name}={val!r} is not an integer; using default {default}", stacklevel=2)
        return default


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        import warnings

        warnings.warn(f"{name}={val!r} is not a number; using default {default}", stacklevel=2)
        return default


# --- Workspace ---
# The ONLY filesystem root an agent is granted by default. Everything outside
# is unreachable through the capability system, not merely discouraged.
WORKSPACE_DIR = ROOT_DIR / os.getenv("WORKSPACE_DIR", "workspace")

# --- Audit ---
# Deliberately OUTSIDE the workspace: an agent holding fs_write on the
# workspace must not be able to reach its own audit trail. See app/audit.py.
AUDIT_LOG_PATH = ROOT_DIR / os.getenv("AUDIT_LOG_PATH", "data/audit.jsonl")
ALLOW_AUDIT_RESET = _get_bool("ALLOW_AUDIT_RESET", True)  # tests only; false in prod

# --- Approvals (doc §6.5) ---
# Approvals must be RARE enough to stay meaningful. Auto-allow reversible work,
# auto-deny the forbidden set, and prompt only for the irreversible middle.
APPROVAL_REQUIRED = _get_bool("APPROVAL_REQUIRED", True)
# Session grants avoid per-call prompting for the same repeated action.
SESSION_GRANT_TTL_SECONDS = _get_int("SESSION_GRANT_TTL_SECONDS", 900)
# When no human is attached (batch/CI), an irreversible action has nobody to
# approve it. Failing CLOSED is the only safe default.
AUTO_DENY_WHEN_UNATTENDED = _get_bool("AUTO_DENY_WHEN_UNATTENDED", True)

# --- Egress ---
# Empty by default: no network. Doc §6.1 — cutting egress is the single
# highest-value control against the lethal trifecta, and it's free.
DEFAULT_EGRESS_HOSTS = [
    h.strip() for h in os.getenv("DEFAULT_EGRESS_HOSTS", "").split(",") if h.strip()
]
EGRESS_TIMEOUT_SECONDS = _get_float("EGRESS_TIMEOUT_SECONDS", 10.0)
EGRESS_MAX_BYTES = _get_int("EGRESS_MAX_BYTES", 2_000_000)

# --- Execution sandbox ---
# NOTE: this build uses subprocess isolation with resource limits, which is NOT
# a kernel boundary. Doc §6.2 is explicit that untrusted code execution needs
# gVisor/Firecracker/E2B. EXEC_ENABLED defaults FALSE so the weakest control is
# opt-in rather than silently on. See PRODUCTION_GAPS.md.
EXEC_ENABLED = _get_bool("EXEC_ENABLED", False)
EXEC_TIMEOUT_SECONDS = _get_int("EXEC_TIMEOUT_SECONDS", 10)
EXEC_MAX_OUTPUT_BYTES = _get_int("EXEC_MAX_OUTPUT_BYTES", 100_000)

# --- Tool registry ---
# Snapshot tool descriptions at approval and alert on change (doc §6.4 rug-pull).
TOOL_MANIFEST_PATH = ROOT_DIR / os.getenv("TOOL_MANIFEST_PATH", "data/tool_manifest.json")
BLOCK_ON_TOOL_CHANGE = _get_bool("BLOCK_ON_TOOL_CHANGE", True)

# --- Limits ---
MAX_FILE_READ_BYTES = _get_int("MAX_FILE_READ_BYTES", 1_000_000)
MAX_TOOL_CALLS_PER_TASK = _get_int("MAX_TOOL_CALLS_PER_TASK", 50)

# --- API ---
AUTH_ENABLED = _get_bool("AUTH_ENABLED", True)
API_KEYS_PATH = ROOT_DIR / os.getenv("API_KEYS_PATH", "keys.json")
RATE_LIMIT_ENABLED = _get_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_REQUESTS = _get_int("RATE_LIMIT_REQUESTS", 30)
RATE_LIMIT_WINDOW_SECONDS = _get_int("RATE_LIMIT_WINDOW_SECONDS", 60)

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_JSON = _get_bool("LOG_JSON", False)
