"""
In-process sliding-window rate limiter.

SCOPE LIMIT — read before deploying: this state lives in one worker process's
memory. With N workers each enforces the limit independently, so the effective
limit is N x the configured value. It also resets on restart and does not
coordinate across machines.

That is adequate for protecting against accidental runaway clients and casual
abuse, which matters here because /report runs an embedding model per call —
an unthrottled loop is CPU-expensive to serve and trivially cheap to send. It
is NOT an abuse/DoS control. For that, enforce at the reverse proxy or API
gateway, or move this counter to Redis.
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

    def prune(self) -> int:
        """Drops tracking entries whose window has fully expired.

        Without this the `_hits` dict grows one entry per distinct client key
        forever — a slow memory leak on a public endpoint where the key is a
        client IP. Called opportunistically from the request path.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        removed = 0
        with self._lock:
            for key in list(self._hits.keys()):
                hits = self._hits[key]
                while hits and hits[0] < cutoff:
                    hits.popleft()
                if not hits:
                    del self._hits[key]
                    removed += 1
        return removed

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)
