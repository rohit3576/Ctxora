"""Session digest tests: threshold, caching, invalidation on growth."""

from collections.abc import Sequence

from llm.client import GenResult
from memory.contracts import HistoryTurn
from memory.digest import DigestCache


class CountingLLM:
    """Fake LLM counting generate() calls."""

    def __init__(self) -> None:
        self.calls: int = 0

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.calls += 1
        return GenResult(
            sql="", raw="- truck-102 rpm discussed", prompt_tokens=1, completion_tokens=1
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


def turns(n: int) -> tuple[HistoryTurn, ...]:
    """n synthetic turns."""
    return tuple(
        HistoryTurn(
            id=i,
            session_id="s",
            nl_query=f"q{i}",
            sql="SELECT 1",
            data=(),
            summary=f"a{i}",
            token_usage=1,
        )
        for i in range(n)
    )


class TestDigestCache:
    def test_below_threshold_returns_none(self) -> None:
        cache = DigestCache(llm=CountingLLM(), turn_threshold=3)

        assert cache.digest_for("s", turns(2)) is None

    def test_at_threshold_builds_digest(self) -> None:
        llm = CountingLLM()
        cache = DigestCache(llm=llm, turn_threshold=3)

        digest = cache.digest_for("s", turns(3))

        assert digest is not None
        assert "rpm" in digest.text
        assert llm.calls == 1

    def test_same_turn_count_reuses_cache(self) -> None:
        llm = CountingLLM()
        cache = DigestCache(llm=llm, turn_threshold=3)
        cache.digest_for("s", turns(3))
        cache.digest_for("s", turns(3))

        assert llm.calls == 1

    def test_turn_growth_rebuilds(self) -> None:
        llm = CountingLLM()
        cache = DigestCache(llm=llm, turn_threshold=3)
        cache.digest_for("s", turns(3))
        cache.digest_for("s", turns(4))

        assert llm.calls == 2
