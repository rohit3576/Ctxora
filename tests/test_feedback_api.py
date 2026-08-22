"""Feedback API tests: capture endpoint, token gate, full flywheel loop."""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent.pipeline import QuerySuccess
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from feedback.fake import InMemoryFeedbackStore
from feedback.promotion import approve
from knowledge.store import KnowledgeStore
from llm.client import GenResult
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_feedback_loop import insert
from tests.test_knowledge_store import canned_query
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore, demo_knowledge_query

ADMIN_TOKEN = "qp-test-admin-token"  # noqa: S105 (test fixture token, not a secret)


class WiretapLLM:
    """Demo LLM recording every prompt it receives."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.embedded: list[str] = []
        self.inner: DemoFakeLLM = DemoFakeLLM()

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.prompts.append(user)
        return self.inner.generate(system, user, temperature=temperature)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[0.5] for _ in texts]


def flags_on_config(tmp_path: Path) -> Path:
    """defaults.yaml with feedback_capture on."""
    tuned = DEFAULT_CONFIG_PATH.read_text().replace(
        "feedback_capture: false", "feedback_capture: true"
    )
    path = tmp_path / "feedback_on.yaml"
    path.write_text(tuned)
    return path


@pytest.fixture
def harness(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]]:
    memory = InMemoryMemoryStore()
    feedback = InMemoryFeedbackStore()
    llm = WiretapLLM()
    app = create_app(
        settings=Settings(feedback_admin_token=ADMIN_TOKEN),
        config_path=flags_on_config(tmp_path),
        store=DemoStore(),
        knowledge_query=canned_query,
        llm=llm,
        memory=memory,
        feedback=feedback,
    )
    with TestClient(app) as client:
        yield client, memory, feedback, llm


class TestCaptureEndpoint:
    def test_thumbs_down_with_comment_lands_in_pending_and_stats_counts_it(
        self, harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]
    ) -> None:
        client, _memory, feedback, _llm = harness
        data = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
        ).json()["data"]

        response = client.post(
            "/v1/feedback",
            json={"historyId": data["historyId"], "rating": "down", "comment": "wrong agg"},
        )

        assert response.status_code == 200
        rows = feedback.list_by_status(("pending",))
        assert len(rows) == 1
        assert rows[0].user_comment == "wrong agg"
        assert rows[0].tenant == "demo"
        stats = client.get("/admin/feedback/stats", headers={"X-Admin-Token": ADMIN_TOKEN})
        assert stats.json()["data"]["pending"] == 1

    def test_unknown_history_is_404(
        self, harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]
    ) -> None:
        client, _memory, _feedback, _llm = harness

        response = client.post("/v1/feedback", json={"historyId": 424242, "rating": "up"})

        assert response.status_code == 404

    def test_capture_router_absent_when_flag_off(self, tmp_path: Path) -> None:
        app = create_app(settings=Settings(feedback_admin_token=ADMIN_TOKEN))

        route_paths: list[str] = [
            route_path
            for route in app.routes
            if isinstance(route_path := getattr(route, "path", None), str)
        ]

        assert "/v1/feedback" not in route_paths
        assert "/admin/feedback/pending" not in route_paths


class TestTokenGate:
    def test_admin_endpoints_403_without_token(
        self, harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]
    ) -> None:
        client, _memory, _feedback, _llm = harness

        for path in (
            "/admin/feedback/pending",
            "/admin/feedback/stats",
            "/admin/feedback/golden-eval",
        ):
            assert client.get(path).status_code == 403

    def test_admin_endpoints_403_with_wrong_token(
        self, harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]
    ) -> None:
        client, _memory, _feedback, _llm = harness

        response = client.get("/admin/feedback/pending", headers={"X-Admin-Token": "nope"})

        assert response.status_code == 403

    def test_store_outage_maps_to_typed_503(self, tmp_path: Path) -> None:
        class DeadStore(InMemoryFeedbackStore):
            def stats(self) -> dict[str, int]:
                msg = "connection refused"
                raise RuntimeError(msg)

        app = create_app(
            settings=Settings(feedback_admin_token=ADMIN_TOKEN),
            config_path=flags_on_config(tmp_path),
            store=DemoStore(),
            knowledge_query=canned_query,
            llm=WiretapLLM(),
            memory=InMemoryMemoryStore(),
            feedback=DeadStore(),
        )
        with TestClient(app) as dead_client:
            response = dead_client.get(
                "/admin/feedback/stats", headers={"X-Admin-Token": ADMIN_TOKEN}
            )

        assert response.status_code == 503
        body: dict[str, object] = response.json()
        assert body["errorType"] == "FEEDBACK_STORE_UNAVAILABLE"

    def test_fail_closed_when_no_token_configured(self, tmp_path: Path) -> None:
        app = create_app(
            settings=Settings(),
            config_path=flags_on_config(tmp_path),
            store=DemoStore(),
            knowledge_query=canned_query,
            llm=WiretapLLM(),
            memory=InMemoryMemoryStore(),
            feedback=InMemoryFeedbackStore(),
        )
        with TestClient(app) as client:
            response = client.get("/admin/feedback/pending", headers={"X-Admin-Token": "anything"})

        assert response.status_code == 403


class TestFlywheelLoop:
    def test_capture_approve_cache_miss_and_retrieval(
        self,
        harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM],
    ) -> None:
        client, _memory, feedback, llm = harness
        knowledge = KnowledgeStore(query=canned_query)

        data = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
        ).json()["data"]
        client.post("/v1/feedback", json={"historyId": data["historyId"], "rating": "down"})

        approve_response = client.post(
            "/admin/feedback/1/approve",
            json={"reviewer": "owner"},
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )

        assert approve_response.status_code == 200
        body: dict[str, object] = approve_response.json()
        assert body["status"] == "Success"

        assert llm.embedded == ["average rpm?"]
        example = feedback.examples[1]
        assert example["status"] == "approved"

        knowledge.load("demo")
        before = KnowledgeStore.metrics()["cache_invalidations"]

        approve(feedback, llm, "m", feedback.list_by_status(("approved",))[0].id, "again")

        assert KnowledgeStore.metrics()["cache_invalidations"] == before + 1

    def test_correction_auto_mines_auto_pending(self, tmp_path: Path) -> None:
        memory = InMemoryMemoryStore()
        feedback = InMemoryFeedbackStore()

        class CorrectingLLM:
            def __init__(self) -> None:
                self.inner: WiretapLLM = WiretapLLM()

            def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
                if "user corrects" in user.lower() and "ROWS:" not in user:
                    sql = (
                        "SELECT max(toFloat64OrNull(value)) "
                        "FROM demo_telemetry WHERE key = 'engine.rpm'"
                    )
                    return GenResult(
                        sql=sql, raw=f"```sql\n{sql}\n```", prompt_tokens=1, completion_tokens=1
                    )
                return self.inner.generate(system, user, temperature=temperature)

            def embed(self, texts: Sequence[str]) -> list[list[float]]:
                return self.inner.embed(texts)

        config = DEFAULT_CONFIG_PATH.read_text()
        config = config.replace("feedback_capture: false", "feedback_capture: true")
        config = config.replace("correction_loop: false", "correction_loop: true")
        path = tmp_path / "flywheel.yaml"
        path.write_text(config)
        app = create_app(
            settings=Settings(feedback_admin_token=ADMIN_TOKEN),
            config_path=path,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=CorrectingLLM(),
            memory=memory,
            feedback=feedback,
        )
        with TestClient(app) as client:
            first = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            ).json()["data"]
            client.post(
                "/v1/query/sql",
                json={
                    "tenant": "demo",
                    "query": "no, that is wrong",
                    "sessionId": first["sessionId"],
                },
            )

        rows = feedback.list_by_status(("auto_pending",))
        assert len(rows) == 1
        assert rows[0].corrected_sql is not None
        assert "max(" in rows[0].corrected_sql

    def test_golden_eval_export_shape(
        self, harness: tuple[TestClient, InMemoryMemoryStore, InMemoryFeedbackStore, WiretapLLM]
    ) -> None:
        client, _memory, feedback, llm = harness

        approve(feedback, llm, "m", feedback.insert(insert()), "owner")

        response = client.get("/admin/feedback/golden-eval", headers={"X-Admin-Token": ADMIN_TOKEN})

        class GoldenView(BaseModel):
            question: str
            sql: str
            tenant: str

        envelope = Envelope[list[GoldenView]].model_validate_json(response.content)
        assert envelope.status == "Success"
        rows = envelope.data
        assert rows is not None
        assert len(rows) == 1
        assert rows[0].tenant == "demo"


class TestQuerySuccessImport:
    def test_success_shape_untouched(self) -> None:
        outcome = QuerySuccess(
            sql="SELECT 1",
            rows=[],
            row_count=0,
            summary="s",
            resolved_keys=(),
            repairs_applied=(),
            execution_time_ms=0.0,
            prompt_tokens=0,
            completion_tokens=0,
        )

        assert outcome.supersedes_id is None
