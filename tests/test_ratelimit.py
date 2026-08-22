"""Rate limiter tests: burst, refill, per-tenant isolation."""

from api.ratelimit import TokenBucketLimiter
from config.settings import RatelimitConfig

CONFIG = RatelimitConfig(requests_per_minute=60, burst=5)


class TestTokenBucket:
    def test_burst_admitted_then_throttled(self) -> None:
        limiter = TokenBucketLimiter(CONFIG)
        clock = 1000.0

        verdicts = [limiter.admit("demo", now=clock) for _ in range(7)]

        assert [v.allowed for v in verdicts] == [True] * 5 + [False] * 2
        assert verdicts[6].retry_after_s >= 1

    def test_refill_restores_capacity_over_time(self) -> None:
        limiter = TokenBucketLimiter(CONFIG)
        for _ in range(5):
            limiter.admit("demo", now=0.0)
        throttled = limiter.admit("demo", now=0.0)
        later = limiter.admit("demo", now=2.0)

        assert throttled.allowed is False
        assert later.allowed is True

    def test_tenants_are_isolated(self) -> None:
        limiter = TokenBucketLimiter(CONFIG)

        for _ in range(5):
            limiter.admit("demo", now=0.0)
        other = limiter.admit("other", now=0.0)

        assert other.allowed is True

    def test_capacity_never_exceeds_burst(self) -> None:
        limiter = TokenBucketLimiter(CONFIG)
        limiter.admit("demo", now=0.0)
        after_idle = limiter.admit("demo", now=10_000.0)

        assert after_idle.remaining <= CONFIG.burst - 1
