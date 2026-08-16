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
    return int(val) if val else default


DB_PATH = ROOT_DIR / os.getenv("DB_PATH", "data/db/helpdesk.sqlite3")

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
KB_DEFLECTION_THRESHOLD = float(os.getenv("KB_DEFLECTION_THRESHOLD", "0.55"))

# --- Alerting ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
P1_ESCALATION_MINUTES = _get_int("P1_ESCALATION_MINUTES", 15)

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
