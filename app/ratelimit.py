import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from typing import ClassVar

# How often every key is checked for expiry, at most. Together with the per-key pruning on each access
# this bounds how long a key (a client IP) is kept: at most `window + SWEEP_INTERVAL` seconds after its
# newest hit, i.e. about an hour for the hourly petition limits (see app/templates/privacy.html).
SWEEP_INTERVAL = 60.0


class RateLimiter:
    """In-memory sliding-window limiter: at most `limit` hits per `window` seconds per key.

    Expiry is exact: a key's timestamps older than the window are pruned whenever the key is accessed,
    a key with no hit inside the window is removed, and all keys are swept at least every
    SWEEP_INTERVAL seconds (on the next access, and by a background daemon thread when the server is idle).
    """

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic,
                 background_sweep: bool = True) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()
        if background_sweep:
            _Sweeper.register(self)

    def hit(self, key: str) -> bool:
        """Record a hit for `key`; return False if the limit is already reached."""
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            hits = self._prune(key, now)
            if hits is None:
                hits = self._hits[key] = deque()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def allowed(self, key: str) -> bool:
        """True if `key` is under the limit, without recording a hit (pair with `hit` on success)."""
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            hits = self._prune(key, now)
            return hits is None or len(hits) < self.limit

    def refund(self, key: str) -> None:
        """Remove one reservation for `key`, when work did not complete successfully."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()
                if not hits:
                    self._hits.pop(key, None)

    def sweep(self) -> None:
        """Drop every key whose newest hit is outside the window."""
        now = self._clock()
        with self._lock:
            self._sweep(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._hits

    def _prune(self, key: str, now: float) -> deque[float] | None:
        """Drop `key`'s expired timestamps; remove the key entirely if none are left. Lock held."""
        hits = self._hits.get(key)
        if hits is None:
            return None
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep >= SWEEP_INTERVAL:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
        self._last_sweep = now


class _Sweeper:
    """One daemon thread that sweeps every live RateLimiter each SWEEP_INTERVAL seconds, so IPs expire
    even when no further requests arrive. Holds limiters weakly (they die with their app)."""

    _limiters: ClassVar["weakref.WeakSet[RateLimiter]"] = weakref.WeakSet()
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _thread: ClassVar[threading.Thread | None] = None

    @classmethod
    def register(cls, limiter: RateLimiter) -> None:
        with cls._lock:
            cls._limiters.add(limiter)
            if cls._thread is None or not cls._thread.is_alive():
                cls._thread = threading.Thread(target=cls._run, name="ratelimit-sweeper", daemon=True)
                cls._thread.start()

    @classmethod
    def _run(cls) -> None:
        while True:
            time.sleep(SWEEP_INTERVAL)
            with cls._lock:
                limiters = list(cls._limiters)
            for limiter in limiters:
                limiter.sweep()
