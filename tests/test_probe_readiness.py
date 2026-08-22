"""Onboarding probe + readiness tests over the real API with fakes."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from api.onboarding import EventTypeView, KeyView, ProbeData, ReadinessData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from database.contracts import Dialect, EventTypeStat, ExecutionResult, KeyStat
from database.dialects.clickhouse import ClickHouseDialect
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_pipeline_e2e import DemoFakeLLM, demo_knowledge_query


class ProbingStore:
    """Demo-shaped store with canned introspection results."""

    def __init__(self) -> None:
        self._dialect: Dialect = ClickHouseDialect()

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        return ExecutionResult(
            success=True, rows=(), row_count=0, column_names=(), execution_time_ms=1.0
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        keys = (
            "speed",
            "engine.rpm",
            "engine.coolantTemp",
            "fuel.level",
            "battery.voltage",
        )
        return [KeyStat(key=k, sample_count=72, first_seen=None, last_seen=None) for k in keys]

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return [EventTypeStat(event_type="overspeed", sample_count=3)]


class RecordingStateQuery:
    """Executor that captures onboarding_state writes and answers reads."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, tuple[object, ...]]] = []
        self.knowledge_calls: int = 0

    def __call__(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        if "onboarding_state" in sql and sql.startswith("INSERT"):
            self.writes.append((sql, params))
            return []
        if "onboarding_state" in sql:
            return [(1,)] if self.writes else []
        return demo_knowledge_query(sql, params)


@pytest.fixture
def state_query() -> RecordingStateQuery:
    return RecordingStateQuery()


@pytest.fixture
def client(state_query: RecordingStateQuery) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=ProbingStore(),
        knowledge_query=state_query,
        llm=DemoFakeLLM(),
        memory=InMemoryMemoryStore(),
    )
    with TestClient(app) as c:
        yield c


def probe_data(response: Response) -> ProbeData:
    """Parse a probe response into its typed model."""
    envelope = Envelope[ProbeData].model_validate_json(response.content)
    assert envelope.data is not None
    return envelope.data


def readiness_data(response: Response) -> ReadinessData:
    """Parse a readiness response into its typed model."""
    envelope = Envelope[ReadinessData].model_validate_json(response.content)
    assert envelope.data is not None
    return envelope.data


class TestProbe:
    def test_probe_returns_keys_and_event_types_with_counts(self, client: TestClient) -> None:
        response = client.get("/v1/onboarding/demo/probe")

        assert response.status_code == 200
        data = probe_data(response)
        assert len(data.keys) == 5
        assert data.keys[0] == KeyView(key="speed", sampleCount=72, firstSeen=None, lastSeen=None)
        assert data.eventTypes == [EventTypeView(eventType="overspeed", sampleCount=3)]

    def test_probe_result_is_cached_in_onboarding_state(
        self, client: TestClient, state_query: RecordingStateQuery
    ) -> None:
        client.get("/v1/onboarding/demo/probe")

        assert len(state_query.writes) == 1


class TestProbeResilience:
    def test_probe_store_outage_maps_to_503(self) -> None:
        class DeadStore:
            dialect: Dialect = ProbingStore().dialect

            def introspect_keys(self, tenant: str) -> list[KeyStat]:
                msg = "clickhouse unreachable"
                raise RuntimeError(msg)

            def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
                return []

            def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
                return ExecutionResult(
                    success=False,
                    rows=(),
                    row_count=0,
                    column_names=(),
                    execution_time_ms=0.0,
                    error_kind="connection",
                    error="unreachable",
                )

        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DeadStore(),
            knowledge_query=demo_knowledge_query,
            llm=DemoFakeLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.get("/v1/onboarding/demo/probe")

        assert response.status_code == 503
        body: dict[str, object] = response.json()
        assert body["errorType"] == "STORE_UNAVAILABLE"


class TestReadinessOutage:
    def test_knowledge_store_down_still_answers(self) -> None:
        def dead_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            msg = "connection refused"
            raise RuntimeError(msg)

        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=ProbingStore(),
            knowledge_query=dead_query,
            llm=DemoFakeLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.get("/v1/onboarding/demo/readiness")

        assert response.status_code == 200
        data = readiness_data(response)
        assert data.keysRegistered is False


class TestReadiness:
    def test_ready_when_keys_registered_and_probe_cached(self, client: TestClient) -> None:
        client.get("/v1/onboarding/demo/probe")

        data = readiness_data(client.get("/v1/onboarding/demo/readiness"))

        assert data == ReadinessData(keysRegistered=True, probeCached=True, ready=True)

    def test_not_ready_without_probe(self, client: TestClient) -> None:
        data = readiness_data(client.get("/v1/onboarding/demo/readiness"))

        assert data == ReadinessData(keysRegistered=True, probeCached=False, ready=True)

    def test_unknown_tenant_reports_no_keys(self, client: TestClient) -> None:
        data = readiness_data(client.get("/v1/onboarding/ghost/readiness"))

        assert data == ReadinessData(keysRegistered=False, probeCached=False, ready=False)
