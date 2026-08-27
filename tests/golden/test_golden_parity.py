"""Golden parity suite: 20 questions x two dialect stores, rows asserted strict.

Validates that the pipeline is engine-independent: flipping the adapter must
not change answers. SQL snapshots are recorded per dialect (printed on
failure) but not asserted, per the Phase 6 decision — LLM regeneration churn
must not break parity testing.
"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from config.settings import DEFAULT_CONFIG_PATH, Settings
from database.contracts import Dialect, EventTypeStat, ExecutionResult, KeyStat
from database.dialects.clickhouse import ClickHouseDialect
from database.dialects.postgres import PostgresDialect
from llm.client import GenResult
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_knowledge_store import canned_query

_ROWS = TypeAdapter(list[dict[str, object]])


def golden_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
    """canned_query plus the wider key registry the 20 questions need."""
    tenant = str(params[0]) if params else ""
    if tenant != "demo":
        return []
    if "sql_agent_telemetry_registry" in sql:
        return [
            *canned_query(sql, params),
            ("speed", "speed", "Speed", "numeric", "km/h", "average", "", "0..120", ""),
            (
                "engine.coolantTemp",
                "engine.coolantTemp",
                "Coolant",
                "numeric",
                "C",
                "average",
                "",
                "70..95",
                "",
            ),
            ("fuel.level", "fuel", "Fuel", "numeric", "%", "latest", "", "10..100", ""),
            ("engine.load", "engine.load", "Load", "numeric", "%", "average", "", "0..100", ""),
        ]
    if "sql_agent_aliases" in sql:
        return [
            *canned_query(sql, params),
            ("speed", "speed", "", ""),
            ("temperature", "engine.coolantTemp", "", ""),
            ("coolant", "engine.coolantTemp", "", ""),
            ("fuel", "fuel.level", "", ""),
            ("voltage", "battery.voltage", "", ""),
            ("load", "engine.load", "", ""),
        ]
    return canned_query(sql, params)


QUESTIONS: tuple[str, ...] = (
    "What was the average rpm of truck-102 yesterday?",
    "Average speed of the fleet today?",
    "Maximum rpm recorded last week?",
    "Latest battery voltage for truck-101?",
    "Minimum fuel level this week?",
    "What speed did truck-103 reach yesterday?",
    "Average engine speed today?",
    "What about truck-102 speed?",
    "Show me revs for truck-101",
    "Daily average rpm trend?",
    "Top speed per truck?",
    "Current battery level?",
    "Fuel usage yesterday?",
    "rpm summary for the fleet",
    "average rpm and speed yesterday?",
    "what was the slowest speed today?",
    "peak voltage this week?",
    "mean temperature yesterday?",
    "speed of truck-102 at noon?",
    "overall average rpm?",
)

DEMO_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"
DEMO_ROW = [{"device_id": "truck-102", "avg_rpm": 1487.5}]


class GoldenStore:
    """Dialect-carrying store answering every validated query identically."""

    def __init__(self, dialect: Dialect) -> None:
        self._dialect: Dialect = dialect
        self.executed: list[str] = []

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        self.executed.append(sql)
        return ExecutionResult(
            success=True,
            rows=({"device_id": "truck-102", "avg_rpm": 1487.5},),
            row_count=1,
            column_names=("device_id", "avg_rpm"),
            execution_time_ms=2.0,
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        return []

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return []


class GoldenLLM:
    """Answers every data question with the demo SQL and a fixed summary."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.prompts.append(user)
        if "QUESTION:" in user and "ROWS:" not in user:
            return GenResult(
                sql=DEMO_SQL, raw=f"```sql\n{DEMO_SQL}\n```", prompt_tokens=10, completion_tokens=10
            )
        return GenResult(
            sql="",
            raw="Truck-102 averaged 1487.5 rpm yesterday.",
            prompt_tokens=5,
            completion_tokens=5,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1] for _ in texts]


@pytest.fixture(params=["clickhouse", "postgres"], ids=["ch", "pg"])
def golden_client(request: pytest.FixtureRequest) -> Iterator[tuple[TestClient, GoldenStore]]:
    dialect = (
        ClickHouseDialect()
        if request.param == "clickhouse"
        else PostgresDialect(use_timescale=True)
    )
    store = GoldenStore(dialect)
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=store,
        knowledge_query=golden_query,
        llm=GoldenLLM(),
        memory=InMemoryMemoryStore(),
    )
    with TestClient(app) as client:
        yield client, store


def answer_for(client: TestClient, question: str) -> tuple[int, object]:
    """POST one question; return (status, data-or-None)."""
    response = client.post("/v1/query/sql", json={"tenant": "demo", "query": question})
    payload: dict[str, object] = response.json()
    return response.status_code, payload.get("data")


@pytest.mark.parametrize("question", QUESTIONS, ids=lambda q: q[:38])
def test_golden_question_answered_identically_across_dialects(
    golden_client: tuple[TestClient, GoldenStore], question: str
) -> None:
    client, store = golden_client

    status, data = answer_for(client, question)

    assert status == 200, f"golden question failed: {question}"
    envelope_data = TypeAdapter(dict[str, object]).validate_python(data)
    assert envelope_data["rows"] == DEMO_ROW
    assert envelope_data["sql"]
    assert envelope_data["summary"]
    assert envelope_data["resolvedKeys"]
    snapshot = store.executed[-1]
    assert snapshot.lstrip().upper().startswith("SELECT")


def test_dialect_snapshots_differ_between_adapters(tmp_path: object) -> None:
    """Sanity: the two stores really run different dialects (prompt EAV rules)."""
    from agent.prompt_builder import PromptBuilder
    from config.settings import DEFAULT_CONFIG_PATH as DCP
    from config.settings import load_app_config
    from knowledge.store import KnowledgeStore

    mapping = load_app_config(DCP).stores.telemetry.mapping
    knowledge = KnowledgeStore(query=golden_query).load("demo")

    ch = PromptBuilder(dialect=ClickHouseDialect(), mapping=mapping).build(knowledge, (), "q")
    pg = PromptBuilder(dialect=PostgresDialect(), mapping=mapping).build(knowledge, (), "q")

    assert "toFloat64OrNull" in ch[0]
    assert "NULLIF" in pg[0]


CANONICAL_SQL = (
    "SELECT avg(NULLIF(value, '')::double precision) FROM demo_telemetry WHERE key = 'engine.rpm'"
)


class TranspileGoldenLLM:
    """Answers every question with the CANONICAL (postgres-grammar) SQL.

    S4's mechanical parity leg: one engine-neutral generation, transpiled
    and post-fixed per store — no hand-scripted per-dialect SQL anywhere.
    """

    def __init__(self) -> None:
        self.generation_prompts: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.generation_prompts.append(user)
        if "QUESTION:" in user and "ROWS:" not in user:
            return GenResult(
                sql=CANONICAL_SQL,
                raw=f"```sql\n{CANONICAL_SQL}\n```",
                prompt_tokens=10,
                completion_tokens=10,
            )
        return GenResult(
            sql="",
            raw="Truck-102 averaged 1487.5 rpm yesterday.",
            prompt_tokens=5,
            completion_tokens=5,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1] for _ in texts]


@pytest.fixture(params=["clickhouse", "postgres"], ids=["ch-t", "pg-t"])
def transpile_client(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[TestClient, GoldenStore, TranspileGoldenLLM]]:
    dialect = (
        ClickHouseDialect()
        if request.param == "clickhouse"
        else PostgresDialect(use_timescale=True)
    )
    store = GoldenStore(dialect)
    llm = TranspileGoldenLLM()
    tuned = (
        Path(DEFAULT_CONFIG_PATH)
        .read_text()
        .replace("transpile_parity: false", "transpile_parity: true")
    )
    config_path = tmp_path / "defaults-transpile.yaml"
    config_path.write_text(tuned)
    app = create_app(
        settings=Settings(),
        config_path=config_path,
        store=store,
        knowledge_query=golden_query,
        llm=llm,
        memory=InMemoryMemoryStore(),
    )
    with TestClient(app) as client:
        yield client, store, llm


@pytest.mark.parametrize("question", QUESTIONS, ids=lambda q: q[:38])
def test_golden_question_through_transpile_path(
    transpile_client: tuple[TestClient, GoldenStore, TranspileGoldenLLM], question: str
) -> None:
    """S4 acceptance: one canonical generation, both engines, strict rows."""
    client, store, llm = transpile_client

    status, data = answer_for(client, question)

    assert status == 200, f"golden question failed through transpile path: {question}"
    envelope_data = TypeAdapter(dict[str, object]).validate_python(data)
    assert envelope_data["rows"] == DEMO_ROW
    assert envelope_data["sql"]
    question_prompts = [p for p in llm.generation_prompts if "SCHEMA" in p and "ROWS:" not in p]
    assert len(question_prompts) == 1, "exactly one generation call per question"
    executed = store.executed[-1]
    assert executed.lstrip().upper().startswith("SELECT")
    if isinstance(store.dialect, ClickHouseDialect):
        assert "toFloat64OrNull" in executed, "post-fixed engine idiom on ClickHouse"
        assert "NULLIF" not in executed
    else:
        assert "NULLIF" in executed, "canonical idiom runs natively on Postgres"
