"""Per-tenant in-memory token bucket (restart resets counters; v1.0 scope)."""

import threading
import time
from dataclasses import dataclass

from config.settings import RatelimitConfig


@dataclass(slots=True)
class _Bucket:
    """One tenant's bucket state (mutable by design)."""

    tokens: float
    last_refill: float


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    """One admission decision."""

    allowed: bool
    remaining: int
    retry_after_s: int


class TokenBucketLimiter:
    """Thread-safe token buckets keyed by tenant."""

    def __init__(self, config: RatelimitConfig) -> None:
        """Bind bucket config (capacity = rpm burst headroom)."""
        self._config: RatelimitConfig = config
        self._capacity: float = float(config.burst)
        self._refill_per_s: float = config.requests_per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock: threading.Lock = threading.Lock()

    def admit(self, tenant: str, now: float | None = None) -> RateLimitVerdict:
        """Consume one token if available."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(tenant)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=moment)
                self._buckets[tenant] = bucket
            elapsed = max(moment - bucket.last_refill, 0.0)
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_s)
            bucket.last_refill = moment
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateLimitVerdict(allowed=True, remaining=int(bucket.tokens), retry_after_s=0)
            wait = (1.0 - bucket.tokens) / self._refill_per_s
            return RateLimitVerdict(allowed=False, remaining=0, retry_after_s=max(int(wait) + 1, 1))
