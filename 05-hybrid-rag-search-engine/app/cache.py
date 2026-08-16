"""
TTL + LRU cache for query results.

Chosen on measured evidence, not instinct: eval/benchmark_pipeline.py shows
the cross-encoder reranker is ~90% of query latency (161ms of 178ms), while
vector search is ~0.1%. Caching whole query results therefore removes far
more real latency than any vector-index optimisation would at this scale.

SECURITY-CRITICAL: the cache key MUST include the caller's ACL groups.
Keying on the question alone would let a low-privilege caller receive a
cached answer generated for a privileged one — a data leak that no amount
of correct SQL filtering would catch, because the query never runs. The
groups are normalised (sorted, de-duplicated) so equivalent group sets share
an entry without ever merging different ones.
"""
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Iterable, Optional

from . import config

logger = logging.getLogger(__name__)


def make_cache_key(question: str, user_groups: Optional[Iterable[str]]) -> str:
    groups = sorted({g.strip() for g in (user_groups or []) if g and g.strip()})
    # Pipeline version included so a chunking/model change can't serve stale
    # answers built by superseded logic.
    payload = json.dumps(
        {
            "q": question.strip().lower(),
            "g": groups,
            "v": config.PIPELINE_VERSION,
            "m": config.EMBEDDING_MODEL,
            "p": config.LLM_PROVIDER,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class QueryCache:
    def __init__(self, max_size: int, ttl_seconds: int):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if now - stored_at > self.ttl_seconds:
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)

    def clear(self) -> None:
        """Called on ingest — new documents can change any answer, including
        turning a previous 'not found' into a real result."""
        with self._lock:
            self._store.clear()
            logger.debug("Query cache cleared")

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "max_size": self.max_size,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


_cache: Optional[QueryCache] = None
_cache_lock = threading.Lock()


def get_cache() -> QueryCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = QueryCache(
                max_size=config.QUERY_CACHE_SIZE,
                ttl_seconds=config.QUERY_CACHE_TTL_SECONDS,
            )
        return _cache
