"""In-memory fakes for LLM and store contracts.

Fakes are real implementations of the protocols backed by canned data, so
tests exercise behavior (not mocks). test_contracts.py proves they satisfy
the protocols.
"""

from collections.abc import Sequence

from database.contracts import Dialect, EventTypeStat, ExecutionResult, KeyStat
from database.dialects.postgres import PostgresDialect
from llm.client import GenResult


class FakeLLM:
    """Deterministic LLM: always returns a trivial SELECT 1 in a fenced block."""

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(
            sql="SELECT 1",
            raw="```sql\nSELECT 1\n```",
            prompt_tokens=1,
            completion_tokens=1,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class FakeStore:
    """In-memory telemetry store: empty results, introspection included."""

    def __init__(self) -> None:
        self._dialect: Dialect = PostgresDialect()

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            rows=(),
            row_count=0,
            column_names=(),
            execution_time_ms=0.0,
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        return []

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return []
