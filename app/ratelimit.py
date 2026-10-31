import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """In-memory sliding-window limiter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def hit(self, key: str) -> bool:
        """Record a hit for `key`; return False if the limit is already reached."""
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            if now - self._last_sweep > self.window:
                self._sweep(cutoff)
                self._last_sweep = now
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def allowed(self, key: str) -> bool:
        """True if `key` is under the limit, without recording a hit (pair with `hit` on success)."""
        cutoff = self._clock() - self.window
        with self._lock:
            hits = self._hits.get(key)
            return hits is None or sum(1 for t in hits if t > cutoff) < self.limit

    def refund(self, key: str) -> None:
        """Remove one reservation for `key`, when work did not complete successfully."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()
                if not hits:
                    self._hits.pop(key, None)

    def _sweep(self, cutoff: float) -> None:
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
