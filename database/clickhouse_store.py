"""ClickHouse store adapter: read-only execution with memory guards."""

import datetime
import time

from config.settings import ColumnMapping, Settings
from database.clickhouse.gateway import ClickHouseGatewayError, run_query
from database.contracts import (
    Dialect,
    EventTypeStat,
    ExecutionResult,
    JsonScalar,
    KeyStat,
)
from database.dialects.clickhouse import ClickHouseDialect
from database.rows import to_json_scalar

_MEMORY_BUDGET_BYTES = 500_000_000


class ClickHouseStore:
    """ClickHouse adapter via the typed gateway package."""

    def __init__(
        self,
        mapping: ColumnMapping,
        settings: Settings,
        events_table_template: str | None = None,
    ) -> None:
        """Hold mapping, credentials, and the optional events table template."""
        self.mapping: ColumnMapping = mapping
        self.settings: Settings = settings
        self.events_table_template: str | None = events_table_template

    @property
    def dialect(self) -> Dialect:
        """Wire the ClickHouse dialect."""
        return ClickHouseDialect()

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        """Run one read-only query with row cap, timeout, memory guards."""
        started = time.perf_counter()
        try:
            columns, raw_rows = run_query(
                self.settings,
                sql,
                {
                    "max_result_rows": row_cap,
                    "result_overflow_mode": "break",
                    "max_execution_time": timeout_s,
                    "max_bytes_before_external_group_by": _MEMORY_BUDGET_BYTES,
                    "max_bytes_before_external_sort": _MEMORY_BUDGET_BYTES,
                },
            )
        except ClickHouseGatewayError as exc:
            return ExecutionResult(
                success=False,
                rows=(),
                row_count=0,
                column_names=(),
                execution_time_ms=0.0,
                error_kind="connection" if exc.kind == "connection" else "query",
                error=exc.detail,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        rows = tuple(
            {name: to_json_scalar(value) for name, value in zip(columns, row, strict=True)}
            for row in raw_rows
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
        table = self.mapping.table.format(tenant=tenant)
        key_col = self.mapping.key
        ts_col = self.mapping.timestamp
        result = self.execute(
            f"SELECT {key_col} AS k, count() AS c, "
            f"min({ts_col}) AS first_seen, max({ts_col}) AS last_seen "
            f"FROM {self.dialect.quote_ident(table)} GROUP BY k ORDER BY k",
            row_cap=10_000,
            timeout_s=60,
        )
        if not result.success:
            return []
        return [
            KeyStat(
                key=_as_str(row["k"]),
                sample_count=_as_int(row["c"]),
                first_seen=_as_datetime(row["first_seen"]),
                last_seen=_as_datetime(row["last_seen"]),
            )
            for row in result.rows
        ]

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        """Distinct event types with counts ([] when events are not configured)."""
        if self.events_table_template is None:
            return []
        table = self.events_table_template.format(tenant=tenant)
        result = self.execute(
            f"SELECT event_type AS e, count() AS c FROM {self.dialect.quote_ident(table)} "
            f"GROUP BY e ORDER BY e",
            row_cap=10_000,
            timeout_s=60,
        )
        if not result.success:
            return []
        return [
            EventTypeStat(event_type=_as_str(row["e"]), sample_count=_as_int(row["c"]))
            for row in result.rows
        ]


def _as_str(value: JsonScalar) -> str:
    """Narrow a scalar to str."""
    return value if isinstance(value, str) else str(value)


def _as_int(value: JsonScalar) -> int:
    """Narrow a scalar to int (0 when absent)."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _as_datetime(value: JsonScalar) -> datetime.datetime | None:
    """Parse an isoformat string back to datetime."""
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    return None
