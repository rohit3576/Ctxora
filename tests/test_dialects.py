"""Golden-string tests for dialect rendering, locked to the portability matrix.

The matrix lives in docs/architecture/ARCHITECTURE.md section 7. Changing a
golden string here without changing that matrix is a contract violation.
"""

import pytest

from config.settings import DEFAULT_CONFIG_PATH, ColumnMapping, load_app_config
from database.dialects.clickhouse import ClickHouseDialect
from database.dialects.postgres import PostgresDialect


@pytest.fixture
def mapping() -> ColumnMapping:
    return load_app_config(DEFAULT_CONFIG_PATH).stores.telemetry.mapping


class TestClickHouseDialect:
    def test_cast_numeric(self) -> None:
        assert ClickHouseDialect().cast_numeric("value") == "toFloat64OrNull(value)"

    def test_latest_value_expr(self) -> None:
        expr = ClickHouseDialect().latest_value_expr("toFloat64OrNull(value)", "timestamp")
        assert expr == "argMax(toFloat64OrNull(value), timestamp)"

    def test_json_field_float(self) -> None:
        assert (
            ClickHouseDialect().json_field_float("event_data", "lat")
            == "JSONExtractFloat(event_data, 'lat')"
        )

    def test_time_bucket_single_unit(self) -> None:
        assert (
            ClickHouseDialect().time_bucket("timestamp", "1 HOUR")
            == "toStartOfInterval(timestamp, INTERVAL 1 HOUR)"
        )

    def test_time_bucket_multi_unit(self) -> None:
        assert (
            ClickHouseDialect().time_bucket("timestamp", "6 HOUR")
            == "toStartOfInterval(timestamp, INTERVAL 6 HOUR)"
        )

    def test_now_minus(self) -> None:
        assert ClickHouseDialect().now_minus("DAY", 1) == "now() - INTERVAL 1 DAY"

    def test_quote_ident_uses_backticks(self) -> None:
        assert ClickHouseDialect().quote_ident("my_table") == "`my_table`"

    def test_readonly_patterns_block_engine_admin_verbs(self) -> None:
        patterns = " ".join(ClickHouseDialect().readonly_violation_patterns()).upper()

        for verb in ("DROP", "OPTIMIZE", "KILL", "SYSTEM"):
            assert verb in patterns

    def test_eav_rules_use_mapped_columns(self, mapping: ColumnMapping) -> None:
        rules = ClickHouseDialect().eav_system_rules(mapping)

        assert "toFloat64OrNull(value)" in rules
        assert "argMax" in rules


class TestPostgresDialect:
    def test_cast_numeric(self) -> None:
        assert PostgresDialect().cast_numeric("value") == "NULLIF(value, '')::double precision"

    def test_latest_value_expr(self) -> None:
        assert (
            PostgresDialect().latest_value_expr("value", "timestamp")
            == "(array_agg(value ORDER BY timestamp DESC))[1]"
        )

    def test_json_field_float(self) -> None:
        assert (
            PostgresDialect().json_field_float("event_data", "lat")
            == "(event_data::jsonb ->> 'lat')::double precision"
        )

    def test_time_bucket_single_unit_uses_date_trunc(self) -> None:
        assert (
            PostgresDialect().time_bucket("timestamp", "1 HOUR") == "date_trunc('hour', timestamp)"
        )

    def test_time_bucket_multi_unit_uses_timescale(self) -> None:
        timescale = PostgresDialect(use_timescale=True)

        assert timescale.time_bucket("timestamp", "6 HOUR") == "time_bucket('6 hours', timestamp)"

    def test_now_minus_quotes_interval(self) -> None:
        assert PostgresDialect().now_minus("DAY", 2) == "now() - INTERVAL '2 days'"

    def test_quote_ident_uses_double_quotes(self) -> None:
        assert PostgresDialect().quote_ident("my_table") == '"my_table"'

    def test_readonly_patterns_block_engine_admin_verbs(self) -> None:
        patterns = " ".join(PostgresDialect().readonly_violation_patterns()).upper()

        for verb in ("DROP", "VACUUM", "COPY", "REINDEX"):
            assert verb in patterns

    def test_eav_rules_use_mapped_columns(self, mapping: ColumnMapping) -> None:
        rules = PostgresDialect().eav_system_rules(mapping)

        assert "NULLIF(value, '')::double precision" in rules
        assert "array_agg" in rules


class TestDialectParity:
    def test_both_dialects_declare_expected_names(self) -> None:
        assert ClickHouseDialect().name == "clickhouse"
        assert PostgresDialect().name == "postgres"
