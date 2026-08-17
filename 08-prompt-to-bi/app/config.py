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


WAREHOUSE_PATH = ROOT_DIR / os.getenv("WAREHOUSE_PATH", "data/warehouse.sqlite3")
APP_DB_PATH = ROOT_DIR / os.getenv("APP_DB_PATH", "data/app.sqlite3")
SEMANTIC_MODEL_PATH = ROOT_DIR / os.getenv("SEMANTIC_MODEL_PATH", "model/semantic_model.yaml")

# --- Selector (question -> Selection) ---
# none  = rule-based matcher over metric/dimension synonyms. No key, no cost,
#         fully legible: you can read why a term mapped to a metric.
# mock  = deterministic fake for tests.
# groq | gemini = LLM selection (still constrained to the defined vocabulary —
#         the model picks names, it never writes SQL).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").strip().lower()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# --- Guardrails (architecture doc §6.2) ---
# These are enforced at the DATABASE layer, not by asking a model to behave.
# A prompt saying "only read data" is a suggestion; a read-only connection is
# a boundary.
MAX_RESULT_ROWS = _get_int("MAX_RESULT_ROWS", 1000)
# Rows the query planner may scan before we refuse to run it. Stands in for the
# bytes-scanned ceiling you'd set on BigQuery/Snowflake, where a single bad
# generated query can cost real money.
MAX_SCAN_ROWS = _get_int("MAX_SCAN_ROWS", 5_000_000)
QUERY_TIMEOUT_SECONDS = _get_int("QUERY_TIMEOUT_SECONDS", 15)

# --- Result cache ---
# Interactive questions only. Scheduled reports deliberately BYPASS this (see
# reports.run) — serving a scheduled report from cache means Monday's report can
# silently be Friday's numbers.
CACHE_ENABLED = _get_bool("CACHE_ENABLED", True)
CACHE_TTL_SECONDS = _get_int("CACHE_TTL_SECONDS", 300)
# Hard entry cap with LRU eviction. Cached payloads contain full ROW SETS, so a
# TTL alone is not enough: a busy day of distinct questions grew the cache
# without bound and never released it until restart.
CACHE_MAX_ENTRIES = _get_int("CACHE_MAX_ENTRIES", 200)

# --- Defaults ---
DEFAULT_DATE_RANGE = os.getenv("DEFAULT_DATE_RANGE", "last_30_days")
DEFAULT_TIME_GRAIN = os.getenv("DEFAULT_TIME_GRAIN", "day")

# --- Sample data generation ---
SAMPLE_SEED = _get_int("SAMPLE_SEED", 20260817)
SAMPLE_DAYS = _get_int("SAMPLE_DAYS", 400)
SAMPLE_ORDERS = _get_int("SAMPLE_ORDERS", 6000)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
