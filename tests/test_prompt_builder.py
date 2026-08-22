"""Prompt assembly tests: section order, dialect rules, resolved keys."""

from agent.key_resolver import ResolvedKey
from agent.prompt_builder import PromptBuilder, build_prompt
from config.settings import ColumnMapping
from database.dialects.clickhouse import ClickHouseDialect
from knowledge.contracts import (
    AliasEntry,
    BusinessRule,
    SQLExample,
    TelemetryKey,
    TenantKnowledge,
)


def mapping() -> ColumnMapping:
    return ColumnMapping(
        table="demo_telemetry",
        timestamp="timestamp",
        entity_id="device_id",
        key="key",
        value="value",
    )


def knowledge() -> TenantKnowledge:
    keys = (
        TelemetryKey("engine.rpm", "engine.rpm", "RPM", "numeric", "rpm", "average", "", "", ""),
        TelemetryKey("speed", "speed", "Speed", "numeric", "km/h", "average", "", "", ""),
        TelemetryKey("fuel.level", "fuel", "Fuel", "numeric", "%", "latest", "", "", ""),
    )
    aliases = (AliasEntry("rpm", "engine.rpm", "", ""), AliasEntry("speed", "speed", "", ""))
    rules = (BusinessRule(1, "Bound multi-metric CTEs with a timestamp filter."),)
    examples = (
        SQLExample(
            "average rpm?",
            "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'",
            "",
            "telemetry",
            "",
        ),
        SQLExample(
            "latest fuel?",
            "SELECT argMax(toFloat64OrNull(value), timestamp) FROM demo_telemetry",
            "",
            "telemetry",
            "",
        ),
    )
    return TenantKnowledge(
        tenant="demo",
        keys=keys,
        aliases=aliases,
        rules=rules,
        examples=examples,
        schema_columns=(),
        table_metadata=(),
        eav_rules_text="EAV preamble from tenant row.",
    )


class TestBuildPrompt:
    def test_contains_dialect_eav_rules_and_output_contract(self) -> None:
        system, user = build_prompt(
            dialect=ClickHouseDialect(),
            mapping=mapping(),
            knowledge=knowledge(),
            resolved=(ResolvedKey("rpm", "engine.rpm", "engine.rpm"),),
            question="average rpm yesterday?",
        )

        assert "toFloat64OrNull(value)" in system
        assert "```sql" in system
        assert "average rpm yesterday?" in user

    def test_resolved_keys_are_sliced_into_telemetry_section(self) -> None:
        _system, user = build_prompt(
            dialect=ClickHouseDialect(),
            mapping=mapping(),
            knowledge=knowledge(),
            resolved=(ResolvedKey("rpm", "engine.rpm", "engine.rpm"),),
            question="rpm?",
        )

        assert "engine.rpm" in user
        assert "### fuel" not in user

    def test_unresolved_keys_are_not_injected(self) -> None:
        _system, user = build_prompt(
            dialect=ClickHouseDialect(),
            mapping=mapping(),
            knowledge=knowledge(),
            resolved=(),
            question="weather?",
        )

        assert "### engine.rpm" not in user
        assert "### fuel" not in user

    def test_rules_and_eav_preamble_always_present(self) -> None:
        _system, user = build_prompt(
            dialect=ClickHouseDialect(),
            mapping=mapping(),
            knowledge=knowledge(),
            resolved=(),
            question="anything?",
        )

        assert "Bound multi-metric CTEs" in user
        assert "EAV preamble from tenant row." in user

    def test_examples_are_sliced_to_resolved_keys(self) -> None:
        _system, user = build_prompt(
            dialect=ClickHouseDialect(),
            mapping=mapping(),
            knowledge=knowledge(),
            resolved=(ResolvedKey("rpm", "engine.rpm", "engine.rpm"),),
            question="rpm?",
        )

        assert "average rpm?" in user
        assert "latest fuel?" not in user


class TestPromptBuilderClass:
    def test_builder_reuses_arguments(self) -> None:
        builder = PromptBuilder(dialect=ClickHouseDialect(), mapping=mapping())
        _system, user = builder.build(knowledge(), (), "q?")

        assert "q?" in user
