"""Phase 7 acceptance: auth, rate limit, activation gate, wizard flow."""

import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from config.settings import DEFAULT_CONFIG_PATH, Settings
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_knowledge_store import canned_query
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore

SECRET = "0123456789abcdef0123456789abcdef"  # noqa: S105 (RFC 7518 test key)
_PLAN = TypeAdapter(list[dict[str, object]])


class WizardQuery:
    """Executor answering knowledge queries + wizard tables."""

    def __init__(self) -> None:
        self.tenant_status: dict[str, str] = {"demo": "active"}
        self.candidates: list[dict[str, object]] = []
        self.registry_writes: list[dict[str, object]] = []
        self._next_id: int = 1

    def __call__(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        lowered = sql.lower()
        if lowered.startswith("select status from sql_agent_tenants"):
            status = self.tenant_status.get(str(params[0]))
            return [(status,)] if status else []
        if lowered.startswith("update sql_agent_tenants"):
            tenant = str(params[1])
            if tenant not in self.tenant_status:
                return []
            self.tenant_status[tenant] = str(params[0])
            return [(1,)]
        if lowered.startswith("select id from sql_agent_tenants"):
            return [(1,)] if str(params[0]) in self.tenant_status else []
        if "key_mapping_candidates" in lowered and lowered.startswith("insert"):
            self.candidates.append(
                {
                    "id": self._next_id,
                    "canonical_key": params[1],
                    "physical_key": params[2],
                    "alias": params[3],
                    "status": "pending",
                }
            )
            self._next_id += 1
            return []
        if "c.status = 'pending' order by" in lowered:
            return [
                (c["id"], c["canonical_key"], c["physical_key"], c["alias"], 0.9, "pending")
                for c in self.candidates
                if c["status"] == "pending"
            ]
        if "and c.status = 'pending'" in lowered:
            wanted = params[1] if isinstance(params[1], int) else 0
            for c in self.candidates:
                if c["id"] == wanted and c["status"] == "pending":
                    return [(c["id"], c["canonical_key"], c["physical_key"], c["alias"], 1)]
            return []
        if "sql_agent_telemetry_registry" in lowered and lowered.startswith("insert"):
            self.registry_writes.append({"canonical_key": params[1], "physical_key": params[2]})
            return []
        if "sql_agent_aliases" in lowered and lowered.startswith("insert"):
            return []
        if lowered.startswith("update sql_agent_key_mapping_candidates"):
            for c in self.candidates:
                if c["id"] == (params[0] if isinstance(params[0], int) else -1):
                    c["status"] = "approved"
            return []
        return canned_query(sql, params)


def ratelimit_on(tmp_path: Path) -> Path:
    """defaults.yaml with ratelimit flag on and a small limit."""
    tuned = Path(DEFAULT_CONFIG_PATH).read_text()
    tuned = tuned.replace("ratelimit: false", "ratelimit: true")
    tuned = tuned.replace("requests_per_minute: 60", "requests_per_minute: 6000")
    tuned = tuned.replace("burst: 10", "burst: 3")
    path = tmp_path / "rl.yaml"
    path.write_text(tuned)
    return path


def make_token(claims: dict[str, object]) -> str:
    payload = {"exp": int(time.time()) + 600, **claims}
    return pyjwt.encode(payload, SECRET, algorithm="HS256")


class WireLLM(DemoFakeLLM):
    """Demo LLM compatible with Sequence embed."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1] for _ in texts]


@pytest.fixture
def wizard_query() -> WizardQuery:
    return WizardQuery()


@pytest.fixture
def client(wizard_query: WizardQuery) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=DemoStore(),
        knowledge_query=wizard_query,
        llm=WireLLM(),
        memory=InMemoryMemoryStore(),
    )
    with TestClient(app) as c:
        yield c


class TestAuthEnforced:
    def _enforced(self, wizard_query: WizardQuery) -> TestClient:
        app = create_app(
            settings=Settings(auth_disabled=False, jwt_secret=SECRET, tenant_claim="tenant"),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=wizard_query,
            llm=WireLLM(),
            memory=InMemoryMemoryStore(),
        )
        return TestClient(app)

    def test_missing_token_is_401(self, wizard_query: WizardQuery) -> None:
        with self._enforced(wizard_query) as enforced:
            response = enforced.post("/v1/query/sql", json={"tenant": "demo", "query": "rpm?"})

        assert response.status_code == 401
        body: dict[str, object] = response.json()
        assert body["errorType"] == "UNAUTHORIZED"

    def test_valid_token_claim_wins_over_request(self, wizard_query: WizardQuery) -> None:
        token = make_token({"tenant": "demo"})

        with self._enforced(wizard_query) as enforced:
            response = enforced.post(
                "/v1/query/sql",
                json={"tenant": "spoofed", "query": "average rpm?"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200

    def test_expired_token_is_401(self, wizard_query: WizardQuery) -> None:
        expired = pyjwt.encode(
            {"exp": int(time.time()) - 10, "tenant": "acme"}, SECRET, algorithm="HS256"
        )
        with self._enforced(wizard_query) as enforced:
            response = enforced.post(
                "/v1/query/sql",
                json={"tenant": "demo", "query": "rpm?"},
                headers={"Authorization": f"Bearer {expired}"},
            )

        assert response.status_code == 401


class TestActivationGate:
    def test_disabled_tenant_is_403(self, wizard_query: WizardQuery, client: TestClient) -> None:
        wizard_query.tenant_status["demo"] = "disabled"

        response = client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})

        assert response.status_code == 403
        body: dict[str, object] = response.json()
        assert body["errorType"] == "TENANT_NOT_ACTIVE"

    def test_unknown_tenant_stays_422_not_403(self, client: TestClient) -> None:
        response = client.post("/v1/query/sql", json={"tenant": "ghost", "query": "average rpm?"})

        assert response.status_code == 422


class TestRateLimit:
    def test_burst_exhaustion_returns_429(self, wizard_query: WizardQuery, tmp_path: Path) -> None:
        app = create_app(
            settings=Settings(),
            config_path=ratelimit_on(tmp_path),
            store=DemoStore(),
            knowledge_query=wizard_query,
            llm=WireLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as limited:
            statuses = [
                limited.post(
                    "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
                ).status_code
                for _ in range(5)
            ]

        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429

    def test_tenants_isolated_under_limits(self, wizard_query: WizardQuery, tmp_path: Path) -> None:
        app = create_app(
            settings=Settings(),
            config_path=ratelimit_on(tmp_path),
            store=DemoStore(),
            knowledge_query=wizard_query,
            llm=WireLLM(),
            memory=InMemoryMemoryStore(),
        )
        wizard_query.tenant_status["other"] = "active"
        with TestClient(app) as limited:
            for _ in range(3):
                limited.post("/v1/query/sql", json={"tenant": "demo", "query": "rpm?"})
            other = limited.post("/v1/query/sql", json={"tenant": "other", "query": "average rpm?"})

        # "other" has its own bucket: throttling on demo must not leak over.
        # 422 is honest here (other is not onboarded); 429 would mean leakage.
        assert other.status_code != 429


class TestWizardFlow:
    def test_probe_naming_stage_approve_enable(
        self, client: TestClient, wizard_query: WizardQuery
    ) -> None:
        suggestions = client.get("/v1/onboarding/demo/naming-suggestions")
        assert suggestions.status_code == 200

        staged = client.post(
            "/v1/onboarding/demo/candidates",
            json={
                "source": "probe",
                "candidates": [{"physicalKey": "engine.oilTemp", "alias": "oil temperature"}],
            },
        )
        assert staged.status_code == 200
        assert staged.json()["data"]["staged"] == 1

        queue = client.get("/v1/onboarding/demo/review")
        plan = _PLAN.validate_python(queue.json()["data"])
        assert plan and plan[0]["alias"] == "oil temperature"

        approved = client.post("/v1/onboarding/demo/candidates/1/approve")
        assert approved.status_code == 200
        assert wizard_query.registry_writes[0]["canonical_key"] == "engine.oilTemp"
        assert wizard_query.candidates[0]["status"] == "approved"

        disabled = client.post("/v1/onboarding/demo/disable")
        assert disabled.status_code == 200
        blocked = client.post("/v1/query/sql", json={"tenant": "demo", "query": "rpm?"})
        assert blocked.status_code == 403

        enabled = client.post("/v1/onboarding/demo/enable")
        assert enabled.status_code == 200
        ok = client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})
        assert ok.status_code == 200

    def test_approve_unknown_candidate_is_404(self, client: TestClient) -> None:
        response = client.post("/v1/onboarding/demo/candidates/999/approve")

        assert response.status_code == 404

    def test_enable_unknown_tenant_is_404(self, client: TestClient) -> None:
        response = client.post("/v1/onboarding/ghost/enable")

        assert response.status_code == 404
