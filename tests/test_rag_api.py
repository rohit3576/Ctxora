"""Phase 5 acceptance: upload→list→delete, RAG routing, hybrid merge, advisor."""

from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter

from api.rag import RAGAnswerData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from llm.client import GenResult
from main import create_app
from memory.contracts import Session, TurnInsert
from memory.fake import InMemoryMemoryStore
from rag.contracts import RagFilters, RetrievedChunk
from rag.fake import InMemoryRagStore
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore, demo_knowledge_query

MANUAL = b"""# Coolant Guidelines

The acceptable coolant temperature range is 70 to 95 degrees Celsius.

# Battery Notes

Battery voltage below 11.8 volts indicates a failing battery.
"""

DEMO_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"
# S1 add-limit bounds unbounded telemetry selects post-validation, so the
# executed/returned form carries LIMIT 1000 while generators emit DEMO_SQL.
EXPECTED_SQL = f"{DEMO_SQL} LIMIT 1000"


class _DocView(BaseModel):
    documentId: str
    filename: str
    totalPages: int
    chunkCount: int


class RoutingLLM:
    """Coolant-aware embedder; routes replies by system-prompt markers."""

    def __init__(self) -> None:
        self.inner: DemoFakeLLM = DemoFakeLLM()
        self.doc_reply: str = "The range is 70 to 95 degrees Celsius."
        self.rewrite_reply: str = ""  # empty default: rewrite degrades to raw question
        self.rewrite_prompts: list[str] = []  # observation recorder (documented mutation)

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        if "incident analysis" in system:
            return GenResult(sql="", raw=ADVISOR_JSON, prompt_tokens=2, completion_tokens=2)
        if "RECENT CONVERSATION" in user:
            self.rewrite_prompts.append(user)
            return GenResult(sql="", raw=self.rewrite_reply, prompt_tokens=1, completion_tokens=1)
        if "CONTEXT:" in user:
            return GenResult(sql="", raw=self.doc_reply, prompt_tokens=2, completion_tokens=2)
        return self.inner.generate(system, user, temperature=temperature)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "coolant" in t.lower() else [0.0, 1.0] for t in texts]


ADVISOR_JSON = (
    '{"summary": "coolant overheating", "possible_causes": ["low coolant"], '
    '"possible_consequences": ["engine damage"], "immediate_actions": ["stop the engine"], '
    '"inspection_checklist": ["check hoses"], "recommended_action": "inspect cooling system", '
    '"estimated_risk": "high", "can_continue": false, "confidence": 0.9}'
)


@pytest.fixture
def harness() -> Iterator[tuple[TestClient, InMemoryRagStore, RoutingLLM]]:
    rag_store = InMemoryRagStore()
    llm = RoutingLLM()
    app = create_app(
        settings=Settings(),
        config_path=DEFAULT_CONFIG_PATH,
        store=DemoStore(),
        knowledge_query=demo_knowledge_query,
        llm=llm,
        memory=InMemoryMemoryStore(),
        rag_store=rag_store,
    )
    with TestClient(app) as client:
        yield client, rag_store, llm


def upload_manual(client: TestClient, tenant: str = "demo") -> _DocView:
    """Upload the fixture manual once and return its view."""
    response = client.post(
        "/v1/documents",
        data={"tenant": tenant},
        files={"file": ("coolant.md", MANUAL, "text/markdown")},
    )
    envelope = Envelope[_DocView].model_validate_json(response.content)
    assert envelope.data is not None
    return envelope.data


class TestDocumentLifecycle:
    def test_upload_lists_and_deletes(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, rag_store, _llm = harness

        uploaded = upload_manual(client)

        assert uploaded.chunkCount > 0
        listing = Envelope[list[_DocView]].model_validate_json(
            client.get("/v1/documents", params={"tenant": "demo"}).content
        )
        assert listing.data is not None
        assert [doc.documentId for doc in listing.data] == [uploaded.documentId]

        delete = client.delete(f"/v1/documents/demo/{uploaded.documentId}")
        assert delete.status_code == 200
        assert rag_store.list_documents("demo") == []
        gone = client.delete(f"/v1/documents/demo/{uploaded.documentId}")
        assert gone.status_code == 404

    def test_unsupported_format_is_415(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        response = client.post(
            "/v1/documents",
            data={"tenant": "demo"},
            files={"file": ("virus.exe", b"bin", "application/octet-stream")},
        )

        assert response.status_code == 415

    def test_reupload_same_content_dedupes(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        first = upload_manual(client)
        second = upload_manual(client)

        assert second.documentId == first.documentId


class TestRagQuery:
    def test_grounded_answer_cites_manual_page(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        upload_manual(client)

        response = client.post(
            "/v1/rag/query",
            json={
                "tenant": "demo",
                "question": "what is the acceptable coolant temperature range?",
            },
        )

        assert response.status_code == 200
        envelope = Envelope[RAGAnswerData].model_validate_json(response.content)
        assert envelope.data is not None
        assert "70 to 95" in envelope.data.answer
        assert envelope.data.sources
        assert envelope.data.sources[0].document == "coolant.md"

    def test_no_documents_is_404(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        response = client.post(
            "/v1/rag/query", json={"tenant": "demo", "question": "coolant range?"}
        )

        assert response.status_code == 404
        body: dict[str, object] = response.json()
        assert body["errorType"] == "NO_DOCUMENTS"


class TestMetadataFilters:
    BRAKE: bytes = b"""# Brake Notes

Brake pad wear below 3 mm requires replacement.
"""

    def _upload(self, client: TestClient, filename: str, content: bytes, device_model: str) -> None:
        response = client.post(
            "/v1/documents",
            data={"tenant": "demo", "deviceModel": device_model},
            files={"file": (filename, content, "text/markdown")},
        )
        assert response.status_code == 200

    def test_filtered_query_cites_only_matching_device(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        self._upload(client, "coolant.md", MANUAL, "Sensor-A")
        self._upload(client, "brakes.md", self.BRAKE, "Sensor-B")

        response = client.post(
            "/v1/rag/query",
            json={
                "tenant": "demo",
                "question": "what is the acceptable coolant temperature range?",
                "filters": {"deviceModel": "Sensor-B"},
            },
        )

        assert response.status_code == 200
        envelope = Envelope[RAGAnswerData].model_validate_json(response.content)
        assert envelope.data is not None
        assert envelope.data.sources
        assert all(source.document == "brakes.md" for source in envelope.data.sources)

        response = client.post(
            "/v1/rag/query",
            json={
                "tenant": "demo",
                "question": "what is the acceptable coolant temperature range?",
                "filters": {"deviceModel": "Sensor-A"},
            },
        )

        assert response.status_code == 200
        envelope = Envelope[RAGAnswerData].model_validate_json(response.content)
        assert envelope.data is not None
        assert envelope.data.sources
        assert all(source.document == "coolant.md" for source in envelope.data.sources)

    def test_filter_matching_nothing_is_404(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        self._upload(client, "coolant.md", MANUAL, "Sensor-A")

        response = client.post(
            "/v1/rag/query",
            json={
                "tenant": "demo",
                "question": "what is the acceptable coolant temperature range?",
                "filters": {"deviceModel": "Sensor-Z"},
            },
        )

        assert response.status_code == 404
        body: dict[str, object] = response.json()
        assert body["errorType"] == "NO_DOCUMENTS"

    def test_upload_rejects_non_string_metadata_values(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        response = client.post(
            "/v1/documents",
            data={"tenant": "demo", "metadata": '{"fleetSize": 12}'},
            files={"file": ("coolant.md", MANUAL, "text/markdown")},
        )

        assert response.status_code == 400
        body: dict[str, object] = response.json()
        assert body["errorType"] == "INVALID_METADATA"

    def test_upload_rejects_form_and_json_key_collision(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        response = client.post(
            "/v1/documents",
            data={"tenant": "demo", "deviceModel": "A", "metadata": '{"deviceModel": "B"}'},
            files={"file": ("coolant.md", MANUAL, "text/markdown")},
        )

        assert response.status_code == 400
        body: dict[str, object] = response.json()
        assert body["errorType"] == "INVALID_METADATA"


class TestSessionFollowup:
    def _seeded_session(self, memory: InMemoryMemoryStore) -> Session:
        session = memory.create_session("demo", "coolant")
        memory.append_turn(
            TurnInsert(
                tenant="demo",
                session_id=session.id,
                nl_query="What is the acceptable coolant temperature?",
                sql=DEMO_SQL,
                data=(),
                summary="coolant range",
                token_usage=1,
            )
        )
        return session

    def _app(
        self, memory: InMemoryMemoryStore, llm: RoutingLLM, rag_store: InMemoryRagStore
    ) -> TestClient:
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=llm,
            memory=memory,
            rag_store=rag_store,
        )
        return TestClient(app)

    def test_session_turns_rewrite_followup_before_retrieval(self) -> None:
        memory = InMemoryMemoryStore()
        session = self._seeded_session(memory)
        llm = RoutingLLM()
        llm.rewrite_reply = "coolant temperature acceptable range"
        rag_store = InMemoryRagStore()
        with self._app(memory, llm, rag_store) as client:
            upload_manual(client)

            response = client.post(
                "/v1/rag/query",
                json={
                    "tenant": "demo",
                    "question": "What about it?",
                    "sessionId": session.id,
                },
            )

        assert response.status_code == 200
        envelope = Envelope[RAGAnswerData].model_validate_json(response.content)
        assert envelope.data is not None
        assert envelope.data.sources
        assert len(llm.rewrite_prompts) == 1
        assert "acceptable coolant temperature" in llm.rewrite_prompts[0]

    def test_unknown_session_answers_without_rewrite(self) -> None:
        memory = InMemoryMemoryStore()
        llm = RoutingLLM()
        rag_store = InMemoryRagStore()
        with self._app(memory, llm, rag_store) as client:
            upload_manual(client)

            response = client.post(
                "/v1/rag/query",
                json={
                    "tenant": "demo",
                    "question": "what is the acceptable coolant temperature range?",
                    "sessionId": "does-not-exist",
                },
            )

        assert response.status_code == 200
        assert llm.rewrite_prompts == []

    def test_cross_tenant_session_answers_without_rewrite(self) -> None:
        memory = InMemoryMemoryStore()
        self._seeded_session(memory)
        llm = RoutingLLM()
        llm.rewrite_reply = "should never be used"
        rag_store = InMemoryRagStore()
        with self._app(memory, llm, rag_store) as client:
            upload_manual(client, tenant="demo")

            response = client.post(
                "/v1/rag/query",
                json={
                    "tenant": "demo",
                    "question": "what is the acceptable coolant temperature range?",
                    "sessionId": memory.create_session("other", "not yours").id,
                },
            )

        assert response.status_code == 200
        assert llm.rewrite_prompts == []


class TestUnifiedRouting:
    def test_data_question_matches_sql_endpoint_outcome(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness

        unified = client.post(
            "/v1/query", json={"tenant": "demo", "query": "average rpm yesterday?"}
        )
        direct = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm yesterday?"}
        )

        unified_data = unified.json()["data"]
        direct_data = direct.json()["data"]
        assert unified.json()["intent"] == "data"
        assert unified_data["sql"] == direct_data["sql"] == EXPECTED_SQL
        assert unified_data["summary"] == direct_data["summary"]

    def test_docs_question_routes_to_rag(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        upload_manual(client)

        response = client.post(
            "/v1/query",
            json={"tenant": "demo", "query": "what does the manual say about the coolant range?"},
        )

        assert response.status_code == 200
        body: dict[str, object] = response.json()
        assert body["intent"] == "docs"
        assert "70 to 95" in str(body["data"])

    def test_hybrid_merges_rows_and_doc_actions(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        upload_manual(client)

        response = client.post(
            "/v1/query",
            json={
                "tenant": "demo",
                "query": "average rpm yesterday and what does the manual say to do on overheating?",
            },
        )

        body: dict[str, object] = response.json()
        assert body["intent"] == "hybrid"
        data = body["data"]
        assert isinstance(data, dict)
        assert data["sql"] == EXPECTED_SQL
        assert "docAnswer" in data
        assert "sources" in data


class TestOutageBoundaries:
    def test_rag_query_store_down_is_typed_503(self) -> None:
        class DeadStore(InMemoryRagStore):
            def search(
                self,
                query_embedding: list[float],
                tenant: str,
                shared_scope: str,
                top_k: int,
                filters: RagFilters | None = None,
            ) -> list[RetrievedChunk]:
                msg = "connection refused"
                raise RuntimeError(msg)

        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=RoutingLLM(),
            memory=InMemoryMemoryStore(),
            rag_store=DeadStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/rag/query", json={"tenant": "demo", "question": "coolant range?"}
            )

        assert response.status_code == 503
        body: dict[str, object] = response.json()
        assert body["errorType"] == "RAG_STORE_UNAVAILABLE"

    def test_hybrid_degrades_to_sql_when_rag_down(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, llm = harness
        upload_manual(client)

        class DeadSearch(InMemoryRagStore):
            def search(
                self,
                query_embedding: list[float],
                tenant: str,
                shared_scope: str,
                top_k: int,
                filters: RagFilters | None = None,
            ) -> list[RetrievedChunk]:
                msg = "connection refused"
                raise RuntimeError(msg)

        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=llm,
            memory=InMemoryMemoryStore(),
            rag_store=DeadSearch(),
        )
        with TestClient(app) as degraded:
            response = degraded.post(
                "/v1/query",
                json={
                    "tenant": "demo",
                    "query": "average rpm yesterday and what does the manual say?",
                },
            )

        assert response.status_code == 200
        body: dict[str, object] = response.json()
        data = body["data"]
        assert isinstance(data, dict)
        assert data["sql"] == EXPECTED_SQL
        assert "docAnswer" not in data


class TestAdvisor:
    def test_structured_ten_field_json_with_sources(
        self, harness: tuple[TestClient, InMemoryRagStore, RoutingLLM]
    ) -> None:
        client, _rag, _llm = harness
        upload_manual(client)

        response = client.post(
            "/v1/rag/advisor",
            json={
                "tenant": "demo",
                "event": "coolant temperature exceeded safe range",
                "telemetry": {"engine.coolantTemp": 102},
            },
        )

        assert response.status_code == 200
        data: dict[str, object] = response.json()["data"]
        for field in (
            "summary",
            "possible_causes",
            "possible_consequences",
            "immediate_actions",
            "inspection_checklist",
            "recommended_action",
            "estimated_risk",
            "can_continue",
            "confidence",
            "sources",
        ):
            assert field in data, f"missing {field}"
        assert data["can_continue"] is False
        source_rows = TypeAdapter(list[dict[str, str | int]]).validate_python(data["sources"])
        assert len(source_rows) > 0
