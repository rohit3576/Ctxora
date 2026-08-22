"""Session + history API tests: sessionId round-trip, isolation, resilience."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api.history import SessionView
from api.query import QueryResponseData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from main import create_app
from memory.contracts import HistoryTurn, Session, SessionHistory, TurnInsert
from memory.fake import InMemoryMemoryStore
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore, demo_knowledge_query


class BrokenMemory:
    """Every operation fails: exercises the non-blocking contract."""

    def create_session(self, tenant: str, title: str) -> Session:
        msg = "metadata db down"
        raise RuntimeError(msg)

    def fetch_session(self, session_id: str) -> Session | None:
        msg = "metadata db down"
        raise RuntimeError(msg)

    def append_turn(self, turn: TurnInsert) -> int:
        msg = "metadata db down"
        raise RuntimeError(msg)

    def list_history(self, tenant: str, limit: int = 100) -> list[SessionHistory]:
        msg = "metadata db down"
        raise RuntimeError(msg)

    def find_turn(self, history_id: int) -> HistoryTurn | None:
        msg = "metadata db down"
        raise RuntimeError(msg)


@pytest.fixture
def memory() -> InMemoryMemoryStore:
    return InMemoryMemoryStore()


@pytest.fixture
def client(memory: InMemoryMemoryStore) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=DemoStore(),
        knowledge_query=demo_knowledge_query,
        llm=DemoFakeLLM(),
        memory=memory,
    )
    with TestClient(app) as c:
        yield c


def query_data(client: TestClient, payload: dict[str, object]) -> QueryResponseData:
    """POST one question and return the typed success payload."""
    response = client.post("/v1/query/sql", json=payload)
    envelope = Envelope[QueryResponseData].model_validate_json(response.content)
    assert envelope.data is not None
    return envelope.data


class TestSessionRoundTrip:
    def test_no_session_id_creates_session_and_returns_it(self, client: TestClient) -> None:
        data = query_data(client, {"tenant": "demo", "query": "average rpm?"})

        assert data.sessionId is not None
        assert data.historyId is not None

    def test_two_turns_share_one_session_and_persist(
        self, client: TestClient, memory: InMemoryMemoryStore
    ) -> None:
        first = query_data(client, {"tenant": "demo", "query": "average rpm?"})
        second = query_data(
            client,
            {"tenant": "demo", "query": "latest speed?", "sessionId": first.sessionId},
        )

        assert second.sessionId == first.sessionId
        assert second.historyId != first.historyId

        page = memory.list_history("demo")
        assert len(page) == 1
        assert len(page[0].turns) == 2
        assert page[0].turns[0].nl_query == "average rpm?"

    def test_title_is_deterministic_keyword_title(
        self, client: TestClient, memory: InMemoryMemoryStore
    ) -> None:
        query_data(client, {"tenant": "demo", "query": "average rpm?"})

        page = memory.list_history("demo")

        assert page[0].session.title == "Rpm Query"

    def test_unknown_session_id_is_400(self, client: TestClient) -> None:
        response = client.post(
            "/v1/query/sql",
            json={
                "tenant": "demo",
                "query": "average rpm?",
                "sessionId": "00000000-0000-0000-0000-000000000000",
            },
        )

        assert response.status_code == 400

    def test_session_of_other_tenant_is_400(self, client: TestClient) -> None:
        created = query_data(client, {"tenant": "demo", "query": "average rpm?"})

        response = client.post(
            "/v1/query/sql",
            json={"tenant": "other", "query": "average rpm?", "sessionId": created.sessionId},
        )

        assert response.status_code == 400


class TestHistoryEndpoint:
    def test_history_lists_sessions_newest_first_with_titles(self, client: TestClient) -> None:
        query_data(client, {"tenant": "demo", "query": "average rpm?"})
        query_data(client, {"tenant": "demo", "query": "latest speed?"})

        response = client.get("/v1/history", params={"tenant": "demo"})

        assert response.status_code == 200
        envelope = Envelope[list[SessionView]].model_validate_json(response.content)
        sessions = envelope.data
        assert sessions is not None
        assert len(sessions) == 2
        assert sessions[0].title == "Speed Query"

    def test_history_is_tenant_scoped(
        self, client: TestClient, memory: InMemoryMemoryStore
    ) -> None:
        other = memory.create_session("other", "Theirs")

        response = client.get("/v1/history", params={"tenant": "demo"})

        envelope = Envelope[list[SessionView]].model_validate_json(response.content)
        sessions = envelope.data
        assert sessions is not None
        assert all(item.sessionId != other.id for item in sessions)


class TestMemoryResilience:
    def test_query_answers_200_when_memory_is_down(self) -> None:
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=DemoFakeLLM(),
            memory=BrokenMemory(),
        )
        with TestClient(app) as client:
            data = query_data(client, {"tenant": "demo", "query": "average rpm?"})

        assert data.sessionId is None
