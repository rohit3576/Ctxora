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

from typing import ClassVar

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


# --- (m) S1 repair-loop transforms ---


class TestRepairTransformBehaviors:
    def test_median_node_exists_and_avg_arg_is_column(self) -> None:
        """Observed: avg(value) parses with exp.Avg whose `this` is a bare
        exp.Column (name 'value', empty table) — REQUIRED by the value-cast
        repair's trigger shape; exp.Median exists in 27.x."""
        ast = sqlglot.parse_one("SELECT avg(value) FROM t", read="clickhouse")

        assert hasattr(exp, "Median")
        agg = next(ast.find_all(exp.Avg))
        assert isinstance(agg.this, exp.Column)
        assert agg.this.name == "value"
        assert agg.this.table == ""

    def test_setting_agg_this_regenerates_with_replacement(self) -> None:
        """Observed: aggregate.set('this', parsed_cast) yields the cast inside
        the aggregate on regenerate — the value-cast transform's mechanism."""
        ast = sqlglot.parse_one("SELECT avg(value) FROM t", read="clickhouse")
        cast = sqlglot.parse_one("toFloat64OrNull(value)", read="clickhouse")

        next(ast.find_all(exp.Avg)).set("this", cast)

        assert ast.sql(dialect="clickhouse") == "SELECT avg(toFloat64OrNull(value)) FROM t"

    def test_quoted_identifier_column_keeps_bare_name(self) -> None:
        """Observed: avg("value") parses with Column.name == 'value' and empty
        table — REQUIRED: the v1 regex missed quoted identifiers; the AST
        trigger must catch them."""
        ast = sqlglot.parse_one('SELECT avg("value") FROM t', read="postgres")
        agg = next(ast.find_all(exp.Avg))

        assert agg.this.name == "value"
        assert agg.this.table == ""

    @pytest.mark.parametrize("dialect", ["clickhouse", "postgres"])
    def test_limit_set_renders_limit_n(self, dialect: str) -> None:
        """Observed: Select.set('limit', Limit(Literal.number(1000))) renders
        'LIMIT 1000' in both engine dialects — the add-limit transform."""
        ast = sqlglot.parse_one("SELECT value FROM demo_telemetry", read=dialect)
        ast.set("limit", exp.Limit(expression=exp.Literal.number(1000)))

        assert ast.sql(dialect=dialect).endswith("LIMIT 1000")

    def test_trailing_semicolon_is_single_statement(self) -> None:
        """Observed: parse('SELECT 1 FROM t;') yields exactly one statement —
        REQUIRED: strip-junk may regenerate single-statement input with a
        trailing semicolon, but must not touch multi-statement input (that
        is a hard reject, never a repair)."""
        statements = sqlglot.parse("SELECT 1 FROM t;", read="postgres")

        assert len(statements) == 1
        assert isinstance(statements[0], exp.Select)

    def test_regenerate_drops_comments(self) -> None:
        """Observed: .sql(comments=False) drops both -- and /* */ comments —
        the strip-junk transform's output contract."""
        ast = sqlglot.parse_one("SELECT 1 FROM t -- note\n/* blk */", read="postgres")

        assert "--" not in ast.sql(comments=False)
        assert "/*" not in ast.sql(comments=False)

    def test_cte_inline_via_table_replace_and_list_removal(self) -> None:
        """Observed: replacing a CTE-name Table ref with Subquery(this=body)
        and removing the CTE from with.expressions inlines it exactly — the
        inline-cte-depth transform's mechanism; with.recursive is readable
        (False here) so recursive WITH can be skipped."""
        ast = sqlglot.parse_one(
            "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT * FROM b",
            read="postgres",
        )
        with_node = ast.args["with"]
        assert with_node.recursive is False

        deepest = with_node.expressions[-1]
        body = deepest.this
        for table in list(ast.find_all(exp.Table)):
            if table.name == "b" and not table.db:
                table.replace(exp.Subquery(this=body.copy()))
        with_node.expressions.remove(deepest)

        assert ast.sql(dialect="postgres") == (
            "WITH a AS (SELECT 1 AS x) SELECT * FROM (SELECT x FROM a)"
        )


# --- (n) S2 qualify behaviors ---


class TestQualifyBehaviors:
    SCHEMA: ClassVar[dict[str, dict[str, str]]] = {
        "demo_telemetry": {
            "timestamp": "datetime",
            "device_id": "string",
            "key": "string",
            "value": "string",
        }
    }

    def test_qualify_resolves_cte_shadow_to_the_cte(self) -> None:
        """Observed: a CTE named like a real table makes the referencing
        scope's source a Scope (CTE), while base tables stay direct
        exp.Table sources — REQUIRED: this distinction is what makes the
        scope-walk allowlist exact where the name-set heuristic guessed."""
        from sqlglot.optimizer.scope import Scope, build_scope

        ast = sqlglot.parse_one(
            "WITH demo_telemetry AS (SELECT 1 AS key) SELECT key FROM demo_telemetry",
            read="postgres",
        )
        scope = build_scope(ast)
        assert scope is not None
        sources = scope.sources

        assert isinstance(sources["demo_telemetry"], Scope)
        assert not isinstance(sources["demo_telemetry"], exp.Table)

    def test_base_table_sources_are_direct_table_nodes(self) -> None:
        """Observed: a real (non-CTE) table source is the exp.Table itself,
        aliased or not, and Table.name is the table name, not the alias."""
        from sqlglot.optimizer.scope import build_scope

        ast = sqlglot.parse_one("SELECT t.key FROM demo_telemetry t", read="postgres")
        scope = build_scope(ast)
        assert scope is not None
        sources = scope.sources

        source = sources["t"]
        assert isinstance(source, exp.Table)
        assert source.name == "demo_telemetry"

    def test_unknown_table_column_raises_optimize_error(self) -> None:
        """Observed: a column read from a schema-unknown table fails qualify
        with OptimizeError 'could not be resolved' — the typed reject class
        for schema-unknown-table (indirect: via the column, not the table)."""
        from sqlglot.optimizer.qualify import qualify

        ast = sqlglot.parse_one("SELECT key FROM sql_agent_tenants", read="postgres")
        with pytest.raises(sqerr.OptimizeError, match="could not be resolved"):
            qualify(ast, dialect="postgres", schema=self.SCHEMA)

    def test_unknown_column_on_schema_table_raises_optimize_error(self) -> None:
        """Observed: a column absent from the schema'd table raises the same
        OptimizeError — unknown and ambiguous columns share one failure
        surface ('could not be resolved')."""
        from sqlglot.optimizer.qualify import qualify

        ast = sqlglot.parse_one("SELECT nonexistent FROM demo_telemetry", read="postgres")
        with pytest.raises(sqerr.OptimizeError, match="could not be resolved"):
            qualify(ast, dialect="postgres", schema=self.SCHEMA)

    def test_star_expands_against_schema_but_not_unknown_tables(self) -> None:
        """Observed: SELECT * on a schema'd table expands to its columns;
        on an unknown table qualify returns SILENTLY without expansion —
        REQUIRED pin: qualify alone is NOT a table gate; the scope-walk
        allowlist must stay the authority."""
        from sqlglot.optimizer.qualify import qualify

        expanded = qualify(
            sqlglot.parse_one("SELECT * FROM demo_telemetry", read="postgres"),
            dialect="postgres",
            schema=self.SCHEMA,
        )
        assert [col.name for col in expanded.find_all(exp.Column)] == [
            "timestamp",
            "device_id",
            "key",
            "value",
        ]

        unexpanded = qualify(
            sqlglot.parse_one("SELECT * FROM sql_agent_tenants", read="postgres"),
            dialect="postgres",
            schema=self.SCHEMA,
        )
        assert any(isinstance(node, exp.Star) for node in unexpanded.walk())

    def test_qualified_refs_to_unschema_table_pass(self) -> None:
        """Observed: a second (events-shaped) table absent from the schema
        passes qualify when its columns are explicitly qualified — REQUIRED:
        events tables must be schema'd too, or bare event columns over-block."""
        from sqlglot.optimizer.qualify import qualify

        ast = sqlglot.parse_one(
            "SELECT b.event_type FROM demo_telemetry a JOIN demo_events b"
            " ON a.device_id = b.device_id",
            read="postgres",
        )
        qualify(ast, dialect="postgres", schema=self.SCHEMA)

        bare = sqlglot.parse_one("SELECT event_type FROM demo_events", read="postgres")
        with pytest.raises(sqerr.OptimizeError):
            qualify(bare, dialect="postgres", schema=self.SCHEMA)
