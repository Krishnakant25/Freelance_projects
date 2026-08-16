"""
Local embeddings for KB deflection search.

Deliberately NOT shared with 05-hybrid-rag-search-engine's app/embeddings.py
— this project is self-contained per the portfolio convention (see README).
The code is intentionally near-identical; that's an acceptable amount of
duplication for keeping the two projects independently deployable.
"""
import numpy as np

from . import config

_model = None


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(config.EMBEDDING_MODEL)
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    model = _load_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
    return vectors.astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]
