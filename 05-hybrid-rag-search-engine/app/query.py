"""Top-level orchestration: retrieve -> rerank -> apply relevance floor -> generate."""
import logging
import time
from dataclasses import asdict
from typing import Iterable, Optional

from . import config
from .cache import get_cache, make_cache_key
from .generate import GenerationResult, generate_answer
from .rerank import rerank
from .retrieval import RetrievedChunk, hybrid_search

logger = logging.getLogger(__name__)


def answer_question(
    query: str,
    user_groups: Optional[Iterable[str]] = None,
) -> dict:
    started = time.perf_counter()

    # Cache key includes the caller's groups — see app/cache.py. Keying on
    # the question alone would serve a privileged caller's answer to an
    # unprivileged one.
    cache_key = None
    if config.QUERY_CACHE_ENABLED:
        cache_key = make_cache_key(query, user_groups)
        cached = get_cache().get(cache_key)
        if cached is not None:
            result = dict(cached)
            result["cached"] = True
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

    candidates = hybrid_search(query, user_groups=user_groups)
    ranked = rerank(query, candidates)

    # Relevance floor (architecture doc §2: below a similarity threshold,
    # retrieve nothing rather than pad context with near-miss chunks).
    #
    # NOTE: an earlier version of this used an absolute cutoff on the
    # cross-encoder score (rerank_score >= 0). That was wrong — the
    # ms-marco-MiniLM cross-encoder's logit scale is not centered at 0 for
    # relevant results (a correct top match regularly scores around -5),
    # so an absolute floor silently discarded correct answers. Fixed to a
    # *relative* gap from the top score instead: keep candidates within
    # RELEVANCE_SCORE_GAP of the best one, since hybrid_search + ACL
    # filtering has already established these are plausible/permitted
    # candidates — reranking's job here is to drop the tail, not to
    # second-guess an untuned absolute threshold. If you have a labeled
    # eval set, tune RELEVANCE_SCORE_GAP against it rather than trusting
    # this default.
    if ranked and ranked[0].rerank_score is not None:
        top_score = ranked[0].rerank_score
        if top_score < config.ABSOLUTE_RELEVANCE_FLOOR:
            # Even the best candidate isn't a real match (e.g. genuinely no
            # answer in the accessible corpus) — treat the whole set as
            # empty instead of padding context with uniformly-bad chunks.
            kept = []
        else:
            kept = [
                c for c in ranked
                if c.rerank_score is not None and (top_score - c.rerank_score) <= config.RELEVANCE_SCORE_GAP
            ]
    else:
        kept = [c for c in ranked if c.fused_score >= config.RETRIEVAL_MIN_SCORE / 10]

    result = generate_answer(query, kept)

    payload = {
        "query": query,
        "answer": result.answer,
        "insufficient_evidence": result.insufficient_evidence,
        "provider": result.provider,
        "citations": [
            {
                "chunk_id": c.chunk_id,
                "document_title": c.document_title,
                "source_path": c.source_path,
                "section": c.section,
                "verified": c.verified,
                "overlap_score": round(c.overlap_score, 3),
            }
            for c in result.citations
        ],
        "retrieved_chunks": [
            {
                "chunk_id": c.chunk_id,
                "document_title": c.document_title,
                "section": c.section,
                "raw_text": c.raw_text,
                "vector_score": round(c.vector_score, 4),
                "keyword_score": round(c.keyword_score, 4),
                "fused_score": round(c.fused_score, 4),
                "rerank_score": round(c.rerank_score, 4) if c.rerank_score is not None else None,
            }
            for c in kept
        ],
    }

    if cache_key is not None:
        get_cache().set(cache_key, payload)

    payload = dict(payload)
    payload["cached"] = False
    payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return payload
