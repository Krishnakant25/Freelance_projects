"""
Local, free embeddings via sentence-transformers. Default model
(all-MiniLM-L6-v2) is small (~80MB) and fast on CPU — good for a demo/local
corpus. Swap EMBEDDING_MODEL to a larger model (e.g. bge-base-en-v1.5) for
better retrieval quality once accuracy, not setup speed, is the bottleneck.
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
    vectors = model.encode(
        texts,
        normalize_embeddings=True,  # so dot product == cosine similarity
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]
