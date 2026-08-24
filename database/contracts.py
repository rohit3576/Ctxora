"""Core storage contracts.

These protocols are the load-bearing walls of Ctxora (architecture doc
section 4): the agent core imports these, never a concrete engine client.
Adding a storage engine = one file in database/dialects/ + one store file.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from config.settings import ColumnMapping

# JSON-compatible scalar values a query result row may carry.
JsonScalar = int | float | str | bool | None

ErrorKind = Literal["connection", "query"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of a read-only telemetry query."""

    success: bool
    rows: tuple[Mapping[str, JsonScalar], ...]
    row_count: int
    column_names: tuple[str, ...]
    execution_time_ms: float
    error_kind: ErrorKind | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class KeyStat:
    """One distinct telemetry key observed in a tenant's table."""

    key: str
    sample_count: int
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass(frozen=True, slots=True)
class EventTypeStat:
    """One distinct event type observed in a tenant's events table."""

    event_type: str
    sample_count: int


@runtime_checkable
class Dialect(Protocol):
    """Per-engine SQL rendering. Engine knowledge is data, not code branches."""

    @property
    def name(self) -> str:
        """Dialect identifier used in config and logging."""
        ...

    @property
    def sqlglot_name(self) -> str:
        """Return the sqlglot dialect identifier for AST parsing."""
        ...

    def cast_numeric(self, value_expr: str) -> str:
        """Null-safe numeric cast of the EAV value column."""
        ...

    def latest_value_expr(self, value_expr: str, ts_col: str) -> str:
        """Aggregate expression for the latest reading in a group."""
        ...

    def json_field_float(self, json_col: str, field: str) -> str:
        """Extract a JSON payload field as a float."""
        ...

    def time_bucket(self, ts_col: str, interval: str) -> str:
        """Bucket expression for an interval like '1 HOUR' or '30 MINUTE'."""
        ...

    def now_minus(self, unit: str, n: int) -> str:
        """Expression for now minus n units (unit like 'DAY', 'HOUR')."""
        ...

    def quote_ident(self, name: str) -> str:
        """Quote an identifier with the engine's quoting character."""
        ...

    def readonly_violation_patterns(self) -> tuple[str, ...]:
        """Regex source strings for statements that must never execute."""
        ...

    def eav_system_rules(self, mapping: ColumnMapping) -> str:
        """Dialect-specific EAV rules block injected into the system prompt."""
        ...


@runtime_checkable
class TelemetryStore(Protocol):
    """The only door to tenant telemetry data."""

    @property
    def dialect(self) -> Dialect:
        """The engine dialect this store executes against."""
        ...

    def execute(self, sql: str, *, row_cap: int, timeout_s: int) -> ExecutionResult:
        """Read-only execution.

        Implementations must use a read-only credential, enforce
        cap/timeout, and distinguish connection errors from query errors
        via ExecutionResult.error_kind.
        """
        ...

    def introspect_keys(self, tenant: str) -> list[KeyStat]:
        """Distinct keys with counts and time bounds (onboarding, S5)."""
        ...

    def introspect_event_types(self, tenant: str) -> list[EventTypeStat]:
        """Distinct event types with counts."""
        ...
