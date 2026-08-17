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
    """Malformed values fall back to the default rather than crashing at
    import time — a typo'd env var shouldn't take the service down on boot."""
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


DB_PATH = ROOT_DIR / os.getenv("DB_PATH", "data/db/helpdesk.sqlite3")
# How long a writer waits for a lock before failing. Without this SQLite
# raises "database is locked" immediately on contention. See db.get_connection.
DB_BUSY_TIMEOUT_SECONDS = _get_float("DB_BUSY_TIMEOUT_SECONDS", 10.0)

# --- Request limits ---
# Hard caps on user-supplied text. The intake path runs an embedding model
# over the description, so an unbounded field is both a memory and a CPU
# amplification vector: a few MB of text per request is cheap to send and
# expensive to process.
MAX_DESCRIPTION_CHARS = _get_int("MAX_DESCRIPTION_CHARS", 4000)
MAX_REQUESTER_CHARS = _get_int("MAX_REQUESTER_CHARS", 120)

# --- Rate limiting ---
# In-process, per-client. Same scope limitation as the RAG project's limiter:
# with N workers the effective limit is N x this value, and it resets on
# restart. Adequate against accidental runaway clients and cheap abuse; not a
# DoS control. Enforce at the proxy/gateway for that.
RATE_LIMIT_ENABLED = _get_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_REQUESTS = _get_int("RATE_LIMIT_REQUESTS", 30)
RATE_LIMIT_WINDOW_SECONDS = _get_int("RATE_LIMIT_WINDOW_SECONDS", 60)

# --- Startup ---
# Load the embedding model at startup instead of on the first request. The
# lazy path made the first real user wait ~30s (observed in browser testing).
WARMUP_ON_STARTUP = _get_bool("WARMUP_ON_STARTUP", True)

LOG_JSON = _get_bool("LOG_JSON", False)

# --- LLM extraction (pluggable, same pattern as the RAG project) ---
# none  = a rule-based keyword extractor. No key, no cost, always available.
#         Materially worse than an LLM at understanding free text, but never
#         down and never a compliance/cost concern for a demo.
# mock  = deterministic fake used only by the test suite.
# groq | gemini = real extraction (see MANUAL_STEPS.md).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").strip().lower()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# --- KB deflection ---
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
KB_DEFLECTION_THRESHOLD = _get_float("KB_DEFLECTION_THRESHOLD", 0.55)

# --- Alerting ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
P1_ESCALATION_MINUTES = _get_int("P1_ESCALATION_MINUTES", 15)
# Retries for a failed P1 alert. A dropped page on a security incident is the
# most expensive single failure this system can have, so one attempt isn't
# enough — but this is still best-effort in-process, not a durable queue.
ALERT_MAX_ATTEMPTS = _get_int("ALERT_MAX_ATTEMPTS", 3)
ALERT_TIMEOUT_SECONDS = _get_float("ALERT_TIMEOUT_SECONDS", 8.0)
# Total attempts across ALL sweeps before an outbox entry is marked 'failed'
# and stops being retried. A failed row is kept, not deleted — it's the record
# that a page was owed and never delivered. Surfaced in /ready.
ALERT_MAX_TOTAL_ATTEMPTS = _get_int("ALERT_MAX_TOTAL_ATTEMPTS", 12)

# Minimum gap between escalation alerts FOR THE SAME TICKET. Without this,
# check_escalations() re-pages every invocation — on a 1-minute scheduler an
# unacknowledged P1 alerts every minute indefinitely, which trains people to
# mute the channel and defeats the point of a P1.
ESCALATION_COOLDOWN_MINUTES = _get_int("ESCALATION_COOLDOWN_MINUTES", 30)

# --- Background scheduler ---
# Runs escalation checks and outbox flushes in-process, so the
# "unacknowledged P1s escalate" guarantee holds without external cron.
#
# MULTI-WORKER WARNING: every worker would run its own scheduler, so N workers
# means N escalation sweeps. The per-ticket cooldown limits the damage to at
# most N pages per cooldown window rather than unbounded spam, but the correct
# setup is to run the scheduler in exactly one place: either --workers 1, or
# SCHEDULER_ENABLED=false everywhere plus an external cron hitting
# POST /admin/check-escalations.
SCHEDULER_ENABLED = _get_bool("SCHEDULER_ENABLED", True)
SCHEDULER_INTERVAL_SECONDS = _get_int("SCHEDULER_INTERVAL_SECONDS", 60)

# --- Authentication (staff/admin endpoints only) ---
# Intake endpoints (/report etc.) are anonymous BY DESIGN — see app/auth.py.
# These keys gate ticket data and admin actions.
AUTH_ENABLED = _get_bool("AUTH_ENABLED", True)
API_KEYS_PATH = ROOT_DIR / os.getenv("API_KEYS_PATH", "keys.json")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
