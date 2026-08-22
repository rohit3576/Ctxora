"""Onboarding naming: rule-based suggestions + review-queue promotion.

The knowledge registry is only mutated through the human-review queue:
LLM/rule suggestions land in sql_agent_key_mapping_candidates, and the
approve action promotes rows into telemetry_registry + aliases with cache
invalidation. The agent never reads the queue.
"""

import re

from knowledge.store import KnowledgeStore, Query

_WORDISH: re.Pattern[str] = re.compile(r"[a-zA-Z0-9]+")


def suggest_name(raw_key: str) -> tuple[str, str]:
    """Derive (friendly_name, alias) from a physical key, rule-based.

    'engine.coolantTemp' -> ('Engine Coolant Temp', 'engine coolant temp')
    """
    words = _WORDISH.findall(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw_key))
    lowered = " ".join(word.lower() for word in words)
    friendly = lowered.title()
    return friendly, lowered


def stage_suggestions(
    query: Query, tenant: str, candidates: tuple[tuple[str, str, float], ...], source: str
) -> int:
    """Insert suggestion rows into the review queue; return staged count."""
    staged = 0
    for physical_key, alias, confidence in candidates:
        canonical = physical_key
        rows = query("SELECT id FROM sql_agent_tenants WHERE tenant_name = %s", (tenant,))
        if not rows:
            return staged
        tenant_id = rows[0][0]
        query(
            "INSERT INTO sql_agent_key_mapping_candidates "
            "(tenant_id, canonical_key, physical_key, alias, confidence, source_doc, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending') "
            "ON CONFLICT (tenant_id, canonical_key, alias) DO NOTHING",
            (tenant_id, canonical, physical_key, alias, confidence, source),
        )
        staged += 1
    return staged


def promotion_plan(query: Query, tenant: str) -> list[dict[str, object]]:
    """Pending candidates for review, oldest first."""
    rows = query(
        "SELECT c.id, c.canonical_key, c.physical_key, c.alias, c.confidence, c.status "
        "FROM sql_agent_key_mapping_candidates c "
        "JOIN sql_agent_tenants t ON t.id = c.tenant_id "
        "WHERE t.tenant_name = %s AND c.status = 'pending' ORDER BY c.id",
        (tenant,),
    )
    return [
        {
            "candidate_id": row[0] if isinstance(row[0], int) else 0,
            "canonical_key": row[1] if isinstance(row[1], str) else "",
            "physical_key": row[2] if isinstance(row[2], str) else "",
            "alias": row[3] if isinstance(row[3], str) else "",
            "confidence": float(row[4]) if isinstance(row[4], (int, float)) else 0.0,
            "status": row[5] if isinstance(row[5], str) else "pending",
        }
        for row in rows
    ]


def promote_candidate(query: Query, tenant: str, candidate_id: int) -> bool:
    """Approve one candidate: registry + alias upsert, queue update, cache drop.

    Returns False when the candidate does not exist or is not pending.
    """
    rows = query(
        "SELECT c.id, c.canonical_key, c.physical_key, c.alias, c.tenant_id "
        "FROM sql_agent_key_mapping_candidates c "
        "JOIN sql_agent_tenants t ON t.id = c.tenant_id "
        "WHERE t.tenant_name = %s AND c.id = %s AND c.status = 'pending'",
        (tenant, candidate_id),
    )
    if not rows:
        return False
    _candidate_id, canonical, physical, alias, tenant_id = rows[0]
    friendly, lowered = suggest_name(str(physical or canonical))
    query(
        "INSERT INTO sql_agent_telemetry_registry "
        "(tenant_id, canonical_key, physical_key, description, provenance) "
        "VALUES (%s,%s,%s,%s,'onboarded') "
        "ON CONFLICT (tenant_id, canonical_key) DO UPDATE SET "
        "physical_key = EXCLUDED.physical_key",
        (tenant_id, str(canonical), str(physical), friendly),
    )
    query(
        "INSERT INTO sql_agent_aliases (tenant_id, alias, canonical_key) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, alias) DO NOTHING",
        (tenant_id, str(alias or lowered), str(canonical)),
    )
    query(
        "UPDATE sql_agent_key_mapping_candidates "
        "SET status = 'approved', reviewed_at = NOW() WHERE id = %s",
        (candidate_id,),
    )
    KnowledgeStore.invalidate_cache(tenant)
    return True


def set_activation(query: Query, tenant: str, enabled: bool) -> bool:
    """Flip the tenant's activation status; False when the tenant is unknown."""
    verb = "active" if enabled else "disabled"
    rows = query(
        "UPDATE sql_agent_tenants SET status = %s WHERE tenant_name = %s RETURNING id",
        (verb, tenant),
    )
    return bool(rows)


def tenant_active(query: Query, tenant: str) -> bool:
    """Whether the tenant exists with status 'active'."""
    rows = query(
        "SELECT 1 FROM sql_agent_tenants WHERE tenant_name = %s AND status = 'active'",
        (tenant,),
    )
    return bool(rows)


def tenant_disabled(query: Query, tenant: str) -> bool:
    """Whether the tenant exists AND is deliberately deactivated.

    Unknown tenants are NOT disabled (they surface as 422 from the
    knowledge gate instead); only an explicit non-active status blocks.
    """
    rows = query(
        "SELECT status FROM sql_agent_tenants WHERE tenant_name = %s",
        (tenant,),
    )
    if not rows:
        return False
    status = rows[0][0]
    return isinstance(status, str) and status != "active"
