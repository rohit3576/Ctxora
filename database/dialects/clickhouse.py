"""ClickHouse dialect: every engine-specific SQL expression in one place."""

import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp

from config.settings import ColumnMapping


class ClickHouseDialect:
    """Renders ClickHouse SQL fragments. Stateless; instantiate freely."""

    @property
    def name(self) -> str:
        """Dialect identifier used in config and logging."""
        return "clickhouse"

    @property
    def sqlglot_name(self) -> str:
        """Return the sqlglot dialect identifier for AST parsing."""
        return "clickhouse"

    def cast_numeric(self, value_expr: str) -> str:
        """Null-safe numeric cast: toFloat64OrNull(expr)."""
        return f"toFloat64OrNull({value_expr})"

    def latest_value_expr(self, value_expr: str, ts_col: str) -> str:
        """Latest reading in a group: argMax(value, ts)."""
        return f"argMax({value_expr}, {ts_col})"

    def json_field_float(self, json_col: str, field: str) -> str:
        """JSON payload field as float: JSONExtractFloat(col, 'field')."""
        return f"JSONExtractFloat({json_col}, '{field}')"

    def time_bucket(self, ts_col: str, interval: str) -> str:
        """Time bucket: toStartOfInterval(ts, INTERVAL n UNIT)."""
        return f"toStartOfInterval({ts_col}, INTERVAL {interval})"

    def now_minus(self, unit: str, n: int) -> str:
        """Now minus n units: now() - INTERVAL n UNIT."""
        return f"now() - INTERVAL {n} {unit}"

    def quote_ident(self, name: str) -> str:
        """Quote an identifier with backticks."""
        return f"`{name}`"

    def postfix_canonical(self, sql: str) -> str:
        """Rewrite the canonical shapes a pg->CH transpile would break.

        NULLIF(x, '')::double transpiles to a plain CAST (0 for garbage
        strings, not NULL) -> toFloat64OrNull(x); the ordered array_agg
        subscript has no ClickHouse equivalent and loses its ORDER BY in
        the CH parse -> argMax(x, ts). Runs on the postgres-read tree
        where both shapes keep full fidelity.
        """
        try:
            ast = sqlglot.parse_one(sql, read="postgres")
        except (sqerr.ParseError, sqerr.TokenError):
            return sql
        fired = False
        for node in list(ast.find_all(exp.Cast)):
            inner = node.this
            if isinstance(inner, exp.Nullif) and node.to.this in (
                exp.DataType.Type.DOUBLE,
                exp.DataType.Type.FLOAT,
            ):
                node.replace(exp.Anonymous(this="toFloat64OrNull", expressions=[inner.this]))
                fired = True
        for node in list(ast.find_all(exp.Bracket)):
            aggregate = node.this.this if isinstance(node.this, exp.Paren) else node.this
            if not isinstance(aggregate, (exp.ArrayAgg, exp.AggFunc)):
                continue
            order = aggregate.this if isinstance(aggregate.this, exp.Order) else None
            if order is None:
                continue
            column = order.this if isinstance(order.this, exp.Column) else None
            ordered = [item.this for item in order.expressions if isinstance(item, exp.Ordered)]
            if column is not None and len(ordered) == 1:
                node.replace(
                    exp.Anonymous(this="argMax", expressions=[column.copy(), ordered[0].copy()])
                )
                fired = True
        return ast.sql(dialect="clickhouse") if fired else sql

    def eav_system_rules(self, mapping: ColumnMapping) -> str:
        """Render the dialect-specific EAV rules block for the system prompt."""
        cast = self.cast_numeric(mapping.value)
        latest = self.latest_value_expr(cast, mapping.timestamp)
        return (
            f"The telemetry table stores key-value observations; each row is one "
            f"({mapping.timestamp}, {mapping.entity_id}, {mapping.key}, {mapping.value}) reading.\n"
            f"- Filter metrics with {mapping.key} = '<metric_key>'\n"
            f"- ALL numeric math on {mapping.value} must use {cast}\n"
            f"- Latest value per {mapping.entity_id}: {latest}\n"
            f"- Bound every multi-metric CTE with a {mapping.timestamp} filter "
            f"(e.g. {mapping.timestamp} >= {self.now_minus('DAY', 30)})"
        )
