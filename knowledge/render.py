"""Render structured knowledge into prompt sections.

Slicing happens on the structured contracts BEFORE rendering; this module
only turns already-sliced entries into text blocks.
"""

from collections.abc import Iterable, Sequence

from knowledge.contracts import (
    AliasEntry,
    BusinessRule,
    SchemaColumn,
    SQLExample,
    TableMeta,
    TelemetryKey,
)


def telemetry_section(keys: Iterable[TelemetryKey]) -> str:
    """Render registry entries as compact per-key blocks."""
    lines: list[str] = []
    for entry in keys:
        lines.append(f"### {entry.physical_key}")
        lines.append(f"- description: {entry.description}")
        lines.append(f"- datatype: {entry.datatype} (unit: {entry.unit})")
        lines.append(f"- default aggregation: {entry.aggregation}")
        if entry.typical_range:
            lines.append(f"- typical range: {entry.typical_range}")
        if entry.cast_pattern:
            lines.append(f"- access pattern: {entry.cast_pattern}")
        if entry.operational_meaning:
            lines.append(f"- operational meaning: {entry.operational_meaning}")
        lines.append("")
    return "\n".join(lines)


def aliases_section(entries: Iterable[AliasEntry]) -> str:
    """Render alias mappings as one line per entry."""
    lines = [f"'{entry.alias}' -> {entry.canonical_key}" for entry in entries]
    return "\n".join(lines)


def rules_section(rules: Iterable[BusinessRule]) -> str:
    """Render numbered rules verbatim."""
    return "\n".join(f"{entry.rule_number}. {entry.rule_text}" for entry in rules)


def examples_section(examples: Iterable[SQLExample], limit_per_key: int = 2) -> str:
    """Render few-shot pairs (caller slices; this renders with a hard cap)."""
    max_examples = limit_per_key * 3
    lines: list[str] = []
    for example in list(examples)[:max_examples]:
        lines.append(f"Q: {example.question}")
        lines.append("```sql")
        lines.append(example.sql_query)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def schema_section(columns: Sequence[SchemaColumn], tables: Sequence[TableMeta]) -> str:
    """Render table metadata plus column lists."""
    lines: list[str] = []
    by_table: dict[str, list[SchemaColumn]] = {}
    for column in columns:
        by_table.setdefault(column.table_name, []).append(column)

    for meta in tables:
        lines.append(f"### {meta.table_name} ({meta.table_type})")
        if meta.purpose:
            lines.append(f"purpose: {meta.purpose}")
        if meta.time_column:
            lines.append(f"time column: {meta.time_column}")
        for column in by_table.get(meta.table_name, []):
            suffix = f" — {column.description}" if column.description else ""
            lines.append(f"- {column.column_name}: {column.datatype}{suffix}")
        lines.append("")
    return "\n".join(lines)
