"""
In-process sliding-window rate limiter, keyed by principal.

SCOPE LIMIT — read before deploying: this state lives in the worker process's
memory. With N uvicorn/gunicorn workers, each enforces the limit
independently, so the effective limit is N x the configured value. It also
resets on restart and does not coordinate across machines.

That is adequate for a single-worker deployment and for protecting against
accidental runaway clients. It is NOT adequate as an abuse/DoS control or for
any multi-worker deployment — for that, move the counter to Redis
(see DEPLOYMENT.md) or enforce limits at the reverse proxy / API gateway.
"""
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = max(1, int(hits[0] + self.window_seconds - now) + 1)
                return False, retry_after
            hits.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
