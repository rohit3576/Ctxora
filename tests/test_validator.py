"""Validator tests: read-only enforcement, table scoping, EAV auto-repair."""

import pytest

from agent.validator import SQLValidator
from config.settings import ColumnMapping
from database.contracts import Dialect
from database.dialects.clickhouse import ClickHouseDialect
from database.dialects.postgres import PostgresDialect


def mapping() -> ColumnMapping:
    return ColumnMapping(
        table="demo_telemetry",
        timestamp="timestamp",
        entity_id="device_id",
        key="key",
        value="value",
    )


def validator(dialect: Dialect) -> SQLValidator:
    return SQLValidator(dialect=dialect, mapping=mapping(), allowed_tables=("demo_telemetry",))


GOOD_CH = """
SELECT device_id, avg(toFloat64OrNull(value)) AS avg_rpm
FROM demo_telemetry
WHERE key = 'engine.rpm' AND timestamp >= now() - INTERVAL 1 DAY
GROUP BY device_id
"""


class TestHardRejections:
    def test_delete_is_rejected_clickhouse(self) -> None:
        result = validator(ClickHouseDialect()).validate("DELETE FROM demo_telemetry")

        assert result.valid is False
        assert any("forbidden" in error.lower() for error in result.errors)

    def test_drop_is_rejected_postgres(self) -> None:
        result = validator(PostgresDialect()).validate("DROP TABLE demo_telemetry")

        assert result.valid is False

    def test_engine_admin_verbs_are_rejected(self) -> None:
        result = validator(ClickHouseDialect()).validate("OPTIMIZE TABLE demo_telemetry")

        assert result.valid is False

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM other_tenant_telemetry",
            "SELECT 1 FROM pg_catalog.pg_tables",
            "WITH x AS (SELECT 1 FROM secrets) SELECT * FROM x",
        ],
    )
    def test_tables_outside_allowlist_are_rejected(self, sql: str) -> None:
        result = validator(ClickHouseDialect()).validate(sql)

        assert result.valid is False
        assert any("table" in error.lower() for error in result.errors)

    def test_cte_depth_over_five_is_rejected(self) -> None:
        deep = (
            "WITH a AS (WITH b AS (WITH c AS (WITH d AS (WITH e AS (WITH f AS (SELECT 1) "
            "SELECT * FROM f) SELECT * FROM e) SELECT * FROM d) "
            "SELECT * FROM c) SELECT * FROM demo_telemetry"
        )
        sql = deep
        result = validator(ClickHouseDialect()).validate(sql)

        assert result.valid is False

    def test_non_select_statement_is_rejected(self) -> None:
        result = validator(ClickHouseDialect()).validate("EXPLAIN SELECT 1")

        assert result.valid is False


class TestAutoRepair:
    def test_bare_aggregate_over_value_gets_cast_wrapped(self) -> None:
        sql = "SELECT avg(value) FROM demo_telemetry WHERE key = 'engine.rpm' GROUP BY device_id"

        result = validator(ClickHouseDialect()).validate(sql)

        assert result.valid is True
        assert "avg(toFloat64OrNull(value))" in result.normalized_sql
        assert "value-cast" in result.repairs_applied

    def test_already_cast_sql_is_untouched(self) -> None:
        result = validator(ClickHouseDialect()).validate(GOOD_CH)

        assert result.valid is True
        assert result.repairs_applied == ()
        assert "toFloat64OrNull(value)" in result.normalized_sql

    def test_postgres_repair_uses_postgres_cast(self) -> None:
        sql = "SELECT max(value) FROM demo_telemetry WHERE key = 'speed'"

        result = validator(PostgresDialect()).validate(sql)

        assert result.valid is True
        assert "NULLIF(value, '')::double precision" in result.normalized_sql


class TestValidQueries:
    def test_simple_aggregation_passes(self) -> None:
        result = validator(ClickHouseDialect()).validate(GOOD_CH)

        assert result.valid is True
        assert result.errors == ()

    def test_cte_query_with_allowed_table_passes(self) -> None:
        sql = """
        WITH bounded AS (
            SELECT device_id, timestamp, toFloat64OrNull(value) AS v
            FROM demo_telemetry
            WHERE key = 'speed' AND timestamp >= now() - INTERVAL 1 DAY
        )
        SELECT device_id, avg(v) FROM bounded GROUP BY device_id
        """
        result = validator(ClickHouseDialect()).validate(sql)

        assert result.valid is True

    def test_repairs_are_reported_not_hidden(self) -> None:
        sql = "SELECT min(value) FROM demo_telemetry WHERE key = 'speed'"

        result = validator(ClickHouseDialect()).validate(sql)

        assert result.repairs_applied == ("value-cast",)
