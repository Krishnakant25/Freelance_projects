"""
Cross-encoder reranking of the fused hybrid candidates. A cross-encoder scores
(query, chunk) pairs jointly and is far more accurate than cosine similarity
or BM25 alone at judging true relevance — worth the extra latency on the
small top-N list that survives fusion, not on the whole corpus.
"""
from . import config
from .retrieval import RetrievedChunk

_model = None


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(config.RERANK_MODEL)
    return _model


def rerank(query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    if not chunks or not config.RERANK_ENABLED:
        return chunks
    model = _load_model()
    # Score against `text`, not `raw_text`: `text` carries the contextual
    # header ("[Source: Doc > 01 Hero / Landing Section]"), which is often the
    # strongest relevance signal available. Scoring raw_text hid the section
    # title from the reranker — a question about the "hero section" could not
    # match on the words "Hero Section" because they only existed in the
    # header. Observed in practice: an unrelated section outranked the exact
    # one the question named.
    pairs = [(query, c.text) for c in chunks]
    scores = model.predict(pairs)
    for chunk, score in zip(chunks, scores):
        chunk.rerank_score = float(score)
    return sorted(chunks, key=lambda c: -(c.rerank_score or 0.0))
