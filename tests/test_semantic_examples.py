"""Semantic few-shot retrieval tests: threshold, fail-open, flag-off."""

from collections.abc import Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from api.query import QueryResponseData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings
from knowledge.store import KnowledgeStore
from llm.client import GenResult
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_knowledge_store import canned_query
from tests.test_pipeline_e2e import DemoStore

DEMO_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"
# S1 add-limit bounds unbounded telemetry selects post-validation, so the
# executed/returned form carries LIMIT 1000 while generators emit DEMO_SQL.
EXPECTED_SQL = f"{DEMO_SQL} LIMIT 1000"


def flags_on(tmp_path: Path, flag: str) -> Path:
    """defaults.yaml copy with one flag flipped on."""
    base = Path(DEFAULT_CONFIG_PATH).read_text()
    tuned = base.replace(f"{flag}: false", f"{flag}: true")
    path = tmp_path / "on.yaml"
    path.write_text(tuned)
    return path


class SemanticQuery:
    """Executor capturing the semantic SELECT and answering with one example."""

    def __init__(self, similarity: float) -> None:
        self.similarity: float = similarity
        self.semantic_selects: list[str] = []
        self.usage_updates: list[str] = []

    def __call__(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        if "similarity" in sql.lower() or ("<=>" in sql and "approved" in sql):
            self.semantic_selects.append(sql)
            return [
                (1, "average rpm of truck-102?", DEMO_SQL, "", "telemetry", "", self.similarity)
            ]
        if "use_count" in sql.lower():
            self.usage_updates.append(sql)
            return []
        return canned_query(sql, params)


class VectorLLM:
    """LLM embedding once per call; answers demo SQL; counts embed calls."""

    def __init__(self) -> None:
        self.embed_calls: int = 0

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        if "QUESTION:" in user and "ROWS:" not in user:
            return GenResult(
                sql=DEMO_SQL, raw=f"```sql\n{DEMO_SQL}\n```", prompt_tokens=5, completion_tokens=5
            )
        return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class TestSemanticRetrieval:
    def test_semantic_examples_flow_through_prompt(self, tmp_path: Path) -> None:
        config = flags_on(tmp_path, "semantic_examples")
        executor = SemanticQuery(0.9)
        llm = VectorLLM()
        app = create_app(
            settings=Settings(),
            config_path=config,
            store=DemoStore(),
            knowledge_query=executor,
            llm=llm,
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "how fast did it rev yesterday?"}
            )

        envelope = Envelope[QueryResponseData].model_validate_json(response.content)
        assert envelope.data is not None
        assert envelope.data.sql == EXPECTED_SQL
        assert len(executor.semantic_selects) == 1
        assert len(executor.usage_updates) == 1
        assert llm.embed_calls == 1

    def test_below_threshold_returns_no_examples(self, tmp_path: Path) -> None:
        config = flags_on(tmp_path, "semantic_examples")
        executor = SemanticQuery(0.3)
        llm = VectorLLM()
        app = create_app(
            settings=Settings(),
            config_path=config,
            store=DemoStore(),
            knowledge_query=executor,
            llm=llm,
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            client.post("/v1/query/sql", json={"tenant": "demo", "query": "how fast did it rev?"})

        assert executor.semantic_selects
        assert executor.usage_updates == []

    def test_store_failure_fails_open_to_keyword(self, tmp_path: Path) -> None:
        config = flags_on(tmp_path, "semantic_examples")
        llm = VectorLLM()

        class BrokenSemantic:
            def __call__(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
                if "<=>" in sql:
                    msg = "vector index unavailable"
                    raise RuntimeError(msg)
                return canned_query(sql, params)

        app = create_app(
            settings=Settings(),
            config_path=config,
            store=DemoStore(),
            knowledge_query=BrokenSemantic(),
            llm=llm,
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            )

        envelope = Envelope[QueryResponseData].model_validate_json(response.content)
        assert envelope.data is not None
        assert envelope.data.sql == EXPECTED_SQL

    def test_flag_off_issues_zero_embed_calls(self) -> None:
        llm = VectorLLM()
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=canned_query,
            llm=llm,
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})

        assert llm.embed_calls == 0

    def test_empty_example_registry_skips_semantics(self, tmp_path: Path) -> None:
        config = flags_on(tmp_path, "semantic_examples")
        executor = SemanticQuery(0.9)

        def no_examples(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            if "sql_agent_sql_examples" in sql:
                return []
            return canned_query(sql, params)

        app = create_app(
            settings=Settings(),
            config_path=config,
            store=DemoStore(),
            knowledge_query=no_examples,
            llm=VectorLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            client.post("/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"})

        assert executor.semantic_selects == []


class TestKnowledgeSemantic:
    def test_fetch_filters_by_threshold_and_bumps_usage(self) -> None:
        captured: list[str] = []

        def executor(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
            captured.append(sql)
            if "<=>" in sql:
                return [(4, "average rpm?", "SELECT 1", "", "telemetry", "", 0.91)]
            return []

        store = KnowledgeStore(query=executor)

        hits = store.fetch_semantic_examples("demo", [0.1], 0.85, 2)

        assert [example.question for example in hits] == ["average rpm?"]
        assert any("use_count" in sql for sql in captured)
