"""
Self-service KB deflection: local semantic search over a small article
corpus, used to offer a fix BEFORE a ticket is created.

Deliberately simple — brute-force cosine similarity, no hybrid/BM25, no ANN
index. At the scale a helpdesk KB actually operates (tens to a few hundred
articles), this is both correct and fast; see the RAG project's own
benchmark for why over-engineering retrieval at small scale is a mistake to
avoid, not a best practice to copy reflexively.
"""
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import config, db
from .embeddings import embed_query, embed_texts


@dataclass
class KBMatch:
    article_id: int
    title: str
    body: str
    score: float


def ingest_kb_article(title: str, body: str, category: str = "") -> int:
    vec = embed_query(f"{title}\n{body}")
    with db.session() as conn:
        return db.insert_kb_article(conn, title=title, body=body, category=category, embedding=vec)


def ingest_kb_directory(directory) -> list[int]:
    """Ingests every .md/.txt file in a directory as a KB article. Title =
    first line, body = the rest."""
    from pathlib import Path

    directory = Path(directory)
    ids = []
    for path in sorted(directory.glob("*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        lines = text.split("\n", 1)
        title = lines[0].lstrip("#").strip()
        body = lines[1].strip() if len(lines) > 1 else text
        ids.append(ingest_kb_article(title=title, body=body, category=""))
    return ids


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


def _ensure_loaded():
    global _cached_ids, _cached_matrix, _cached_meta
    with _cache_lock:
        if _cached_matrix is not None:
            return
        with db.session() as conn:
            rows = db.all_kb_articles(conn)
        if not rows:
            _cached_ids = np.array([], dtype=np.int64)
            _cached_matrix = np.zeros((0, 0), dtype=np.float32)
            _cached_meta = []
            return
        ids, vecs, meta = [], [], []
        for r in rows:
            if r["embedding"] is None:
                continue
            ids.append(r["id"])
            vecs.append(db.unpack_embedding(r["embedding"], r["embedding_dim"]))
            meta.append({"title": r["title"], "body": r["body"]})
        _cached_ids = np.array(ids, dtype=np.int64)
        _cached_matrix = np.array(vecs, dtype=np.float32) if vecs else np.zeros((0, 0), dtype=np.float32)
        _cached_meta = meta


def search(query: str, top_k: int = 3) -> list[KBMatch]:
    _ensure_loaded()
    if _cached_matrix is None or _cached_matrix.size == 0:
        return []
    query_vec = embed_query(query)
    scores = _cached_matrix @ query_vec
    order = np.argsort(-scores)[:top_k]
    return [
        KBMatch(
            article_id=int(_cached_ids[i]),
            title=_cached_meta[i]["title"],
            body=_cached_meta[i]["body"],
            score=float(scores[i]),
        )
        for i in order
    ]


def best_match(query: str) -> Optional[KBMatch]:
    """Returns the top match ONLY if it clears the deflection confidence
    threshold — below that, offering a low-confidence "maybe this helps"
    article does more harm than good (user tries a wrong fix, wastes time,
    then still needs a ticket). Same "don't pad with near-misses" principle
    as the RAG project's relevance floor."""
    matches = search(query, top_k=1)
    if not matches:
        return None
    top = matches[0]
    if top.score < config.KB_DEFLECTION_THRESHOLD:
        return None
    return top
