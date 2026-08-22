"""Knowledge store: cached PG access to tenant knowledge.

Single-connection loads, thread-safe class-level TTL+LRU cache, write-path
invalidation. A tenant that is not onboarded raises NotOnboardedError so the
API boundary can map it to a 4xx (never a silent fallback).
"""

import threading
import time
from collections.abc import Callable
from typing import ClassVar, Final

from knowledge.contracts import (
    AliasEntry,
    BusinessRule,
    SchemaColumn,
    SQLExample,
    TableMeta,
    TelemetryKey,
    TenantKnowledge,
)

Query = Callable[[str, tuple[object, ...]], list[tuple[object, ...]]]


class NotOnboardedError(Exception):
    """Tenant has no metadata registered in the knowledge store."""

    def __init__(self, tenant: str) -> None:
        """Name the tenant that is missing metadata."""
        self.tenant: str = tenant
        super().__init__(f"tenant '{tenant}' is not onboarded")


def _text(value: object) -> str:
    """Narrow a row cell to str (empty when not a str)."""
    return value if isinstance(value, str) else ""


def _whole(value: object) -> int:
    """Narrow a row cell to int (0 when not numeric)."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


class KnowledgeStore:
    """Loads and caches TenantKnowledge; fetcher injectable for tests."""

    _cache: ClassVar[dict[str, tuple[TenantKnowledge, float]]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _metrics: ClassVar[dict[str, int | float]] = {
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_invalidations": 0,
        "cache_evictions": 0,
    }

    def __init__(
        self,
        query: Query,
        ttl_seconds: float = 300.0,
        max_tenants: int = 100,
    ) -> None:
        """Bind a parameterized-query callable (sql, params) -> rows."""
        self._query: Query = query
        self.ttl: Final = ttl_seconds
        self.max_tenants: Final = max_tenants

    @classmethod
    def invalidate_cache(cls, tenant: str) -> None:
        """Drop one tenant's cached knowledge (write paths call this)."""
        with cls._lock:
            if tenant in cls._cache:
                del cls._cache[tenant]
                cls._metrics["cache_invalidations"] += 1

    @classmethod
    def metrics(cls) -> dict[str, int | float]:
        """Copy of the cache counters for observability."""
        with cls._lock:
            return dict(cls._metrics)

    def fetch_semantic_examples(
        self,
        tenant: str,
        embedding: list[float],
        threshold: float,
        limit: int,
    ) -> list[SQLExample]:
        """Cosine retrieval over approved embedded examples (caller embeds).

        Returns hits at/above ``threshold`` up to ``limit`` and bumps their
        usage stats. Raises on failure; the pipeline catches and falls back
        to keyword slicing (fail-open at that boundary, not here).
        """
        vector = "[" + ",".join(str(value) for value in embedding) + "]"
        rows = self._query(
            "SELECT s.id, s.question, s.sql_query, s.intent, s.query_category, "
            "s.tables_used, 1 - (s.embedding <=> %s::vector) AS similarity "
            "FROM sql_agent_sql_examples s "
            "JOIN sql_agent_tenants t ON t.id = s.tenant_id "
            "WHERE t.tenant_name = %s AND s.status = 'approved' "
            "AND s.embedding IS NOT NULL "
            "ORDER BY s.embedding <=> %s::vector LIMIT 10",
            (vector, tenant, vector),
        )
        selected_ids: list[int] = []
        examples: list[SQLExample] = []
        for row in rows:
            similarity = row[6] if isinstance(row[6], (int, float)) else 0.0
            if similarity < threshold:
                continue
            selected_ids.append(_whole(row[0]))
            examples.append(
                SQLExample(
                    question=_text(row[1]),
                    sql_query=_text(row[2]),
                    intent=_text(row[3]),
                    query_category=_text(row[4]),
                    tables_used=_text(row[5]),
                )
            )
            if len(examples) >= limit:
                break
        if selected_ids:
            placeholders = ", ".join(str(example_id) for example_id in selected_ids)
            self._query(
                f"UPDATE sql_agent_sql_examples "
                f"SET use_count = use_count + 1, last_used_at = NOW() "
                f"WHERE id IN ({placeholders})",
                (),
            )
        return examples

    @classmethod
    def reset_state(cls) -> None:
        """Clear the cache and counters (test isolation hook)."""
        with cls._lock:
            cls._cache.clear()
            cls._metrics.update(
                {
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "cache_invalidations": 0,
                    "cache_evictions": 0,
                }
            )

    def load(self, tenant: str) -> TenantKnowledge:
        """Return cached knowledge or cold-load it from the fetcher."""
        with self._lock:
            cached = self._cache.get(tenant)
        if cached is not None and time.monotonic() - cached[1] < self.ttl:
            with self._lock:
                self._metrics["cache_hits"] += 1
            return cached[0]

        with self._lock:
            self._metrics["cache_misses"] += 1
        knowledge = self._cold_load(tenant)
        with self._lock:
            if len(self._cache) >= self.max_tenants:
                oldest = min(self._cache, key=lambda name: self._cache[name][1])
                del self._cache[oldest]
                self._metrics["cache_evictions"] += 1
            self._cache[tenant] = (knowledge, time.monotonic())
        return knowledge

    def _cold_load(self, tenant: str) -> TenantKnowledge:
        rows = self._query(
            "SELECT eav_rules_text FROM sql_agent_tenants WHERE tenant_name = %s",
            (tenant,),
        )
        if not rows:
            raise NotOnboardedError(tenant)

        key_rows = self._query(_KEYS_SQL, (tenant,))
        if not key_rows:
            raise NotOnboardedError(tenant)

        keys = tuple(
            TelemetryKey(
                canonical_key=_text(row[0]),
                physical_key=_text(row[1]) or _text(row[0]),
                description=_text(row[2]),
                datatype=_text(row[3]),
                unit=_text(row[4]),
                aggregation=_text(row[5]) or "average",
                cast_pattern=_text(row[6]),
                typical_range=_text(row[7]),
                operational_meaning=_text(row[8]),
            )
            for row in key_rows
        )
        aliases = tuple(
            AliasEntry(
                alias=_text(row[0]),
                canonical_key=_text(row[1]),
                alternative_key=_text(row[2]),
                owning_table=_text(row[3]),
            )
            for row in self._query(_ALIASES_SQL, (tenant,))
        )
        rules = tuple(
            BusinessRule(rule_number=_whole(row[0]), rule_text=_text(row[1]))
            for row in self._query(_RULES_SQL, (tenant,))
        )
        examples = tuple(
            SQLExample(
                question=_text(row[0]),
                sql_query=_text(row[1]),
                intent=_text(row[2]),
                query_category=_text(row[3]),
                tables_used=_text(row[4]),
            )
            for row in self._query(_EXAMPLES_SQL, (tenant,))
        )
        columns = tuple(
            SchemaColumn(
                table_name=_text(row[0]),
                column_name=_text(row[1]),
                datatype=_text(row[2]),
                description=_text(row[3]),
            )
            for row in self._query(_COLUMNS_SQL, (tenant,))
        )
        tables = tuple(
            TableMeta(
                table_name=_text(row[0]),
                table_type=_text(row[1]),
                purpose=_text(row[2]),
                time_column=_text(row[3]),
            )
            for row in self._query(_TABLES_SQL, (tenant,))
        )
        return TenantKnowledge(
            tenant=tenant,
            keys=keys,
            aliases=aliases,
            rules=rules,
            examples=examples,
            schema_columns=columns,
            table_metadata=tables,
            eav_rules_text=_text(rows[0][0]),
        )


_TENANT_JOIN = "JOIN sql_agent_tenants t ON t.id = {col}.tenant_id WHERE t.tenant_name = %s"

_KEYS_SQL = f"""
    SELECT r.canonical_key, r.physical_key, r.description, r.datatype, r.unit,
           r.aggregation, r.cast_pattern, r.typical_range, r.operational_meaning
    FROM sql_agent_telemetry_registry r
    {_TENANT_JOIN.format(col="r")}
    ORDER BY r.canonical_key
"""

_ALIASES_SQL = f"""
    SELECT a.alias, a.canonical_key, a.alternative_key, a.owning_table
    FROM sql_agent_aliases a
    {_TENANT_JOIN.format(col="a")}
    ORDER BY a.canonical_key, a.alias
"""

_RULES_SQL = f"""
    SELECT b.rule_number, b.rule_text
    FROM sql_agent_business_rules b
    {_TENANT_JOIN.format(col="b")}
    ORDER BY b.rule_number
"""

_EXAMPLES_SQL = f"""
    SELECT s.question, s.sql_query, s.intent, s.query_category, s.tables_used
    FROM sql_agent_sql_examples s
    {_TENANT_JOIN.format(col="s")}
    WHERE s.status = 'approved'
    ORDER BY s.id
"""

_COLUMNS_SQL = f"""
    SELECT c.table_name, c.column_name, c.datatype, c.description
    FROM sql_agent_schema_columns c
    {_TENANT_JOIN.format(col="c")}
    ORDER BY c.table_name, c.column_name
"""

_TABLES_SQL = f"""
    SELECT m.table_name, m.table_type, m.purpose, m.time_column
    FROM sql_agent_table_metadata m
    {_TENANT_JOIN.format(col="m")}
    ORDER BY m.table_name
"""
