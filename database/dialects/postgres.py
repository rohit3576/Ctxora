"""PostgreSQL dialect: plain KV tables and TimescaleDB hypertables.

The dialect instance carries one piece of state: whether the TimescaleDB
extension is available (probed once by the store). Single-unit buckets use
date_trunc either way; multi-unit buckets use time_bucket only on Timescale.
"""

from dataclasses import dataclass
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
    "GRANT",
    "REVOKE",
    "VACUUM",
    "ANALYZE",
    "REINDEX",
    "CLUSTER",
    "CALL",
    "DO",
    "COPY",
    "LISTEN",
    "NOTIFY",
)

_INTERVAL_TOKEN_COUNT: Final = 2


@dataclass(frozen=True, slots=True)
class PostgresDialect:
    """Renders PostgreSQL SQL fragments; Timescale-aware time buckets."""

    use_timescale: bool = False

    @property
    def name(self) -> str:
        """Dialect identifier used in config and logging."""
        return "postgres"

    @property
    def sqlglot_name(self) -> str:
        """Return the sqlglot dialect identifier for AST parsing."""
        return "postgres"

    def cast_numeric(self, value_expr: str) -> str:
        """Null-safe numeric cast: NULLIF(expr, '')::double precision."""
        return f"NULLIF({value_expr}, '')::double precision"

    def latest_value_expr(self, value_expr: str, ts_col: str) -> str:
        """Latest reading in a group: (array_agg(v ORDER BY ts DESC))[1]."""
        return f"(array_agg({value_expr} ORDER BY {ts_col} DESC))[1]"

    def json_field_float(self, json_col: str, field: str) -> str:
        """JSON payload field as float: (col::jsonb ->> 'field')::double precision."""
        return f"({json_col}::jsonb ->> '{field}')::double precision"

    def time_bucket(self, ts_col: str, interval: str) -> str:
        """Single-unit buckets via date_trunc; multi-unit via Timescale when available."""
        n, unit = _parse_interval(interval)
        if n == 1:
            return f"date_trunc('{unit}', {ts_col})"
        if self.use_timescale:
            return f"time_bucket('{n} {unit}s', {ts_col})"
        return f"date_trunc('{unit}', {ts_col})"

    def now_minus(self, unit: str, n: int) -> str:
        """Now minus n units: now() - INTERVAL 'n units'."""
        plural = unit.lower() if n == 1 else f"{unit.lower()}s"
        return f"now() - INTERVAL '{n} {plural}'"

    def quote_ident(self, name: str) -> str:
        """Quote an identifier with double quotes."""
        return f'"{name}"'

    def readonly_violation_patterns(self) -> tuple[str, ...]:
        """Word-boundary regexes for forbidden statements, incl. PG admin verbs."""
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


def _parse_interval(interval: str) -> tuple[int, str]:
    """'6 HOUR' -> (6, 'hour'); '1 DAY' -> (1, 'day')."""
    parts = interval.split()
    if len(parts) != _INTERVAL_TOKEN_COUNT or not parts[0].isdigit():
        msg = f"interval must look like '1 HOUR', got {interval!r}"
        raise ValueError(msg)
    return int(parts[0]), parts[1].lower().rstrip("s")
