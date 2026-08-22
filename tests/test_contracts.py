"""Contract tests: store stubs wire dialects, fakes satisfy protocols."""

import pytest

from config.settings import (
    DEFAULT_CONFIG_PATH,
    ColumnMapping,
    Settings,
    load_app_config,
)
from database.clickhouse_store import ClickHouseStore
from database.contracts import (
    ExecutionResult,
    KeyStat,
    TelemetryStore,
)
from database.postgres_store import PostgresStore
from llm.client import GenResult, LLMClient
from tests.fakes import FakeLLM, FakeStore


@pytest.fixture
def mapping() -> ColumnMapping:
    return load_app_config(DEFAULT_CONFIG_PATH).stores.telemetry.mapping


@pytest.fixture
def settings() -> Settings:
    return Settings()


class TestStoreStubs:
    def test_clickhouse_store_wires_clickhouse_dialect(self, mapping: ColumnMapping) -> None:
        store = ClickHouseStore(mapping=mapping, settings=Settings())

        assert store.dialect.name == "clickhouse"

    def test_postgres_store_wires_postgres_dialect(self, mapping: ColumnMapping) -> None:
        store = PostgresStore(mapping=mapping, settings=Settings())

        assert store.dialect.name == "postgres"

    def test_pg_execute_on_unreachable_db_returns_connection_error(
        self, mapping: ColumnMapping
    ) -> None:
        store = PostgresStore(mapping=mapping, settings=Settings(telemetry_db_port=1))

        result = store.execute("SELECT 1", row_cap=10, timeout_s=5)

        assert result.success is False
        assert result.error_kind == "connection"

    def test_event_introspection_returns_empty_when_events_unconfigured(
        self, mapping: ColumnMapping
    ) -> None:
        store = PostgresStore(mapping=mapping, settings=Settings())

        assert store.introspect_event_types(tenant="demo") == []


class TestProtocolSatisfaction:
    def test_fake_llm_satisfies_llm_client_contract(self) -> None:
        assert isinstance(FakeLLM(), LLMClient)

    def test_fake_store_satisfies_telemetry_store_contract(self) -> None:
        assert isinstance(FakeStore(), TelemetryStore)


class TestResultShapes:
    def test_execution_result_is_frozen(self) -> None:
        result = ExecutionResult(
            success=True,
            rows=({"avg": 61.4},),
            row_count=1,
            column_names=("avg",),
            execution_time_ms=12.5,
        )
        attribute_name = "success"

        with pytest.raises(AttributeError):
            setattr(result, attribute_name, False)

    def test_key_stat_holds_optional_timestamps(self) -> None:
        stat = KeyStat(key="speed", sample_count=10, first_seen=None, last_seen=None)

        assert stat.key == "speed"

    def test_fake_llm_generate_returns_typed_result(self) -> None:
        result = FakeLLM().generate("system", "question", temperature=0.0)

        assert isinstance(result, GenResult)
        assert result.sql == "SELECT 1"

    def test_fake_store_execute_returns_typed_result(self) -> None:
        result = FakeStore().execute("SELECT 1", row_cap=5, timeout_s=5)

        assert result.row_count == 0

    def test_fake_store_introspect_returns_stats(self) -> None:
        stats = FakeStore().introspect_keys("demo")

        assert list(stats) == []

    def test_settings_fixture_is_local_development_friendly(self, settings: Settings) -> None:
        assert settings.metadata_db_host == "localhost"
