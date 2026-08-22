"""PostgreSQL store adapter: read-only execution with statement timeout."""

import datetime
import time

import psycopg

from config.settings import ColumnMapping, Settings
from database.contracts import (
    Dialect,
    EventTypeStat,
    ExecutionResult,
    KeyStat,
)
from database.dialects.postgres import PostgresDialect
from database.rows import to_json_scalar


class PostgresStore:
    """PostgreSQL adapter (plain KV tables and TimescaleDB hypertables)."""

    def __init__(
        self,
        mapping: ColumnMapping,
        settings: Settings,
        events_table_template: str | None = None,
        timescale: bool | None = None,
    ) -> None:
        """Hold mapping, credentials, events template, and Timescale availability.

        ``timescale=None`` means "probe on first use"; pass a bool to pin it
        (tests) or to skip probing when the extension is known absent/present.
        """
        self.mapping: ColumnMapping = mapping
        self.settings: Settings = settings
        self.events_table_template: str | None = events_table_template
        self._timescale: bool | None = timescale
        self._dialect: Dialect = PostgresDialect()

    @property
    def dialect(self) -> Dialect:
        """The (possibly Timescale-aware) PostgreSQL dialect."""
        if self._timescale is None:
            self._timescale = self._probe_timescale()
        if self._timescale != getattr(self._dialect, "use_timescale", False):
            self._dialect = PostgresDialect(use_timescale=self._timescale)
        return self._dialect

    def _probe_timescale(self) -> bool:
        """Check once whether the TimescaleDB extension is installed."""
        result = self.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescale'",
            row_cap=1,
            timeout_s=10,
        )
        return result.success and result.row_count > 0

    def _conninfo(self, timeout_s: int) -> str:
        user = self.settings.telemetry_db_user or self.settings.metadata_db_user
        password = self.settings.telemetry_db_password or self.settings.metadata_db_password
        host = self.settings.telemetry_db_host or self.settings.metadata_db_host
        return (
            f"host={host} port={self.settings.telemetry_db_port} "
            f"dbname={self.settings.telemetry_db_name} user={user} "
            f"password={password} connect_timeout=5 "
            f"options='-c default_transaction_read_only=on -c statement_timeout={timeout_s * 1000}'"
        )

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        """Run one read-only query capped at row_cap rows."""
        started = time.perf_counter()
        try:
            with (
                psycopg.connect(self._conninfo(timeout_s)) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(sql.encode("utf-8"))
                tuples = cur.fetchmany(row_cap)
                columns = tuple(desc.name for desc in cur.description or ())
        except psycopg.OperationalError as exc:
            message = str(exc).splitlines()[0] if str(exc) else "connection failed"
            lowered = message.lower()
            kind = "query" if "statement" in lowered or "permission" in lowered else "connection"
            return ExecutionResult(
                success=False,
                rows=(),
                row_count=0,
                column_names=(),
                execution_time_ms=0.0,
                error_kind=kind,
                error=message,
            )
        except psycopg.ProgrammingError as exc:
            message = str(exc).splitlines()[0] if str(exc) else "query failed"
            return ExecutionResult(
                success=False,
                rows=(),
                row_count=0,
                column_names=(),
                execution_time_ms=0.0,
                error_kind="query",
                error=message,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        rows = tuple(
            {name: to_json_scalar(value) for name, value in zip(columns, row, strict=False)}
            for row in tuples
        )
        return ExecutionResult(
            success=True,
            rows=rows,
            row_count=len(rows),
            column_names=columns,
            execution_time_ms=elapsed_ms,
        )

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        """Distinct keys with counts and time bounds."""
        table = self.dialect.quote_ident(self.mapping.table.format(tenant=tenant))
        result = self.execute(
            f"SELECT {self.mapping.key} AS k, count(*) AS c, "
            f"min({self.mapping.timestamp}) AS first_seen, "
            f"max({self.mapping.timestamp}) AS last_seen "
            f"FROM {table} GROUP BY k ORDER BY k",
            row_cap=10_000,
            timeout_s=60,
        )
        if not result.success:
            return []

        def parse_ts(value: object) -> datetime.datetime | None:
            if isinstance(value, str):
                return datetime.datetime.fromisoformat(value)
            return None

        return [
            KeyStat(
                key=str(row["k"]),
                sample_count=int(row["c"] or 0),
                first_seen=parse_ts(row["first_seen"]),
                last_seen=parse_ts(row["last_seen"]),
            )
            for row in result.rows
        ]

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        """Distinct event types with counts ([] when events are not configured)."""
        if self.events_table_template is None:
            return []
        table = self.dialect.quote_ident(self.events_table_template.format(tenant=tenant))
        result = self.execute(
            f"SELECT event_type AS e, count(*) AS c FROM {table} GROUP BY e ORDER BY e",
            row_cap=10_000,
            timeout_s=60,
        )
        if not result.success:
            return []
        stats: list[EventTypeStat] = []
        for row in result.rows:
            value = row["e"]
            count = row["c"]
            stats.append(
                EventTypeStat(
                    event_type=value if isinstance(value, str) else str(value),
                    sample_count=count if isinstance(count, int) else 0,
                )
            )
        return stats
