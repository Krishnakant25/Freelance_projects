"""
Local embeddings for KB deflection search.

Deliberately NOT shared with 05-hybrid-rag-search-engine's app/embeddings.py
— this project is self-contained per the portfolio convention (see README).
The code is intentionally near-identical; that's an acceptable amount of
duplication for keeping the two projects independently deployable.
"""
import logging
import threading
import time

import numpy as np

from . import config

logger = logging.getLogger(__name__)

_model = None
# Guards model construction. Without it, concurrent first-requests each build
# their own SentenceTransformer (several hundred MB of duplicated tensors and
# multiple simultaneous model loads) before one wins the assignment. The
# double-checked pattern below keeps the lock off the hot path once loaded.
_model_lock = threading.Lock()


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:  # re-check: another thread may have loaded it
            from sentence_transformers import SentenceTransformer

            started = time.perf_counter()
            logger.info("Loading embedding model %s ...", config.EMBEDDING_MODEL)
            _model = SentenceTransformer(config.EMBEDDING_MODEL)
            logger.info(
                "Embedding model loaded in %.1fs", time.perf_counter() - started
            )
    return _model


def warmup() -> float:
    """Loads the model and runs one throwaway encode, returning seconds taken.

    Called at API startup. Without this the first real user pays the full
    model-load cost inside their request — measured at roughly 30 seconds
    during browser testing, which reads as a hung page, not a slow one.
    Encoding a dummy string matters too: the first forward pass does
    additional lazy initialisation beyond just loading weights.
    """
    started = time.perf_counter()
    embed_query("warmup")
    elapsed = time.perf_counter() - started
    logger.info("Embedding warmup complete in %.1fs", elapsed)
    return elapsed


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    model = _load_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
    return vectors.astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]
