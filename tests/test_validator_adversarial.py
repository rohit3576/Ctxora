"""Adversarial battery: every attack shape the validator MUST reject.

Born RED against the regex validator (Phase 2 of the sqlglot migration,
docs/fix/PLAN-sqlglot-migration-phases.md): 9 of 26 must-reject cases were
accepted by the regexes (R1-ch, R1-pg, R2-pg, R4-pg, R7-pg, R8-pgreadfile-pg,
R8-pgreadbin-pg, R10-pg, R12-for-share-pg) and were xfail-locked. Phase 4's
AST rewrite closed all nine; every case is now a permanent green regression.
"""

import pytest

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


def _validator(dialect: Dialect) -> SQLValidator:
    return SQLValidator(dialect=dialect, mapping=_mapping(), allowed_tables=("demo_telemetry",))


# (id, sql, dialect, expected error substring or None)
# Substrings are only asserted where the vocabulary is stable across the
# regex -> AST transition ("table not allowed"); everything else asserts
# valid is False so Phase 4 may reword errors without breaking this lock.
_MUST_REJECT = (
    # R1 — comma-join bypass: _TABLE_REF sees only the first table after FROM.
    pytest.param(
        "SELECT * FROM demo_telemetry, sql_agent_tenants WHERE 1 = 1",
        ClickHouseDialect(),
        "table not allowed",
        id="R1-clickhouse",
    ),
    pytest.param(
        "SELECT * FROM demo_telemetry, sql_agent_tenants WHERE 1 = 1",
        PostgresDialect(),
        "table not allowed",
        id="R1-postgres",
    ),
    # R2 — comment-split verb in a writable CTE: \bDELETE\b never matches.
    pytest.param(
        "WITH x AS (DEL/**/ETE FROM demo_telemetry RETURNING *) SELECT * FROM x",
        PostgresDialect(),
        None,
        id="R2-postgres",
    ),
    # R3 — stacked statements (rejected today by the DROP verb; regression).
    pytest.param(
        "SELECT 1; DROP TABLE demo_telemetry",
        ClickHouseDialect(),
        None,
        id="R3-clickhouse",
    ),
    pytest.param(
        "SELECT 1; DROP TABLE demo_telemetry",
        PostgresDialect(),
        None,
        id="R3-postgres",
    ),
    # R4 — comment-split DROP smuggled after a valid head (regex bypass).
    pytest.param(
        "SELECT * FROM demo_telemetry; /*x*/ DR/**/OP TABLE demo_telemetry",
        PostgresDialect(),
        None,
        id="R4-postgres",
    ),
    # R5 — engine admin verbs with non-SELECT heads.
    pytest.param("SHOW TABLES", ClickHouseDialect(), None, id="R5-show-clickhouse"),
    pytest.param("DESCRIBE demo_telemetry", ClickHouseDialect(), None, id="R5-describe-clickhouse"),
    pytest.param("SYSTEM FLUSH LOGS", ClickHouseDialect(), None, id="R5-system-clickhouse"),
    pytest.param("VACUUM demo_telemetry", PostgresDialect(), None, id="R5-vacuum-postgres"),
    # R6 — PRAGMA (head check) and GRANT (verb list) on both engines.
    pytest.param(
        "PRAGMA table_info(demo_telemetry)", ClickHouseDialect(), None, id="R6-pragma-clickhouse"
    ),
    pytest.param(
        "PRAGMA table_info(demo_telemetry)", PostgresDialect(), None, id="R6-pragma-postgres"
    ),
    pytest.param(
        "GRANT ALL ON demo_telemetry TO public", ClickHouseDialect(), None, id="R6-grant-clickhouse"
    ),
    pytest.param(
        "GRANT ALL ON demo_telemetry TO public", PostgresDialect(), None, id="R6-grant-postgres"
    ),
    # R7 — SELECT ... INTO: Select head passes, INTO copies the table (regex bypass).
    pytest.param(
        "SELECT * INTO evil_copy FROM demo_telemetry",
        PostgresDialect(),
        None,
        id="R7-postgres",
    ),
    # R8 — dangerous functions. FROM-position ones die as "table not allowed"
    # today (wrong reason, still rejected); SCALAR ones sail through (bypass).
    pytest.param(
        "SELECT * FROM read_csv('/etc/passwd')",
        ClickHouseDialect(),
        None,
        id="R8-readcsv-clickhouse",
    ),
    pytest.param(
        "SELECT pg_read_file('/etc/shadow')",
        PostgresDialect(),
        None,
        id="R8-pgreadfile-postgres",
    ),
    pytest.param(
        "SELECT pg_read_binary_file('/etc/shadow')",
        PostgresDialect(),
        None,
        id="R8-pgreadbinfile-postgres",
    ),
    pytest.param("SELECT * FROM s3('https://x')", ClickHouseDialect(), None, id="R8-s3-clickhouse"),
    # R9 — unparseable garbage must fail closed (head check today).
    pytest.param("NOT SQL AT ALL $$$", ClickHouseDialect(), None, id="R9-clickhouse"),
    pytest.param("NOT SQL AT ALL $$$", PostgresDialect(), None, id="R9-postgres"),
    # R10 — semicolon inside a CTE: regex blind to it (regex bypass).
    pytest.param(
        "WITH x AS (SELECT 1;) SELECT * FROM x",
        PostgresDialect(),
        None,
        id="R10-postgres",
    ),
    # R11 — qualified-name smuggling: leaf matches allowlist, qualifier doesn't.
    pytest.param(
        "SELECT * FROM other_schema.demo_telemetry",
        PostgresDialect(),
        "table not allowed",
        id="R11-postgres",
    ),
    pytest.param(
        "SELECT * FROM evil_db.demo_telemetry",
        ClickHouseDialect(),
        "table not allowed",
        id="R11-clickhouse",
    ),
    # R12 — lock clause: today ONLY the UPDATE verb regex blocks it. When the
    # verb lists are deleted (Phase 5), the lock-node deny (Phase 4) must
    # keep this rejected — regression-critical.
    pytest.param(
        "SELECT entity_id FROM demo_telemetry FOR UPDATE",
        PostgresDialect(),
        None,
        id="R12-postgres",
    ),
    # R12 twin — FOR SHARE has no verb-list cover even today.
    pytest.param(
        "SELECT entity_id FROM demo_telemetry FOR SHARE",
        PostgresDialect(),
        None,
        id="R12-for-share-postgres",
    ),
)


class TestAdversarialMustReject:
    @pytest.mark.parametrize(("sql", "dialect", "expected_substring"), _MUST_REJECT)
    def test_rejected(self, sql: str, dialect: Dialect, expected_substring: str | None) -> None:
        result = _validator(dialect).validate(sql)

        assert result.valid is False
        if expected_substring is not None:
            assert any(expected_substring in error for error in result.errors)


_MUST_PASS = (
    pytest.param("SELECT avg(value) FROM demo_telemetry", ClickHouseDialect(), id="G1-clickhouse"),
    pytest.param("SELECT avg(value) FROM demo_telemetry", PostgresDialect(), id="G1-postgres"),
    pytest.param(
        "WITH bounded AS (SELECT * FROM demo_telemetry) SELECT * FROM bounded",
        ClickHouseDialect(),
        id="G2-clickhouse",
    ),
    pytest.param(
        "WITH bounded AS (SELECT * FROM demo_telemetry) SELECT * FROM bounded",
        PostgresDialect(),
        id="G2-postgres",
    ),
    pytest.param(
        "SELECT entity_id FROM demo_telemetry UNION ALL SELECT entity_id FROM demo_telemetry",
        ClickHouseDialect(),
        id="G3-clickhouse",
    ),
    pytest.param(
        "SELECT entity_id FROM demo_telemetry UNION SELECT entity_id FROM demo_telemetry",
        PostgresDialect(),
        id="G3-postgres",
    ),
)


class TestAdversarialMustPass:
    """Over-blocking guards: legitimate SELECT shapes must stay valid."""

    @pytest.mark.parametrize(("sql", "dialect"), _MUST_PASS)
    def test_valid(self, sql: str, dialect: Dialect) -> None:
        result = _validator(dialect).validate(sql)

        assert result.valid is True
        assert result.errors == ()
