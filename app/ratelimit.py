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

    def _sweep(self, cutoff: float) -> None:
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
