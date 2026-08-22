"""Dialect tests: Timescale-aware time_bucket rendering."""

import pytest

from database.dialects.postgres import PostgresDialect


class TestTimeBucketPlain:
    def test_single_unit_uses_date_trunc_without_timescale(self) -> None:
        dialect = PostgresDialect(use_timescale=False)

        assert dialect.time_bucket("timestamp", "1 HOUR") == "date_trunc('hour', timestamp)"

    def test_multi_unit_falls_back_to_date_trunc_without_timescale(self) -> None:
        dialect = PostgresDialect(use_timescale=False)

        bucket = dialect.time_bucket("timestamp", "6 HOUR")

        assert bucket == "date_trunc('hour', timestamp)"


class TestTimeBucketTimescale:
    def test_multi_unit_uses_time_bucket_with_timescale(self) -> None:
        dialect = PostgresDialect(use_timescale=True)

        assert dialect.time_bucket("timestamp", "6 HOUR") == "time_bucket('6 hours', timestamp)"

    def test_single_unit_still_uses_date_trunc_with_timescale(self) -> None:
        dialect = PostgresDialect(use_timescale=True)

        assert dialect.time_bucket("timestamp", "1 HOUR") == "date_trunc('hour', timestamp)"


class TestDialectState:
    def test_dialect_is_frozen_configuration(self) -> None:
        dialect = PostgresDialect(use_timescale=True)
        attribute = "use_timescale"

        with pytest.raises(AttributeError):
            setattr(dialect, attribute, False)
