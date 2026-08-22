"""Wizard tests: naming rules, review queue, promotion, activation gate."""

import pytest

from knowledge.store import KnowledgeStore
from onboarding.wizard import (
    promote_candidate,
    promotion_plan,
    set_activation,
    stage_suggestions,
    suggest_name,
    tenant_active,
    tenant_disabled,
)


class MemoryWizardQuery:
    """Executor over dicts simulating the four wizard tables."""

    def __init__(self) -> None:
        self.tenants: dict[str, dict[str, object]] = {"demo": {"id": 1, "status": "active"}}
        self.registry: list[dict[str, object]] = []
        self.aliases: list[dict[str, object]] = []
        self.candidates: list[dict[str, object]] = []
        self._next_candidate: int = 1

    def __call__(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        lowered = sql.lower()
        if (
            "from sql_agent_tenants where tenant_name" in lowered
            and "status" in lowered
            and lowered.startswith("select 1")
        ):
            row = self.tenants.get(str(params[0]))
            return [(1,)] if row and row["status"] == "active" else []
        if lowered.startswith("select status from sql_agent_tenants"):
            row = self.tenants.get(str(params[0]))
            return [(row["status"],)] if row else []
        if lowered.startswith("update sql_agent_tenants"):
            row = self.tenants.get(str(params[1]))
            if row is None:
                return []
            row["status"] = str(params[0])
            return [(row["id"],)]
        if lowered.startswith("select id from sql_agent_tenants"):
            row = self.tenants.get(str(params[0]))
            return [(row["id"],)] if row else []
        if "sql_agent_key_mapping_candidates" in lowered and lowered.startswith("insert"):
            self.candidates.append(
                {
                    "id": self._next_candidate,
                    "tenant_id": params[0],
                    "canonical_key": params[1],
                    "physical_key": params[2],
                    "alias": params[3],
                    "confidence": params[4],
                    "status": "pending",
                }
            )
            self._next_candidate += 1
            return []
        if "c.status = 'pending' order by" in lowered:
            return [
                (c["id"], c["canonical_key"], c["physical_key"], c["alias"], 0.9, "pending")
                for c in self.candidates
                if c["tenant_id"] == 1 and c["status"] == "pending"
            ]
        if "and c.status = 'pending'" in lowered:
            wanted = params[1] if isinstance(params[1], int) else 0
            for c in self.candidates:
                if c["id"] == wanted and c["status"] == "pending":
                    return [
                        (c["id"], c["canonical_key"], c["physical_key"], c["alias"], c["tenant_id"])
                    ]
            return []
        if "sql_agent_telemetry_registry" in lowered and lowered.startswith("insert"):
            self.registry.append(
                {
                    "tenant_id": params[0],
                    "canonical_key": params[1],
                    "physical_key": params[2],
                }
            )
            return []
        if "sql_agent_aliases" in lowered and lowered.startswith("insert"):
            self.aliases.append(
                {"tenant_id": params[0], "alias": params[1], "canonical_key": params[2]}
            )
            return []
        if lowered.startswith("update sql_agent_key_mapping_candidates"):
            wanted = params[0] if isinstance(params[0], int) else -1
            for c in self.candidates:
                if c["id"] == wanted:
                    c["status"] = "approved"
            return []
        return []


@pytest.fixture
def wq() -> MemoryWizardQuery:
    return MemoryWizardQuery()


class TestSuggestName:
    def test_dotted_camel_key_splits(self) -> None:
        assert suggest_name("engine.coolantTemp") == (
            "Engine Coolant Temp",
            "engine coolant temp",
        )

    def test_plain_key_rounds_through(self) -> None:
        assert suggest_name("speed") == ("Speed", "speed")


class TestReviewQueue:
    def test_stage_then_plan_lists_pending(self, wq: MemoryWizardQuery) -> None:
        staged = stage_suggestions(
            wq, "demo", (("engine.oilTemp", "oil temperature", 0.9),), "manual.pdf"
        )

        assert staged == 1
        plan = promotion_plan(wq, "demo")
        assert plan[0]["alias"] == "oil temperature"

    def test_unknown_tenant_stages_nothing(self, wq: MemoryWizardQuery) -> None:
        assert stage_suggestions(wq, "ghost", (("k", "a", 0.5),), "doc") == 0

    def test_promote_writes_registry_alias_and_marks_approved(
        self, wq: MemoryWizardQuery, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        KnowledgeStore.reset_state()
        stage_suggestions(wq, "demo", (("engine.oilTemp", "oil temperature", 0.9),), "manual.pdf")

        promoted = promote_candidate(wq, "demo", 1)

        assert promoted is True
        assert wq.registry[0]["canonical_key"] == "engine.oilTemp"
        assert wq.aliases[0]["alias"] == "oil temperature"
        assert wq.candidates[0]["status"] == "approved"

    def test_promote_twice_is_false(self, wq: MemoryWizardQuery) -> None:
        stage_suggestions(wq, "demo", (("k", "a", 0.9),), "doc")
        assert promote_candidate(wq, "demo", 1) is True
        assert promote_candidate(wq, "demo", 1) is False


class TestActivation:
    def test_active_by_default_and_flips(self, wq: MemoryWizardQuery) -> None:
        assert tenant_active(wq, "demo") is True

        assert set_activation(wq, "demo", enabled=False) is True
        assert tenant_active(wq, "demo") is False

        assert set_activation(wq, "demo", enabled=True) is True
        assert tenant_active(wq, "demo") is True

    def test_unknown_tenant_is_inactive(self, wq: MemoryWizardQuery) -> None:
        assert tenant_active(wq, "ghost") is False
        assert set_activation(wq, "ghost", enabled=True) is False


class TestDisabledGate:
    def test_active_tenant_is_not_disabled(self, wq: MemoryWizardQuery) -> None:
        assert tenant_disabled(wq, "demo") is False

    def test_disabled_tenant_is_disabled(self, wq: MemoryWizardQuery) -> None:
        set_activation(wq, "demo", enabled=False)

        assert tenant_disabled(wq, "demo") is True

    def test_unknown_tenant_is_not_disabled_goes_422_instead(self, wq: MemoryWizardQuery) -> None:
        assert tenant_disabled(wq, "ghost") is False
