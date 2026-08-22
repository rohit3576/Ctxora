"""Integration tests against live compose services.

Run explicitly: QUERYPULSE_IT=1 uv run pytest tests/integration
Requires: docker compose up -d (+ --profile clickhouse) and seeded demo data
(uv run python -m demo.seed_demo).
"""

import os

import pytest

from config.settings import DEFAULT_CONFIG_PATH, ColumnMapping, Settings, load_app_config
from database.clickhouse_store import ClickHouseStore
from database.contracts import TelemetryStore
from database.metadata import check_metadata_db
from database.postgres_store import PostgresStore
from knowledge.pg import metadata_query
from knowledge.store import KnowledgeStore

pytestmark = pytest.mark.skipif(
    os.environ.get("QUERYPULSE_IT") != "1",
    reason="integration tests run only with QUERYPULSE_IT=1",
)


def _mapping() -> ColumnMapping:
    return load_app_config(DEFAULT_CONFIG_PATH).stores.telemetry.mapping


class TestMetadataDB:
    def test_readyz_roundtrip(self) -> None:
        ok, _detail = check_metadata_db(Settings())

        assert ok is True

    def test_demo_tenant_is_onboarded(self) -> None:
        store = KnowledgeStore(query=metadata_query(Settings()))

        knowledge = store.load("demo")

        assert {entry.canonical_key for entry in knowledge.keys} >= {"speed", "engine.rpm"}


class TestPostgresStore:
    def test_execute_reads_seeded_rows(self) -> None:
        store = PostgresStore(mapping=_mapping(), settings=Settings(telemetry_db_port=5432))

        result = store.execute(
            "SELECT count(*) AS c FROM demo_telemetry WHERE key = 'speed'", row_cap=10, timeout_s=10
        )

        assert result.success is True
        assert result.rows[0]["c"]


class TestClickHouseStore:
    def test_execute_reads_seeded_rows(self) -> None:
        pytest.importorskip("clickhouse_connect")
        store = ClickHouseStore(mapping=_mapping(), settings=Settings())

        result = store.execute(
            "SELECT count() AS c FROM demo_telemetry WHERE key = 'speed'", row_cap=10, timeout_s=10
        )

        assert result.success is True
        assert result.rows[0]["c"]

    def test_stores_satisfy_contract(self) -> None:
        assert isinstance(PostgresStore(mapping=_mapping(), settings=Settings()), TelemetryStore)
        assert isinstance(ClickHouseStore(mapping=_mapping(), settings=Settings()), TelemetryStore)
