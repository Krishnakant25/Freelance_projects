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


DB_PATH = ROOT_DIR / os.getenv("DB_PATH", "data/db/receptionist.sqlite3")
DB_BUSY_TIMEOUT_SECONDS = _get_float("DB_BUSY_TIMEOUT_SECONDS", 10.0)

# --- Business identity / hours ---
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Northside Dental")
# NOTE: there is deliberately no timezone setting. All datetimes in this build
# are NAIVE LOCAL TIME on the host. A BUSINESS_TIMEZONE option existed here but
# was never actually read by any code path — dead config that implied a
# capability the system didn't have. Timezone handling is a real production
# requirement (a caller in another zone, or a server in UTC, would be offered
# wrong times) and is tracked in PRODUCTION_GAPS.md rather than faked here.
# 24h clock, local business time. Outside these hours the escalation ladder
# takes a callback instead of attempting a transfer to nobody (doc §6.6).
BUSINESS_HOURS_START = _get_int("BUSINESS_HOURS_START", 9)
BUSINESS_HOURS_END = _get_int("BUSINESS_HOURS_END", 17)
# Days the business is open: 0=Monday .. 6=Sunday
BUSINESS_DAYS = [
    int(d) for d in os.getenv("BUSINESS_DAYS", "0,1,2,3,4").split(",") if d.strip().isdigit()
]
HUMAN_TRANSFER_NUMBER = os.getenv("HUMAN_TRANSFER_NUMBER", "")
EMERGENCY_ONCALL_NUMBER = os.getenv("EMERGENCY_ONCALL_NUMBER", "")

# --- Legal (doc §6.7) ---
# These cannot be retrofitted onto recordings already taken, so they are
# enforced structurally in the opening turn rather than left to a prompt.
DISCLOSE_AI = _get_bool("DISCLOSE_AI", True)
DISCLOSE_RECORDING = _get_bool("DISCLOSE_RECORDING", True)
RECORDING_ENABLED = _get_bool("RECORDING_ENABLED", False)
TRANSCRIPT_RETENTION_DAYS = _get_int("TRANSCRIPT_RETENTION_DAYS", 90)

# --- Booking ---
APPOINTMENT_MINUTES = _get_int("APPOINTMENT_MINUTES", 30)
# Whether the agent may PROMISE an SMS confirmation. Defaults false because no
# SMS provider is wired in this build — and telling a caller "we'll text you"
# when nothing will is a promise the system can't keep. Enable only once a real
# provider is connected.
SMS_CONFIRMATIONS_ENABLED = _get_bool("SMS_CONFIRMATIONS_ENABLED", False)
# How long a tentative reservation is held before expiring. Long enough for a
# caller to finish giving their details, short enough that an abandoned call
# doesn't block a slot all day.
SLOT_HOLD_SECONDS = _get_int("SLOT_HOLD_SECONDS", 180)
BOOKING_LOOKAHEAD_DAYS = _get_int("BOOKING_LOOKAHEAD_DAYS", 21)

# --- Conversation safety (doc §6.6) ---
# After this many consecutive turns the agent couldn't understand, stop trying
# to be clever and fall back to capturing a callback. A graceful capture beats
# a clever agent looping.
MAX_CONSECUTIVE_CONFUSIONS = _get_int("MAX_CONSECUTIVE_CONFUSIONS", 2)
MAX_TURNS = _get_int("MAX_TURNS", 40)

# --- FAQ ---
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
FAQ_MATCH_THRESHOLD = _get_float("FAQ_MATCH_THRESHOLD", 0.50)
WARMUP_ON_STARTUP = _get_bool("WARMUP_ON_STARTUP", True)

# --- Latency budget (doc §6.1) ---
# Not a limiter — a measurement target. The doc's point is that <800ms round
# trip is an architecture decision, not something you tune into existence, so
# the agent records per-stage timings and the eval harness reports them.
TARGET_TURN_LATENCY_MS = _get_int("TARGET_TURN_LATENCY_MS", 800)

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_JSON = _get_bool("LOG_JSON", False)
