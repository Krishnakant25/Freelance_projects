"""
Hybrid retrieval: vector (dense) search + FTS5/BM25 (lexical) search, each
ACL-filtered inside the SQL query itself, fused with Reciprocal Rank Fusion.

Why hybrid: pure vector search misses exact-match needs (product codes,
names, specific terms); pure keyword search misses paraphrased/semantic
queries. See ../../05_Hybrid_RAG_Search_Engine.md §2 and §6.1/§6.5.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from . import config, db
from .embeddings import embed_query
from .vector_index import get_index

_FTS_SPECIAL = re.compile(r'["\*\(\)\^:]')


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    document_title: str
    source_path: str
    section: str
    raw_text: str
    text: str
    vector_score: float = 0.0
    keyword_score: float = 0.0
    fused_score: float = 0.0
    rerank_score: Optional[float] = None


def _sanitize_fts_query(query: str) -> str:
    """Build a permissive OR-of-terms FTS5 query so odd punctuation in the
    user's question doesn't throw a syntax error."""
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    if not terms:
        return '""'
    quoted = [f'"{t}"' for t in terms]
    return " OR ".join(quoted)


def _vector_search(
    query_vec: np.ndarray,
    user_groups: Optional[Iterable[str]],
    top_k: int,
) -> list[tuple[int, float]]:
    """Delegates to the configured vector backend (app/vector_index.py).

    Previously this re-read every embedding out of SQLite on every single
    query and did a full matrix multiply. That is O(corpus) disk reads per
    query — fine at 12 chunks, untenable at scale. The backend now caches
    (bruteforce) or indexes (hnsw); ACL filtering is applied inside the
    backend so it remains a pre-filter, not a post-filter, in both cases.
    """
    return get_index().search(query_vec, user_groups, top_k)


def _keyword_search(
    conn: sqlite3.Connection,
    query: str,
    user_groups: Optional[Iterable[str]],
    top_k: int,
) -> list[tuple[int, float]]:
    where, params = db.acl_where_clause(user_groups, column="chunks.acl_groups")
    fts_query = _sanitize_fts_query(query)
    rows = conn.execute(
        f"""SELECT chunks.id, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ? AND {where}
            ORDER BY score
            LIMIT ?""",
        [fts_query] + params + [top_k],
    ).fetchall()
    # bm25() in FTS5 returns *lower is better*; flip sign so higher == more relevant,
    # consistent with the vector score direction, before fusion.
    return [(r["id"], -float(r["score"])) for r in rows]


def _reciprocal_rank_fusion(
    result_lists: list[list[tuple[int, float]]], k: int = 60
) -> dict[int, float]:
    fused: dict[int, float] = {}
    for results in result_lists:
        for rank, (chunk_id, _score) in enumerate(results, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return fused


def _hydrate_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, RetrievedChunk]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"""SELECT chunks.id AS chunk_id, chunks.document_id, chunks.section,
                   chunks.raw_text, chunks.text,
                   documents.title AS document_title, documents.source_path
            FROM chunks
            JOIN documents ON documents.id = chunks.document_id
            WHERE chunks.id IN ({placeholders})""",
        chunk_ids,
    ).fetchall()
    out = {}
    for r in rows:
        out[r["chunk_id"]] = RetrievedChunk(
            chunk_id=r["chunk_id"],
            document_id=r["document_id"],
            document_title=r["document_title"],
            source_path=r["source_path"],
            section=r["section"] or "",
            raw_text=r["raw_text"],
            text=r["text"],
        )
    return out


def hybrid_search(
    query: str,
    user_groups: Optional[Iterable[str]] = None,
    top_k: int = None,
) -> list[RetrievedChunk]:
    top_k = top_k or config.RETRIEVAL_TOP_K_FUSED

    query_vec = embed_query(query)
    vector_hits = _vector_search(query_vec, user_groups, config.RETRIEVAL_TOP_K_VECTOR)

    with db.session() as conn:
        keyword_hits = _keyword_search(conn, query, user_groups, config.RETRIEVAL_TOP_K_KEYWORD)

        fused = _reciprocal_rank_fusion([vector_hits, keyword_hits])
        vector_scores = dict(vector_hits)
        keyword_scores = dict(keyword_hits)

        ranked_ids = sorted(fused.keys(), key=lambda cid: -fused[cid])
        chunks_by_id = _hydrate_chunks(conn, ranked_ids)

    results = []
    for cid in ranked_ids:
        if cid not in chunks_by_id:
            continue
        rc = chunks_by_id[cid]
        rc.vector_score = vector_scores.get(cid, 0.0)
        rc.keyword_score = keyword_scores.get(cid, 0.0)
        rc.fused_score = fused[cid]
        results.append(rc)

    return results[:top_k]
