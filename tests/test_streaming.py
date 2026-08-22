"""SSE stream tests: event order, final==sync payload, ping heartbeat."""

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from config.settings import DEFAULT_CONFIG_PATH, Settings
from database.contracts import Dialect, EventTypeStat, ExecutionResult, KeyStat
from database.dialects.clickhouse import ClickHouseDialect
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore, demo_knowledge_query


class SlowStore:
    """Demo-shaped store that stalls execution long enough to trigger pings."""

    def __init__(self) -> None:
        self._inner: DemoStore = DemoStore()
        self._dialect: Dialect = ClickHouseDialect()

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        time.sleep(0.3)
        return self._inner.execute(sql, row_cap=row_cap, timeout_s=timeout_s)

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        return self._inner.introspect_keys(tenant)

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        return self._inner.introspect_event_types(tenant)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=DemoStore(),
        knowledge_query=demo_knowledge_query,
        llm=DemoFakeLLM(),
        memory=InMemoryMemoryStore(),
        stream_ping_interval_s=0.05,
    )
    with TestClient(app) as fast:
        yield fast


def parse_events(raw: str) -> list[tuple[str, dict[str, object]]]:
    """Parse an SSE body into (event, payload) pairs, ignoring comments."""
    events: list[tuple[str, dict[str, object]]] = []
    for frame in raw.split("\n\n"):
        lines = frame.strip().splitlines()
        name = next((ln.removeprefix("event: ") for ln in lines if ln.startswith("event: ")), None)
        data = next((ln.removeprefix("data: ") for ln in lines if ln.startswith("data: ")), None)
        if name and data:
            events.append((name, json.loads(data)))
    return events


class TestEventOrder:
    def test_stages_then_deltas_then_final(self, client: TestClient) -> None:
        fast = client
        raw = fast.post(
            "/v1/query/sql/stream", json={"tenant": "demo", "query": "average rpm?"}
        ).text
        events = parse_events(raw)
        names = [name for name, _ in events]
        stages = [payload["stage"] for name, payload in events if name == "stage"]

        assert stages == [
            "retrieving",
            "generating",
            "validating",
            "executing",
            "summarizing",
        ]
        first_delta = names.index("summary_delta")
        final = names.index("final")
        assert names[:5] == ["stage"] * 5
        assert first_delta > names.index("stage")
        assert final == len(names) - 1
        assert "ping" not in raw


class TestFinalMatchesSync:
    def test_final_payload_equals_sync_response(self, client: TestClient) -> None:
        fast = client
        sync = fast.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}).json()
        raw = fast.post(
            "/v1/query/sql/stream", json={"tenant": "demo", "query": "average rpm?"}
        ).text
        final = parse_events(raw)[-1][1]
        adapter = TypeAdapter(dict[str, object])
        for payload in (final, sync):
            data = adapter.validate_python(payload["data"])
            data["sessionId"] = None
            data["historyId"] = None
            payload["data"] = data

        assert final == sync

    def test_error_path_emits_error_event(self, client: TestClient) -> None:
        fast = client
        raw = fast.post(
            "/v1/query/sql/stream", json={"tenant": "ghost", "query": "average rpm?"}
        ).text
        name, payload = parse_events(raw)[-1]

        assert name == "error"
        assert payload["status"] == "Failure"
        assert payload["statusCode"] == 422


class TestPingHeartbeat:
    def test_ping_emitted_when_pipeline_stalls(self) -> None:
        memory = InMemoryMemoryStore()
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=SlowStore(),
            knowledge_query=demo_knowledge_query,
            llm=DemoFakeLLM(),
            memory=memory,
            stream_ping_interval_s=0.05,
        )
        with TestClient(app) as slow:
            raw = slow.post(
                "/v1/query/sql/stream", json={"tenant": "demo", "query": "average rpm?"}
            ).text

        assert ": ping" in raw
        events = parse_events(raw)
        assert events[-1][0] == "final"
