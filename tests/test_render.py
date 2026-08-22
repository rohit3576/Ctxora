"""Render tests: structured knowledge -> prompt section text."""

from knowledge.contracts import (
    AliasEntry,
    BusinessRule,
    SchemaColumn,
    SQLExample,
    TableMeta,
    TelemetryKey,
)
from knowledge.render import (
    aliases_section,
    examples_section,
    rules_section,
    schema_section,
    telemetry_section,
)


def _key() -> TelemetryKey:
    return TelemetryKey(
        canonical_key="engine.rpm",
        physical_key="engine.rpm",
        description="Engine revolutions per minute",
        datatype="numeric",
        unit="rpm",
        aggregation="average",
        cast_pattern="avg(toFloat64OrNull(value))",
        typical_range="600..3000",
        operational_meaning="wear indicator",
    )


class TestTelemetrySection:
    def test_renders_physical_key_and_aggregation(self) -> None:
        text = telemetry_section([_key()])

        assert "### engine.rpm" in text
        assert "default aggregation: average" in text
        assert "wear indicator" in text


class TestAliasesSection:
    def test_renders_arrow_mappings(self) -> None:
        entry = AliasEntry(
            alias="rpm", canonical_key="engine.rpm", alternative_key="", owning_table=""
        )

        assert "'rpm' -> engine.rpm" in aliases_section([entry])


class TestRulesSection:
    def test_renders_numbered_rules(self) -> None:
        text = rules_section([BusinessRule(rule_number=1, rule_text="Bound CTEs.")])

        assert "1. Bound CTEs." in text


class TestExamplesSection:
    def test_renders_fenced_sql_pairs(self) -> None:
        example = SQLExample(
            question="avg rpm yesterday?",
            sql_query="SELECT 1",
            intent="",
            query_category="telemetry",
            tables_used="",
        )

        text = examples_section([example])

        assert "Q: avg rpm yesterday?" in text
        assert "```sql" in text
        assert "SELECT 1" in text


class TestSchemaSection:
    def test_renders_tables_with_columns(self) -> None:
        columns = [
            SchemaColumn("demo_telemetry", "key", "String", "metric name"),
            SchemaColumn("demo_telemetry", "value", "String", ""),
        ]
        tables = [TableMeta("demo_telemetry", "eav", "readings", "timestamp")]

        text = schema_section(columns, tables)

        assert "### demo_telemetry (eav)" in text
        assert "- key: String — metric name" in text
        assert "- value: String" in text
