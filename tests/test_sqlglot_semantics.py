"""sqlglot semantics locks: permanent pins on every AST behavior the validator
rewrite (Phase 4) and the flywheel normalize (Phase 6) depend on.

Every assertion below encodes behavior OBSERVED EMPIRICALLY against
sqlglot==27.29.0 (probes recorded in .omo/evidence/task-2-sqlglot-
implementation.txt, 2026-08-24) — one observed-behavior comment per test.
If a sqlglot upgrade changes any pinned behavior, these tests fail loudly
and the migration plan's pre-specced resolutions decide the response;
they are never edited casually.

Load-bearing finding for Phase 4: unparseable input raises TWO exception
types (TokenError at tokenize, ParseError at parse) and TokenError is NOT
a ParseError subclass — the validator's parse gate must catch both.
"""

import pytest
import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp

# --- (a) multi-statement parse length ---


class TestMultiStatement:
    def test_semicolon_separated_input_yields_two_statements(self) -> None:
        """Observed: parse('SELECT 1; DROP TABLE t') -> [Select, Drop], len 2, both dialects."""
        for dialect in ("postgres", "clickhouse"):
            statements = sqlglot.parse("SELECT 1; DROP TABLE t", dialect=dialect)

            assert len(statements) == 2
            assert isinstance(statements[0], exp.Select)
            assert isinstance(statements[1], exp.Drop)


# --- (b) unparseable input: exception types ---


class TestUnparseableInput:
    def test_tokenizer_garbage_raises_token_error(self) -> None:
        """Observed: '$$$' input dies in the tokenizer -> TokenError ('Error tokenizing')."""
        with pytest.raises(sqerr.TokenError):
            _ = sqlglot.parse_one("GARBAGE $$$ NOT SQL", read="postgres")

    def test_parser_garbage_raises_parse_error(self) -> None:
        """Observed: plain garbage without tokenizer traps -> ParseError ('Unexpected token')."""
        with pytest.raises(sqerr.ParseError):
            _ = sqlglot.parse_one("NOT SQL AT ALL", read="postgres")

    def test_token_error_is_not_a_parse_error_subclass(self) -> None:
        """Observed: TokenError and ParseError are SIBLINGS under SqlglotError —
        a bare `except ParseError` misses tokenizer failures (battery R9 input
        'NOT SQL AT ALL $$$' raises TokenError). Phase 4 catches BOTH."""
        assert issubclass(sqerr.TokenError, sqerr.SqlglotError)
        assert issubclass(sqerr.ParseError, sqerr.SqlglotError)
        assert not issubclass(sqerr.TokenError, sqerr.ParseError)


# --- (c) non-SQL head fallbacks (ClickHouse) ---


class TestCommandFallback:
    def test_kill_parses_to_kill_node_outside_select_allowlist(self) -> None:
        """Observed: 'KILL QUERY 1' (clickhouse) -> exp.Kill root, NOT exp.Command;
        still outside the {Select, Union, Intersect, Except} allowlist."""
        root = sqlglot.parse_one("KILL QUERY 1", read="clickhouse")

        assert isinstance(root, exp.Kill)
        assert not isinstance(root, exp.Select | exp.Union | exp.Intersect | exp.Except)

    def test_system_flush_parses_to_command(self) -> None:
        """Observed: 'SYSTEM FLUSH LOGS' (clickhouse) -> exp.Command root
        (sqlglot logs an unsupported-syntax note to stderr)."""
        root = sqlglot.parse_one("SYSTEM FLUSH LOGS", read="clickhouse")

        assert isinstance(root, exp.Command)


# --- (d) CTE aliases visible in find_all(Table) ---


class TestCteAliasVisibility:
    def test_cte_alias_appears_as_table_reference(self) -> None:
        """Observed: WITH x AS (SELECT * FROM t) SELECT * FROM x -> find_all(Table)
        yields BOTH 'x' and 't' — the validator MUST subtract CTE aliases."""
        tables = [
            t.name
            for t in sqlglot.parse_one(
                "WITH x AS (SELECT * FROM t) SELECT * FROM x", read="postgres"
            ).find_all(exp.Table)
        ]

        assert sorted(tables) == ["t", "x"]


# --- (e) set-operation root classes ---


class TestSetOpRoots:
    @pytest.mark.parametrize(
        ("sql", "dialect", "expected_root"),
        [
            ("SELECT 1 UNION ALL SELECT 2", "clickhouse", exp.Union),
            ("SELECT 1 UNION SELECT 2", "postgres", exp.Union),
            ("SELECT 1 INTERSECT SELECT 2", "postgres", exp.Intersect),
            ("SELECT 1 EXCEPT SELECT 2", "postgres", exp.Except),
        ],
    )
    def test_set_op_roots_are_exactly_the_allowlisted_classes(
        self, sql: str, dialect: str, expected_root: type[exp.Expression]
    ) -> None:
        """Observed: UNION/UNION ALL -> exp.Union, INTERSECT -> exp.Intersect,
        EXCEPT -> exp.Except — exactly the Phase-4 root allowlist."""
        root = sqlglot.parse_one(sql, read=dialect)

        assert type(root) is expected_root


# --- (f) comment-split verb ---


class TestCommentSplitVerb:
    def test_comment_split_delete_raises_parse_error(self) -> None:
        """Observed: WITH x AS (DEL/**/ETE FROM t RETURNING *) ... -> ParseError
        ('Expecting )') — a reject path via the parse gate, no Delete node."""
        with pytest.raises(sqerr.ParseError):
            _ = sqlglot.parse_one(
                "WITH x AS (DEL/**/ETE FROM demo_telemetry RETURNING *) SELECT * FROM x",
                read="postgres",
            )


# --- (g) .sql(comments=False) kwarg ---


class TestCommentsKwarg:
    def test_sql_comments_false_strips_comments(self) -> None:
        """Observed: parse_one('SELECT 1 -- c').sql(comments=False) -> 'SELECT 1'
        — kwarg supported; Phase 6 uses it for canonical dedupe."""
        assert sqlglot.parse_one("SELECT 1 -- c").sql(comments=False) == "SELECT 1"


# --- (h) table-function node shape ---


class TestTableFunctionShape:
    def test_read_csv_in_from_is_table_with_non_identifier_this(self) -> None:
        """Observed: SELECT * FROM read_csv('/etc/passwd') (clickhouse) ->
        exp.Table with this=ReadCSV node (NOT an Identifier), name='', db=''
        — caught by the structural prong: FROM/JOIN targets must be plain
        identifier tables."""
        tables = list(
            sqlglot.parse_one("SELECT * FROM read_csv('/etc/passwd')", read="clickhouse").find_all(
                exp.Table
            )
        )

        assert len(tables) == 1
        table = tables[0]
        assert isinstance(table, exp.Table)
        assert not isinstance(table.this, exp.Identifier)
        assert table.name == ""


# --- (i) scalar dangerous functions ---


class TestScalarDangerousFunctions:
    @pytest.mark.parametrize(
        "sql",
        ["SELECT pg_read_file('/x')", "SELECT pg_catalog.pg_read_file('/x')"],
    )
    def test_plain_and_qualified_forms_parse_as_anonymous_with_bare_name(self, sql: str) -> None:
        """Observed: both plain and pg_catalog-qualified pg_read_file parse as
        exp.Anonymous with name='pg_read_file' (qualifier dropped from name)
        — the named denylist hits both forms; last-segment matching is a no-op
        safety net."""
        anonymous = [
            a.name for a in sqlglot.parse_one(sql, read="postgres").find_all(exp.Anonymous)
        ]

        assert anonymous == ["pg_read_file"]


# --- (j) generic-dialect roundtrip determinism (ClickHouse idioms) ---


class TestGenericDialectDeterminism:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry",
            "SELECT argMax(value, timestamp) FROM demo_telemetry",
            "SELECT toStartOfInterval(timestamp, INTERVAL 1 HOUR) FROM demo_telemetry",
            "SELECT NULLIF(value, '')::double precision FROM demo_telemetry",
        ],
    )
    def test_roundtrip_is_deterministic(self, sql: str) -> None:
        """Observed: every CH idiom re-renders in the generic dialect (e.g.
        argMax -> ARG_MAX, ::double precision -> CAST(... AS DOUBLE), INTERVAL
        '1' HOUR) but deterministically — same input, same output. Dedupe needs
        determinism only, not fidelity (pre-specced resolution j)."""
        first = sqlglot.parse_one(sql).sql()
        second = sqlglot.parse_one(sql).sql()

        assert first == second
        assert "demo_telemetry" in first.lower()

    def test_re_rendering_changes_the_text(self) -> None:
        """Observed: generic-dialect output differs from the ClickHouse input
        (argMax(value, timestamp) -> ARG_MAX(value, timestamp)) — documents that
        canonical form is a re-rendering, never re-executed (execution path
        stays byte-identical)."""
        sql = "SELECT argMax(value, timestamp) FROM demo_telemetry"

        assert sqlglot.parse_one(sql).sql() != sql


# --- (k) qualified table references ---


class TestQualifiedTables:
    def test_qualifier_sits_in_db_attribute(self) -> None:
        """Observed: SELECT * FROM other_schema.demo_telemetry (postgres) ->
        t.name='demo_telemetry', t.db='other_schema', t.catalog='' — Phase 4
        rejects any Table with non-empty db or catalog."""
        tables = list(
            sqlglot.parse_one(
                "SELECT * FROM other_schema.demo_telemetry", read="postgres"
            ).find_all(exp.Table)
        )

        assert len(tables) == 1
        assert tables[0].name == "demo_telemetry"
        assert tables[0].db == "other_schema"
        assert tables[0].catalog == ""


# --- (l) lock clauses ---


class TestLockClauses:
    @pytest.mark.parametrize(
        "sql",
        ["SELECT * FROM demo_telemetry FOR UPDATE", "SELECT * FROM demo_telemetry FOR SHARE"],
    )
    def test_lock_clause_exposes_exp_lock_node(self, sql: str) -> None:
        """Observed: both FOR UPDATE and FOR SHARE parse with an exp.Lock node
        under a Select root — REQUIRED deny class for Phase 4 (verb-list
        removal would otherwise reopen FOR UPDATE)."""
        root = sqlglot.parse_one(sql, read="postgres")

        assert isinstance(root, exp.Select)
        assert any(isinstance(node, exp.Lock) for node in root.find_all(exp.Lock))
