"""Key resolver tests: NL phrases -> verified canonical telemetry keys."""

from agent.key_resolver import KeyResolver, ResolvedKey
from knowledge.contracts import AliasEntry, TelemetryKey, TenantKnowledge


def knowledge() -> TenantKnowledge:
    keys = (
        TelemetryKey("engine.rpm", "engine.rpm", "", "numeric", "rpm", "average", "", "", ""),
        TelemetryKey("speed", "speed", "", "numeric", "km/h", "average", "", "", ""),
        TelemetryKey("battery.voltage", "batteryVoltage", "", "numeric", "V", "latest", "", "", ""),
    )
    aliases = (
        AliasEntry("rpm", "engine.rpm", "", ""),
        AliasEntry("revs", "engine.rpm", "", ""),
        AliasEntry("engine speed", "engine.rpm", "", ""),
        AliasEntry("speed", "speed", "", ""),
        AliasEntry("battery", "battery.voltage", "batteryVoltage", ""),
    )
    return TenantKnowledge(
        tenant="demo",
        keys=keys,
        aliases=aliases,
        rules=(),
        examples=(),
        schema_columns=(),
        table_metadata=(),
        eav_rules_text="",
    )


class TestResolve:
    def test_alias_phrase_resolves_to_canonical(self) -> None:
        resolved = KeyResolver().resolve("What was the average rpm of truck-102?", knowledge())

        assert resolved.keys[0].canonical_key == "engine.rpm"

    def test_longest_phrase_wins_over_token_overlap(self) -> None:
        resolved = KeyResolver().resolve("average engine speed today?", knowledge())

        assert [entry.canonical_key for entry in resolved.keys] == ["engine.rpm"]

    def test_multiple_keys_resolve_in_order_of_appearance(self) -> None:
        resolved = KeyResolver().resolve("compare speed and revs", knowledge())

        assert [entry.canonical_key for entry in resolved.keys] == ["speed", "engine.rpm"]

    def test_physical_key_is_carried(self) -> None:
        resolved = KeyResolver().resolve("battery level?", knowledge())

        assert resolved.keys[0].physical_key == "batteryVoltage"

    def test_unknown_words_resolve_to_nothing(self) -> None:
        resolved = KeyResolver().resolve("what is the weather?", knowledge())

        assert resolved.keys == ()

    def test_word_boundary_prevents_substring_false_match(self) -> None:
        resolved = KeyResolver().resolve("speedometer reading is odd?", knowledge())

        assert resolved.keys == ()


class TestUnresolvedKeys:
    def test_resolved_key_is_frozen(self) -> None:
        entry = ResolvedKey(
            alias_matched="rpm", canonical_key="engine.rpm", physical_key="engine.rpm"
        )

        assert entry == ResolvedKey(
            alias_matched="rpm", canonical_key="engine.rpm", physical_key="engine.rpm"
        )
