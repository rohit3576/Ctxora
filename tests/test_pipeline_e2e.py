"""Pipeline tests: end-to-end with fakes (S5->S13 happy + failure paths)."""

from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from api.query import QueryResponseData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from database.contracts import (
    Dialect,
    EventTypeStat,
    ExecutionResult,
    KeyStat,
    TelemetryStore,
)
from database.dialects.clickhouse import ClickHouseDialect
from knowledge.store import KnowledgeStore
from llm.client import GenResult, LLMClient
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.fakes import FakeLLM

DEMO_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"


class DemoStore:
    """Telemetry store serving the acceptance-demo row."""

    def __init__(self) -> None:
        self.executed_sql: str = ""
        self._dialect: Dialect = ClickHouseDialect()

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        self.executed_sql = sql
        return ExecutionResult(
            success=True,
            rows=({"device_id": "truck-102", "avg_rpm": 1487.5},),
            row_count=1,
            column_names=("device_id", "avg_rpm"),
            execution_time_ms=3.2,
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        return []

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return []


def demo_knowledge_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
    tenant = str(params[0]) if params else ""
    if sql.lower().startswith("select status from sql_agent_tenants"):
        return [("active",)] if tenant == "demo" else []
    if tenant != "demo":
        return []
    if "FROM sql_agent_tenants WHERE" in sql:
        return [("",)]
    if "sql_agent_telemetry_registry" in sql:
        return [
            (
                "engine.rpm",
                "engine.rpm",
                "Engine RPM",
                "numeric",
                "rpm",
                "average",
                "",
                "600..3000",
                "",
            ),
            ("speed", "speed", "Speed", "numeric", "km/h", "average", "", "0..120", ""),
        ]
    if "sql_agent_aliases" in sql:
        return [
            ("rpm", "engine.rpm", "", ""),
            ("speed", "speed", "", ""),
        ]
    if "sql_agent_business_rules" in sql:
        return [(1, "Bound multi-metric CTEs with a timestamp filter.")]
    if "sql_agent_sql_examples" in sql:
        return [("average rpm?", f"{DEMO_SQL}", "", "telemetry", "demo_telemetry")]
    if "sql_agent_schema_columns" in sql:
        return [
            ("demo_telemetry", "timestamp", "DateTime", ""),
            ("demo_telemetry", "device_id", "String", ""),
            ("demo_telemetry", "key", "String", ""),
            ("demo_telemetry", "value", "String", ""),
        ]
    if "sql_agent_table_metadata" in sql:
        return [("demo_telemetry", "eav", "telemetry readings", "timestamp")]
    return []


class DemoFakeLLM:
    """Serves the demo SQL, DELETE when asked, then a summary."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.calls.append(user)
        lowered = user.lower()
        if "delete" in lowered and "ROWS:" not in user:
            delete_sql = "DELETE FROM demo_telemetry"
            return GenResult(
                sql=delete_sql,
                raw=f"```sql\n{delete_sql}\n```",
                prompt_tokens=5,
                completion_tokens=5,
            )
        if "QUESTION:" in user and "ROWS:" not in user:
            return GenResult(
                sql=DEMO_SQL, raw=f"```sql\n{DEMO_SQL}\n```", prompt_tokens=10, completion_tokens=20
            )
        return GenResult(
            sql="",
            raw="Truck-102 averaged 1487.5 rpm yesterday.",
            prompt_tokens=5,
            completion_tokens=8,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class BrokenLLM:
    """Never fences its SQL: forces the generation-failure path."""

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        if "ROWS:" not in user:
            return GenResult(sql="", raw="no fence here", prompt_tokens=1, completion_tokens=1)
        return GenResult(sql="", raw="summary", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


@pytest.fixture
def demo_store() -> DemoStore:
    return DemoStore()


@pytest.fixture
def app_client(demo_store: DemoStore) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=demo_store,
        knowledge_query=demo_knowledge_query,
        llm=DemoFakeLLM(),
        memory=InMemoryMemoryStore(),
    )
    with TestClient(app) as client:
        yield client


class TestQuerySQLEndpoint:
    def test_acceptance_demo_average_rpm(self, app_client: TestClient) -> None:
        response = app_client.post(
            "/v1/query/sql",
            json={"tenant": "demo", "query": "What was the average rpm of truck-102 yesterday?"},
        )

        assert response.status_code == 200
        envelope = Envelope[QueryResponseData].model_validate_json(response.content)
        assert envelope.status == "Success"
        data = envelope.data
        assert data is not None
        assert data.sql == DEMO_SQL
        assert data.rows == [{"device_id": "truck-102", "avg_rpm": 1487.5}]
        assert "1487.5" in data.summary
        assert data.resolvedKeys == ["engine.rpm"]
        assert data.repairsApplied == []

    def test_delete_query_is_rejected_with_400(self, app_client: TestClient) -> None:
        response = app_client.post(
            "/v1/query/sql",
            json={"tenant": "demo", "query": "delete all telemetry"},
        )

        assert response.status_code == 400
        body: dict[str, object] = response.json()
        assert body["status"] == "Failure"

    def test_unknown_tenant_maps_to_422(self, app_client: TestClient) -> None:
        response = app_client.post(
            "/v1/query/sql",
            json={"tenant": "ghost", "query": "average rpm?"},
        )

        assert response.status_code == 422

    def test_metadata_db_down_maps_to_503(self) -> None:
        def broken_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            msg = "connection refused"
            raise RuntimeError(msg)

        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=broken_query,
            llm=DemoFakeLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            )

        assert response.status_code == 503

    def test_generation_failure_maps_to_502(self) -> None:
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=BrokenLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            )

        assert response.status_code == 502


class TestTenantKnowledgeLoaded:
    def test_knowledge_is_cached_across_requests(self, app_client: TestClient) -> None:
        for _ in range(2):
            app_client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})

        metrics = KnowledgeStore.metrics()
        assert metrics["cache_misses"] == 1
        assert metrics["cache_hits"] == 1


class TestStoreExecuted:
    def test_validated_sql_reaches_the_store(
        self, app_client: TestClient, demo_store: DemoStore
    ) -> None:
        app_client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})

        assert demo_store.executed_sql == DEMO_SQL


class TestProtocolConformance:
    def test_demo_store_satisfies_contract(self) -> None:
        assert isinstance(DemoStore(), TelemetryStore)

    def test_fake_llms_satisfy_contract(self) -> None:
        assert isinstance(FakeLLM(), LLMClient)
        assert isinstance(DemoFakeLLM(), LLMClient)
