"""
FAQ lookup over the business's own answers.

Semantic rather than keyword match, because a caller can't see a search box —
they'll say "how much for a cleaning" when the entry reads "What does a
hygiene appointment cost?". Keyword matching misses that; embeddings don't.

Below the confidence threshold the agent says it doesn't know and offers a
callback, rather than reading out the nearest-but-wrong answer. Same
"don't pad with near-misses" principle as the RAG project's relevance floor —
and it matters more here, because a wrong answer spoken confidently over the
phone is harder for the caller to sanity-check than text on a screen.
"""
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import config, db

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading FAQ embedding model %s", config.EMBEDDING_MODEL)
            _model = SentenceTransformer(config.EMBEDDING_MODEL)
    return _model


def embed(text: str) -> np.ndarray:
    model = _load_model()
    vec = model.encode([text], normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
    return vec[0].astype(np.float32)


def warmup() -> float:
    """Load + one encode at startup. On a phone call the first-request penalty
    isn't 'slow', it's dead air while the caller waits — so it must not be paid
    inside a live call."""
    import time

    started = time.perf_counter()
    embed("warmup")
    elapsed = time.perf_counter() - started
    logger.info("FAQ warmup complete in %.1fs", elapsed)
    return elapsed


@dataclass
class FAQMatch:
    entry_id: int
    question: str
    answer: str
    score: float


_cache_lock = threading.Lock()
_cached_ids: Optional[np.ndarray] = None
_cached_matrix: Optional[np.ndarray] = None
_cached_meta: Optional[list[dict]] = None


def invalidate_cache() -> None:
    global _cached_ids, _cached_matrix, _cached_meta
    with _cache_lock:
        _cached_ids = None
        _cached_matrix = None
        _cached_meta = None


def add_entry(question: str, answer: str) -> int:
    vec = embed(f"{question} {answer}")
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO faq_entries (question, answer, embedding, embedding_dim) VALUES (?, ?, ?, ?)",
            (question, answer, db.pack_embedding(vec), len(vec)),
        )
        entry_id = cur.lastrowid
    # Must invalidate or the new entry is invisible until restart — the exact
    # stale-cache bug found in the helpdesk project's audit.
    invalidate_cache()
    return entry_id


def load_from_directory(directory) -> int:
    """Loads `Q: ... / A: ...` pairs from .txt/.md files."""
    directory = Path(directory)
    count = 0
    for path in sorted(directory.glob("*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        for block in blocks:
            q, a = None, None
            for line in block.split("\n"):
                line = line.strip()
                if line.lower().startswith("q:"):
                    q = line[2:].strip()
                elif line.lower().startswith("a:"):
                    a = line[2:].strip()
                elif a is not None:
                    a += " " + line
            if q and a:
                add_entry(q, a)
                count += 1
    return count


def _load_snapshot():
    """Returns a consistent snapshot so a concurrent invalidate can't null the
    cache out from under a live lookup mid-call."""
    global _cached_ids, _cached_matrix, _cached_meta
    with _cache_lock:
        if _cached_matrix is None:
            with db.session() as conn:
                rows = conn.execute("SELECT * FROM faq_entries").fetchall()
            ids, vecs, meta = [], [], []
            for r in rows:
                if r["embedding"] is None:
                    continue
                ids.append(r["id"])
                vecs.append(db.unpack_embedding(r["embedding"], r["embedding_dim"]))
                meta.append({"question": r["question"], "answer": r["answer"]})
            _cached_ids = np.array(ids, dtype=np.int64)
            _cached_matrix = np.array(vecs, dtype=np.float32) if vecs else np.zeros((0, 0), dtype=np.float32)
            _cached_meta = meta
        return _cached_ids, _cached_matrix, _cached_meta


def search(query: str, top_k: int = 3) -> list[FAQMatch]:
    ids, matrix, meta = _load_snapshot()
    if matrix.size == 0:
        return []
    qvec = embed(query)
    scores = matrix @ qvec
    order = np.argsort(-scores)[:top_k]
    return [
        FAQMatch(
            entry_id=int(ids[i]),
            question=meta[i]["question"],
            answer=meta[i]["answer"],
            score=float(scores[i]),
        )
        for i in order
    ]


def best_answer(query: str) -> Optional[FAQMatch]:
    """Returns a match ONLY above the confidence threshold.

    Returning None is a feature: it routes the caller to a callback with their
    actual question recorded, instead of reading out a plausible-sounding wrong
    answer they have no way to check."""
    matches = search(query, top_k=1)
    if not matches:
        return None
    if matches[0].score < config.FAQ_MATCH_THRESHOLD:
        return None
    return matches[0]


def entry_count() -> int:
    _, matrix, _ = _load_snapshot()
    return int(matrix.shape[0]) if matrix.size else 0
