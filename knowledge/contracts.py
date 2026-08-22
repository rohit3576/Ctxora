"""Structured knowledge contracts: what the agent knows about a tenant.

These frozen dataclasses are the in-memory shape of the sql_agent_* tables.
Retrieval slices THEM; rendering turns them into prompt sections. No
markdown round-trip anywhere.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelemetryKey:
    """One registered telemetry metric."""

    canonical_key: str
    physical_key: str
    description: str
    datatype: str
    unit: str
    aggregation: str
    cast_pattern: str
    typical_range: str
    operational_meaning: str


@dataclass(frozen=True, slots=True)
class AliasEntry:
    """One natural-language synonym mapped to a canonical key."""

    alias: str
    canonical_key: str
    alternative_key: str
    owning_table: str


@dataclass(frozen=True, slots=True)
class BusinessRule:
    """One numbered SQL-generation invariant."""

    rule_number: int
    rule_text: str


@dataclass(frozen=True, slots=True)
class SQLExample:
    """One approved few-shot question/SQL pair."""

    question: str
    sql_query: str
    intent: str
    query_category: str
    tables_used: str


@dataclass(frozen=True, slots=True)
class SchemaColumn:
    """One physical column of one tenant table."""

    table_name: str
    column_name: str
    datatype: str
    description: str


@dataclass(frozen=True, slots=True)
class TableMeta:
    """One tenant table's metadata."""

    table_name: str
    table_type: str
    purpose: str
    time_column: str


@dataclass(frozen=True, slots=True)
class TenantKnowledge:
    """Everything the agent knows about one tenant, structured."""

    tenant: str
    keys: tuple[TelemetryKey, ...]
    aliases: tuple[AliasEntry, ...]
    rules: tuple[BusinessRule, ...]
    examples: tuple[SQLExample, ...]
    schema_columns: tuple[SchemaColumn, ...]
    table_metadata: tuple[TableMeta, ...]
    eav_rules_text: str

    def keys_by_canonical(self) -> dict[str, TelemetryKey]:
        """Index registry entries by canonical key."""
        return {entry.canonical_key: entry for entry in self.keys}

    def alias_lookup(self) -> dict[str, AliasEntry]:
        """Index aliases by their lowercase alias phrase."""
        return {entry.alias.lower(): entry for entry in self.aliases}
