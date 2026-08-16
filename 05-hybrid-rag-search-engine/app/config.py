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


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


DB_PATH = ROOT_DIR / os.getenv("DB_PATH", "data/db/rag.sqlite3")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

RERANK_MODEL = os.getenv("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
RERANK_ENABLED = _get_bool("RERANK_ENABLED", True)

CHUNK_TARGET_TOKENS = _get_int("CHUNK_TARGET_TOKENS", 450)
CHUNK_OVERLAP_TOKENS = _get_int("CHUNK_OVERLAP_TOKENS", 60)

RETRIEVAL_TOP_K_VECTOR = _get_int("RETRIEVAL_TOP_K_VECTOR", 20)
RETRIEVAL_TOP_K_KEYWORD = _get_int("RETRIEVAL_TOP_K_KEYWORD", 20)
RETRIEVAL_TOP_K_FUSED = _get_int("RETRIEVAL_TOP_K_FUSED", 8)
RETRIEVAL_MIN_SCORE = _get_float("RETRIEVAL_MIN_SCORE", 0.15)

# Relative-gap relevance floor applied to cross-encoder rerank scores (see
# app/query.py). Candidates scoring more than this far below the top result
# are dropped. NOT a substitute for an absolute threshold on its own — see
# ABSOLUTE_RELEVANCE_FLOOR below. Untuned default; tune against a labeled
# eval set if possible.
RELEVANCE_SCORE_GAP = _get_float("RELEVANCE_SCORE_GAP", 6.0)

# Absolute sanity floor on the TOP rerank score only (not applied per-chunk).
# Needed because the relative gap alone can't catch the case where every
# retrieved candidate is uniformly irrelevant (e.g. the corpus genuinely has
# no answer) — they're all close in score, so the gap check lets them through.
# If even the best candidate scores below this, the result set is treated as
# empty ("not found") rather than padded with near-miss chunks.
#
# CALIBRATED, NOT GUESSED — but fragile. Run eval/calibrate_threshold.py to
# regenerate. Against the 31-case eval set, the two populations separate as:
#     lowest  "should answer" : -2.12  (harassment-policy)
#     highest "should refuse" : -2.55  (sabbatical leave, absent from corpus)
# This value is the midpoint of that gap, giving 31/31 on the eval set.
#
# READ THIS BEFORE RELYING ON IT: the separating margin is only 0.43 points.
# That is a knife-edge, fit to 4 documents and 31 questions. It will NOT
# transfer to another corpus, embedding model, reranker, or chunking config —
# re-run the calibration after changing any of those. A previous value of
# -8.0, picked from 4 examples, silently passed a small eval set and failed
# two cases as soon as the set grew.
#
# If the distributions ever overlap (no threshold separates them), the answer
# is a better relevance signal — a stronger reranker, or an explicit
# "does this passage answer the question?" check — not a finer cutoff.
ABSOLUTE_RELEVANCE_FLOOR = _get_float("ABSOLUTE_RELEVANCE_FLOOR", -2.33)

# --- Authentication ---
# Groups are resolved server-side from the API key (app/auth.py). They are
# never read from the request body — doing so was the original ACL hole.
AUTH_ENABLED = _get_bool("AUTH_ENABLED", True)
API_KEYS_PATH = ROOT_DIR / os.getenv("API_KEYS_PATH", "keys.json")
# Groups granted when AUTH_ENABLED=false. Local development only.
DEV_BYPASS_GROUPS = [
    g.strip() for g in os.getenv("DEV_BYPASS_GROUPS", "public").split(",") if g.strip()
]

# --- Vector index (scaling) ---
# "bruteforce": load all embeddings and do a full matrix multiply per query.
#   Simple, exact, no extra dependency. Fine to ~50k chunks.
# "hnsw": approximate nearest neighbour index (hnswlib), persisted to disk.
#   Sub-linear query time; needed past ~50k chunks. See app/vector_index.py.
VECTOR_INDEX_BACKEND = os.getenv("VECTOR_INDEX_BACKEND", "bruteforce").strip().lower()
VECTOR_INDEX_PATH = ROOT_DIR / os.getenv("VECTOR_INDEX_PATH", "data/db/hnsw_index.bin")
HNSW_M = _get_int("HNSW_M", 16)
HNSW_EF_CONSTRUCTION = _get_int("HNSW_EF_CONSTRUCTION", 200)
HNSW_EF_SEARCH = _get_int("HNSW_EF_SEARCH", 64)
# Over-fetch factor for ANN search before ACL filtering. ANN returns global
# nearest neighbours; if the caller can only see a small slice of the corpus,
# a naive top-k can come back entirely filtered-out and empty. See
# app/vector_index.py for why this exists.
ANN_ACL_OVERFETCH = _get_int("ANN_ACL_OVERFETCH", 10)

# --- Caching ---
QUERY_CACHE_ENABLED = _get_bool("QUERY_CACHE_ENABLED", True)
QUERY_CACHE_SIZE = _get_int("QUERY_CACHE_SIZE", 512)
QUERY_CACHE_TTL_SECONDS = _get_int("QUERY_CACHE_TTL_SECONDS", 300)

# --- Rate limiting ---
RATE_LIMIT_ENABLED = _get_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_REQUESTS = _get_int("RATE_LIMIT_REQUESTS", 60)
RATE_LIMIT_WINDOW_SECONDS = _get_int("RATE_LIMIT_WINDOW_SECONDS", 60)

# --- Ingestion ---
# Bump this whenever parsing, chunking, or the embedding model changes.
# It is mixed into each document's content hash, so a pipeline change
# invalidates previously-ingested documents and forces a rebuild.
#
# Without it, `ingest` reports "unchanged" and silently keeps stale chunks
# built by the OLD logic — the index quietly disagrees with the code. This
# was hit for real: improving PDF chunking produced no effect until the
# hash accounted for the pipeline itself.
#   v1: markdown-only heading detection
#   v2: structural heading detection for PDFs, bullet re-attachment
PIPELINE_VERSION = os.getenv("PIPELINE_VERSION", "v2")

EMBED_BATCH_SIZE = _get_int("EMBED_BATCH_SIZE", 64)
MAX_UPLOAD_MB = _get_int("MAX_UPLOAD_MB", 50)

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_JSON = _get_bool("LOG_JSON", False)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").strip().lower()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
