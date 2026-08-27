"""S1 AST repair loop: taxonomy coverage, bounds, revert, fuzz invariant.

Provable coverage: every taxonomy class has an input that the v1 regex
path leaves unrepaired (or rejects) and that v2 repairs into a valid
statement. Fail-closed: a transform emitting unparseable SQL reverts to
the last good state and the validator rejects — never executes.
"""

import random

import pytest
import sqlglot
from sqlglot import exp

from agent.repairs import REPAIR_LIMIT
from agent.validator import SQLValidator
from config.settings import ColumnMapping
from database.contracts import Dialect
from database.dialects.clickhouse import ClickHouseDialect
from database.dialects.postgres import PostgresDialect


def _mapping() -> ColumnMapping:
    return ColumnMapping(
        table="demo_telemetry",
        timestamp="timestamp",
        entity_id="device_id",
        key="key",
        value="value",
    )


def _validator(dialect: Dialect, repair_v2: bool = True, passes: int = 3) -> SQLValidator:
    return SQLValidator(
        dialect=dialect,
        mapping=_mapping(),
        allowed_tables=("demo_telemetry",),
        repair_v2=repair_v2,
        repair_passes=passes,
    )


def _chained_ctes(count: int) -> str:
    parts = ["WITH c1 AS (SELECT key FROM demo_telemetry)"]
    parts += [f"c{i} AS (SELECT key FROM c{i - 1})" for i in range(2, count + 1)]
    return ", ".join(parts) + f" SELECT key FROM c{count}"


class TestPerClassCoverage:
    def test_value_cast_ast_catches_quoted_identifier_regex_missed(self) -> None:
        sql = 'SELECT avg("value") FROM demo_telemetry'

        v1 = _validator(PostgresDialect(), repair_v2=False).validate(sql)
        assert v1.valid
        assert "::double precision" not in v1.normalized_sql

        v2 = _validator(PostgresDialect()).validate(sql)
        assert v2.valid
        assert "value-cast" in v2.repairs_applied
        assert "double precision" in v2.normalized_sql.lower()

    def test_value_cast_matches_v1_semantically_on_plain_shapes(self) -> None:
        for dialect in (ClickHouseDialect(), PostgresDialect()):
            sql = "SELECT avg(value) FROM demo_telemetry"

            v1 = _validator(dialect, repair_v2=False).validate(sql)
            v2 = _validator(dialect).validate(sql)

            assert v1.valid and v2.valid
            assert "value-cast" in v2.repairs_applied
            v2_body = v2.normalized_sql.lower().removesuffix(f" limit {REPAIR_LIMIT}")
            canonical_v1 = sqlglot.parse_one(v1.normalized_sql, read=dialect.sqlglot_name).sql()
            canonical_v2 = sqlglot.parse_one(v2_body, read=dialect.sqlglot_name).sql()
            assert canonical_v1 == canonical_v2

    def test_add_limit_bounds_unbounded_select(self) -> None:
        sql = "SELECT value FROM demo_telemetry"

        v1 = _validator(ClickHouseDialect(), repair_v2=False).validate(sql)
        v2 = _validator(ClickHouseDialect()).validate(sql)

        assert v1.valid and "LIMIT" not in v1.normalized_sql.upper()
        assert v2.valid
        assert "add-limit" in v2.repairs_applied
        assert f"LIMIT {REPAIR_LIMIT}" in v2.normalized_sql.upper()

    def test_add_limit_leaves_other_tables_alone(self) -> None:
        sql = "SELECT 1"

        result = _validator(ClickHouseDialect()).validate(sql)

        assert result.valid
        assert "add-limit" not in result.repairs_applied

    def test_strip_junk_removes_comments_and_trailing_semicolon(self) -> None:
        sql = "SELECT key FROM demo_telemetry -- trailing note\n;"

        v1 = _validator(PostgresDialect(), repair_v2=False).validate(sql)
        v2 = _validator(PostgresDialect()).validate(sql)

        assert v1.valid and "--" in v1.normalized_sql
        assert v2.valid
        assert "strip-junk" in v2.repairs_applied
        assert "--" not in v2.normalized_sql
        assert not v2.normalized_sql.rstrip().endswith(";")

    def test_strip_junk_never_repairs_multi_statement(self) -> None:
        sql = "SELECT 1; DROP TABLE demo_telemetry"

        v1 = _validator(PostgresDialect(), repair_v2=False).validate(sql)
        v2 = _validator(PostgresDialect()).validate(sql)

        assert not v1.valid and not v2.valid
        assert v2.repairs_applied == ()

    def test_inline_cte_depth_repairs_over_deep_chain(self) -> None:
        sql = _chained_ctes(8)

        v1 = _validator(PostgresDialect(), repair_v2=False).validate(sql)
        assert not v1.valid

        v2 = _validator(PostgresDialect()).validate(sql)
        assert v2.valid
        assert v2.repairs_applied.count("inline-cte-depth") == 3

    def test_inline_cte_depth_bound_is_fail_closed(self) -> None:
        sql = _chained_ctes(12)

        result = _validator(PostgresDialect(), passes=3).validate(sql)

        assert not result.valid
        assert result.repairs_applied.count("inline-cte-depth") == 3


class TestLoopBehavior:
    def test_repairs_accumulate_across_classes(self) -> None:
        sql = "SELECT avg(value) FROM demo_telemetry -- note\n;"

        result = _validator(PostgresDialect()).validate(sql)

        assert result.valid
        assert "strip-junk" in result.repairs_applied
        assert "value-cast" in result.repairs_applied
        assert "add-limit" in result.repairs_applied

    def test_repair_passes_bound_respected(self) -> None:
        result = _validator(PostgresDialect(), passes=1).validate(_chained_ctes(8))

        assert not result.valid
        assert len(result.repairs_applied) == 1

    def test_clean_input_short_circuits_with_no_repairs(self) -> None:
        sql = "SELECT key FROM demo_telemetry LIMIT 10"

        result = _validator(PostgresDialect()).validate(sql)

        assert result.valid
        assert result.repairs_applied == ()

    def test_broken_transform_reverts_and_never_executes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent import repairs

        def _broken(sql: str, dialect: Dialect, mapping: ColumnMapping) -> str | None:
            return "SELECT ("

        monkeypatch.setitem(repairs.REPAIR_TRANSFORMS, "value-cast", _broken)
        validator = _validator(ClickHouseDialect())
        sql = "SELECT avg(value) FROM demo_telemetry"

        result = validator.validate(sql)

        assert result.normalized_sql == sql
        assert "value-cast" not in result.repairs_applied
        assert "toFloat64OrNull" not in result.normalized_sql

    def test_broken_transform_on_invalid_input_still_rejects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent import repairs

        def _broken(sql: str, dialect: Dialect, mapping: ColumnMapping) -> str | None:
            return "SELECT ("

        monkeypatch.setitem(repairs.REPAIR_TRANSFORMS, "inline-cte-depth", _broken)
        validator = _validator(PostgresDialect(), passes=3)
        sql = _chained_ctes(8)

        result = validator.validate(sql)

        assert not result.valid
        assert "CTE depth" in " ".join(result.errors)


class TestFuzzInvariant:
    GOLDEN_SQL: tuple[str, ...] = (
        "SELECT key, avg(toFloat64OrNull(value)) FROM demo_telemetry GROUP BY key",
        "SELECT device_id, argMax(value, timestamp) FROM demo_telemetry GROUP BY device_id",
        "SELECT key FROM demo_telemetry WHERE timestamp >= now() - INTERVAL 1 DAY LIMIT 100",
        "WITH recent AS (SELECT key, value FROM demo_telemetry) SELECT * FROM recent LIMIT 10",
        "SELECT count() FROM demo_telemetry",
        "SELECT avg(NULLIF(value, '')::double precision) FROM demo_telemetry",
    )

    def _mutations(self, sql: str) -> list[str]:
        mutants = [sql + ";", sql + " -- comment", "  " + sql + "\t"]
        words = sql.split()
        if len(words) > 4:
            mutants.append(" ".join([*words[:2], "WHERE", *words[2:]]))
            mutants.append(sql.replace("SELECT", "SELECTT", 1))
            mutants.append(sql.replace("FROM", ",, FROM", 1))
        return mutants

    @pytest.mark.parametrize("dialect", [ClickHouseDialect(), PostgresDialect()])
    def test_repaired_output_validates_or_reverts(self, dialect: Dialect) -> None:
        rng = random.Random(20260827)  # noqa: S311 (fixture sampling, not crypto)
        for sql in self.GOLDEN_SQL:
            for mutant in self._mutations(sql):
                if rng.random() < 0.3:
                    continue
                result = _validator(dialect).validate(mutant)
                if result.valid:
                    parsed = sqlglot.parse(result.normalized_sql, read=dialect.sqlglot_name)
                    assert len(parsed) == 1
                    root = parsed[0]
                    assert isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except))
                    assert "SELECTT" not in result.normalized_sql
                else:
                    assert result.errors
