"""Integration: sessions/history round-trip + probe on live compose services.

Run explicitly: QUERYPULSE_IT=1 uv run pytest tests/integration
Requires: docker compose up -d, then uv run python -m demo.seed_demo.
"""

import os

import pytest

from agent.pipeline import AgentDeps
from config.settings import DEFAULT_CONFIG_PATH, Settings, load_app_config
from database.factory import build_telemetry_store
from knowledge.pg import metadata_query
from knowledge.store import KnowledgeStore
from memory.contracts import TurnInsert
from memory.pg import PGMemoryStore
from onboarding.state import OnboardingStateStore
from tests.fakes import FakeLLM

pytestmark = pytest.mark.skipif(
    os.environ.get("QUERYPULSE_IT") != "1",
    reason="integration tests run only with QUERYPULSE_IT=1",
)


@pytest.fixture
def memory() -> PGMemoryStore:
    return PGMemoryStore(metadata_query(Settings()))


class TestSessionsRoundTrip:
    def test_two_turns_persist_and_group(
        self, memory: PGMemoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = memory.create_session("demo", "Rpm Query")
        first = memory.append_turn(
            TurnInsert(
                tenant="demo",
                session_id=session.id,
                nl_query="average rpm?",
                sql="SELECT 1",
                data=({"avg": 1.0},),
                summary="avg 1.0",
                token_usage=5,
            )
        )
        second = memory.append_turn(
            TurnInsert(
                tenant="demo",
                session_id=session.id,
                nl_query="latest speed?",
                sql="SELECT 2",
                data=(),
                summary="none",
                token_usage=3,
            )
        )

        page = memory.list_history("demo")

        assert second > first
        demo_sessions = [item for item in page if item.session.id == session.id]
        assert len(demo_sessions) == 1
        turns = demo_sessions[0].turns
        assert [turn.nl_query for turn in turns] == ["average rpm?", "latest speed?"]
        assert turns[0].data == ({"avg": 1.0},)


class TestProbeIntegration:
    def test_probe_write_and_readiness_cache(self) -> None:
        query = metadata_query(Settings())
        state = OnboardingStateStore(query)

        state.save_probe("demo", {"keys": [{"key": "speed"}]})

        assert state.probe_cached("demo") is True
        assert state.probe_cached("never-probed-tenant") is False

    def test_demo_tenant_keys_registered_via_knowledge(self) -> None:
        knowledge = KnowledgeStore(query=metadata_query(Settings()))

        loaded = knowledge.load("demo")

        assert {"speed", "engine.rpm"} <= {entry.canonical_key for entry in loaded.keys}


class TestFactoryStore:
    def test_factory_builds_configured_store_with_events_template(self) -> None:
        app_config = load_app_config(DEFAULT_CONFIG_PATH)

        store = build_telemetry_store(app_config, Settings())

        assert store.dialect.name in ("clickhouse", "postgres")


class TestAgentDepsWiring:
    def test_deps_assemble_over_live_metadata(self) -> None:
        deps = AgentDeps(
            store=build_telemetry_store(load_app_config(DEFAULT_CONFIG_PATH), Settings()),
            knowledge=KnowledgeStore(query=metadata_query(Settings())),
            llm=FakeLLM(),
            config=load_app_config(DEFAULT_CONFIG_PATH),
        )

        assert deps.config.agent.row_cap > 0
