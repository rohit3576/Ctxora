"""Onboarding state access: probe write-through + readiness inputs."""

import json

from knowledge.store import Query


class OnboardingStateStore:
    """Reads/writes the minimal onboarding_state row over an executor."""

    def __init__(self, query: Query) -> None:
        """Bind the (sql, params) -> rows executor (commits writes)."""
        self._query: Query = query

    def save_probe(self, tenant: str, payload: dict[str, object]) -> None:
        """Upsert the tenant's probe result into step_data."""
        self._query(
            "INSERT INTO onboarding_state (tenant, current_step, step_data) "
            "VALUES (%s, 'probed', %s) "
            "ON CONFLICT (tenant) DO UPDATE SET step_data = EXCLUDED.step_data, "
            "updated_at = NOW()",
            (tenant, json.dumps(payload)),
        )

    def probe_cached(self, tenant: str) -> bool:
        """Whether a probe result row exists for the tenant."""
        rows = self._query("SELECT 1 FROM onboarding_state WHERE tenant = %s", (tenant,))
        return bool(rows)
