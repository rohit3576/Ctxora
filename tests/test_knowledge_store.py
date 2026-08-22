"""Knowledge store tests: loading, cache semantics, not-onboarded gate."""

import pytest

from knowledge.contracts import TenantKnowledge
from knowledge.store import KnowledgeStore, NotOnboardedError


def canned_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
    """Fake fetcher serving one onboarded tenant 'demo' with minimal rows."""
    if sql.lower().startswith("select status from sql_agent_tenants"):
        return [("active",)] if str(params[0]) == "demo" else []
    if "FROM sql_agent_tenants WHERE" in sql:
        return [("eav preamble text",)] if str(params[0]) == "demo" else []
    if "sql_agent_telemetry_registry" in sql:
        return [
            (
                "engine.rpm",
                "engine.rpm",
                "Engine revolutions per minute",
                "numeric",
                "rpm",
                "average",
                "avg(toFloat64OrNull(value))",
                "600..3000",
                "high rpm sustained = wear",
            ),
            (
                "battery.voltage",
                "batteryVoltage",
                "Main battery voltage",
                "numeric",
                "V",
                "latest",
                "argMax(toFloat64OrNull(value), timestamp)",
                "11..15",
                "",
            ),
        ]
    if "sql_agent_aliases" in sql:
        return [
            ("rpm", "engine.rpm", "", ""),
            ("revs", "engine.rpm", "", ""),
            ("battery", "battery.voltage", "batteryVoltage", ""),
        ]
    if "sql_agent_business_rules" in sql:
        return [(1, "Bound every multi-metric CTE with a timestamp filter.")]
    if "sql_agent_sql_examples" in sql:
        return [
            (
                "average rpm of truck-102 yesterday?",
                "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry",
                "telemetry aggregation",
                "telemetry",
                "demo_telemetry",
            )
        ]
    if "sql_agent_schema_columns" in sql:
        return [
            ("demo_telemetry", "timestamp", "DateTime", ""),
            ("demo_telemetry", "device_id", "String", ""),
            ("demo_telemetry", "key", "String", ""),
            ("demo_telemetry", "value", "String", ""),
        ]
    if "sql_agent_table_metadata" in sql:
        return [("demo_telemetry", "eav", "telemetry readings", "timestamp")]
    return []


class TestLoad:
    def test_returns_structured_knowledge_for_onboarded_tenant(self) -> None:
        store = KnowledgeStore(query=canned_query)

        knowledge = store.load("demo")

        assert isinstance(knowledge, TenantKnowledge)
        assert knowledge.tenant == "demo"
        assert knowledge.eav_rules_text == "eav preamble text"
        assert [entry.canonical_key for entry in knowledge.keys] == [
            "engine.rpm",
            "battery.voltage",
        ]
        assert knowledge.keys[1].physical_key == "batteryVoltage"

    def test_alias_lookup_is_lowercased(self) -> None:
        store = KnowledgeStore(query=canned_query)

        lookup = store.load("demo").alias_lookup()

        assert lookup["revs"].canonical_key == "engine.rpm"

    def test_unknown_tenant_raises_not_onboarded(self) -> None:
        def empty_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            return []

        with pytest.raises(NotOnboardedError, match="ghost"):
            KnowledgeStore(query=empty_query).load("ghost")

    def test_tenant_without_registry_rows_raises_not_onboarded(self) -> None:
        def tenant_but_no_keys(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            if "sql_agent_telemetry_registry" in sql:
                return []
            if "FROM sql_agent_tenants WHERE" in sql:
                return [("preamble",)]
            return canned_query(sql, params)

        with pytest.raises(NotOnboardedError):
            KnowledgeStore(query=tenant_but_no_keys).load("demo")


class TestCache:
    def test_second_load_is_a_cache_hit_without_refetch(self) -> None:
        calls: list[str] = []

        def counting_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            calls.append(sql)
            return canned_query(sql, params)

        store = KnowledgeStore(query=counting_query)
        store.load("demo")
        first_count = len(calls)
        store.load("demo")

        assert len(calls) == first_count
        assert store.metrics()["cache_hits"] == 1
        assert store.metrics()["cache_misses"] == 1

    def test_invalidate_forces_refetch(self) -> None:
        def counting_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            return canned_query(sql, params)

        store = KnowledgeStore(query=counting_query)
        store.load("demo")
        KnowledgeStore.invalidate_cache("demo")
        store.load("demo")

        assert store.metrics()["cache_invalidations"] == 1
        assert store.metrics()["cache_misses"] == 2

    def test_expired_ttl_forces_refetch(self) -> None:
        def counting_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            return canned_query(sql, params)

        store = KnowledgeStore(query=counting_query, ttl_seconds=0.0)
        store.load("demo")
        store.load("demo")

        assert store.metrics()["cache_misses"] == 2

    def test_lru_eviction_when_max_tenants_reached(self) -> None:
        def two_tenant_query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            return canned_query(sql, ("demo",))

        store = KnowledgeStore(query=two_tenant_query, max_tenants=1)
        store.load("demo")
        store.load("other")

        assert store.metrics()["cache_evictions"] == 1
