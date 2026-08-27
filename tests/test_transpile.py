"""S4 transpilation: dialect post-fixes, transpile wrapper, pipeline path.

Flag on + non-canonical store: the prompt is engine-neutral (postgres
grammar), the generation is validated canonically, engine idioms are
post-fixed on the canonical tree, the result is transpiled and validated
again through the target gauntlet. Any failure falls back to native
regeneration and logs the divergence (drift detector).
"""

import logging
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from agent.transpile import transpile_to
from config.settings import DEFAULT_CONFIG_PATH, Settings
from database.contracts import EventTypeStat, ExecutionResult, KeyStat
from database.dialects.clickhouse import ClickHouseDialect
from database.dialects.postgres import PostgresDialect
from llm.client import GenResult
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_knowledge_store import canned_query

_ROWS = TypeAdapter(dict[str, object])

CANONICAL_SQL = (
    "SELECT avg(NULLIF(value, '')::double precision) FROM demo_telemetry WHERE key = 'engine.rpm'"
)
NATIVE_CH_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"


class RecordingStore:
    """ClickHouse-dialect store capturing every executed statement."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    @property
    def dialect(self) -> ClickHouseDialect:
        return ClickHouseDialect()

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        self.executed.append(sql)
        return ExecutionResult(
            success=True,
            rows=({"device_id": "truck-102", "avg": 1487.5},),
            row_count=1,
            column_names=("device_id", "avg"),
            execution_time_ms=2.0,
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        return []

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return []


class ScriptedLLM:
    """First reply canonical; later replies native (fallback regeneration)."""

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies: list[str] = list(replies)
        self.generation_prompts: list[str] = []
        self.system_prompts: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.generation_prompts.append(user)
        self.system_prompts.append(system)
        reply = self.replies.pop(0) if self.replies else self._last_reply()
        return GenResult(
            sql=reply, raw=f"```sql\n{reply}\n```", prompt_tokens=5, completion_tokens=5
        )

    def _last_reply(self) -> str:
        return self.replies[-1] if self.replies else NATIVE_CH_SQL

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1] for _ in texts]


def _tuned_config(tmp_path: Path) -> Path:
    tuned = Path(DEFAULT_CONFIG_PATH).read_text()
    tuned = tuned.replace("transpile_parity: false", "transpile_parity: true")
    target = tmp_path / "defaults-tuned.yaml"
    target.write_text(tuned)
    return target


def _client(tmp_path: Path, llm: ScriptedLLM, store: RecordingStore) -> TestClient:
    app = create_app(
        settings=Settings(),
        config_path=_tuned_config(tmp_path),
        store=store,
        knowledge_query=_minimal_knowledge,
        llm=llm,
        memory=InMemoryMemoryStore(),
    )
    return TestClient(app)


_minimal_knowledge = canned_query


class TestPostfixCanonical:
    def test_clickhouse_rewrites_nullsafe_cast(self) -> None:
        postfixed = ClickHouseDialect().postfix_canonical(
            "SELECT avg(NULLIF(value, '')::double precision) FROM demo_telemetry"
        )

        assert "toFloat64OrNull(value)" in postfixed
        assert "NULLIF" not in postfixed

    def test_clickhouse_rewrites_ordered_array_agg_to_argmax(self) -> None:
        postfixed = ClickHouseDialect().postfix_canonical(
            "SELECT device_id, (array_agg(value ORDER BY timestamp DESC))[1] "
            "FROM demo_telemetry GROUP BY device_id"
        )

        assert "argMax(value, timestamp)" in postfixed
        assert "array_agg" not in postfixed.lower()

    def test_clickhouse_leaves_clean_sql_untouched(self) -> None:
        sql = "SELECT key FROM demo_telemetry WHERE timestamp >= now() - INTERVAL '1 day'"

        assert ClickHouseDialect().postfix_canonical(sql) == sql

    def test_clickhouse_unparseable_passes_through(self) -> None:
        assert ClickHouseDialect().postfix_canonical("GARBAGE $$$") == "GARBAGE $$$"

    def test_postgres_is_identity(self) -> None:
        assert PostgresDialect().postfix_canonical(CANONICAL_SQL) == CANONICAL_SQL


class TestTranspileTo:
    def test_transpiles_single_statement(self) -> None:
        out = transpile_to("SELECT 1 FROM demo_telemetry", "clickhouse")

        assert out is not None
        assert out.upper().startswith("SELECT")

    def test_multi_statement_is_none(self) -> None:
        assert transpile_to("SELECT 1; SELECT 2", "clickhouse") is None

    def test_garbage_is_none(self) -> None:
        assert transpile_to("GARBAGE $$$", "clickhouse") is None


class TestPipelineTranspilePath:
    def test_flag_on_executes_transpiled_idiomatic_sql(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = ScriptedLLM([CANONICAL_SQL])
        store = RecordingStore()
        with _client(tmp_path, llm, store) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            )

        assert response.status_code == 200
        assert len(store.executed) == 1
        executed = store.executed[0]
        assert "toFloat64OrNull" in executed
        assert "NULLIF" not in executed
        question_prompts = [p for p in llm.generation_prompts if "SCHEMA" in p and "ROWS:" not in p]
        assert len(question_prompts) == 1, "exactly one generation call"
        canonical_system = llm.system_prompts[0]
        assert "NULLIF" in canonical_system, "canonical (postgres) prompt"
        assert "toFloat64OrNull" not in canonical_system

    def test_divergence_falls_back_to_native_regeneration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import agent.pipeline as pipeline_module

        def _broken(sql: str, write: str) -> str | None:
            return None

        monkeypatch.setattr(pipeline_module, "transpile_to", _broken)
        llm = ScriptedLLM([CANONICAL_SQL, NATIVE_CH_SQL])
        store = RecordingStore()
        with _client(tmp_path, llm, store) as client:
            with caplog.at_level(logging.WARNING, logger="ctxora.pipeline"):
                response = client.post(
                    "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
                )

        assert response.status_code == 200
        question_prompts = [p for p in llm.generation_prompts if "SCHEMA" in p and "ROWS:" not in p]
        assert len(question_prompts) == 2, "fallback regenerates natively"
        assert "toFloat64OrNull" in llm.system_prompts[1], "native prompt on fallback"
        executed = store.executed[0]
        assert "toFloat64OrNull" in executed
        assert any("transpile divergence" in record.message for record in caplog.records)

    def test_flag_off_keeps_native_prompt(self, tmp_path: Path) -> None:
        llm = ScriptedLLM([NATIVE_CH_SQL])
        store = RecordingStore()
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=store,
            knowledge_query=_minimal_knowledge,
            llm=llm,
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            )

        assert response.status_code == 200
        assert "toFloat64OrNull" in llm.system_prompts[0], "native CH prompt idiom"
