"""ClickHouse dialect: every engine-specific SQL expression in one place."""

from typing import Final

from config.settings import ColumnMapping

_FORBIDDEN_VERBS: Final = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "ATTACH",
    "DETACH",
    "SYSTEM",
    "SHOW",
    "DESCRIBE",
    "RENAME",
    "OPTIMIZE",
    "GRANT",
    "REVOKE",
    "KILL",
    "FLUSH",
    "EXCHANGE",
)


class ClickHouseDialect:
    """Renders ClickHouse SQL fragments. Stateless; instantiate freely."""

    @property
    def name(self) -> str:
        """Dialect identifier used in config and logging."""
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

    def readonly_violation_patterns(self) -> tuple[str, ...]:
        """Word-boundary regexes for forbidden statements, incl. CH admin verbs."""
        return tuple(rf"\b{verb}\b" for verb in _FORBIDDEN_VERBS)

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
