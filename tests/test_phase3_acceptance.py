"""Phase 3 acceptance: correction, follow-up, assume-first, greeting, parity."""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import TypeAdapter

from api.query import QueryResponseData
from api.schemas import Envelope
from config.settings import DEFAULT_CONFIG_PATH, Settings, load_app_config
from llm.client import GenResult
from main import create_app
from memory.fake import InMemoryMemoryStore
from tests.test_pipeline_e2e import DemoFakeLLM, DemoStore, demo_knowledge_query

DEMO_SQL = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"
# S1 add-limit bounds unbounded telemetry selects post-validation, so the
# executed/returned form carries LIMIT 1000 while generators emit DEMO_SQL.
EXPECTED_SQL = f"{DEMO_SQL} LIMIT 1000"
MAX_SQL = "SELECT max(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'engine.rpm'"
EXPECTED_MAX_SQL = f"{MAX_SQL} LIMIT 1000"

FLAGS_ON: dict[str, bool] = {
    "correction_loop": True,
    "assume_first": True,
    "session_digest": True,
    "streaming": True,
    "semantic_examples": False,
    "feedback_capture": False,
    "followup": True,
    "greeting": True,
}


def flags_on_config(tmp_path: Path) -> Path:
    """defaults.yaml copy with all Phase-3 flags on."""
    tuned = DEFAULT_CONFIG_PATH.read_text()
    for key, value in FLAGS_ON.items():
        tuned = _set_flag(tuned, key, value)
    path = tmp_path / "flags_on.yaml"
    path.write_text(tuned)
    return path


def _set_flag(yaml_text: str, key: str, value: bool) -> str:
    """Flip one flags.<key> line."""
    lines = yaml_text.splitlines()
    in_flags = False
    for i, line in enumerate(lines):
        if line.strip() == "flags:":
            in_flags = True
            continue
        if in_flags:
            if line.startswith("  ") and ":" in line:
                name = line.strip().split(":")[0]
                if name == key:
                    lines[i] = f"  {key}: {str(value).lower()}"
            else:
                in_flags = False
    return "\n".join(lines) + "\n"


class CorrectableLLM:
    """Demo LLM that answers correction merges with a MAX query at temp != 0."""

    def __init__(self) -> None:
        self.inner: DemoFakeLLM = DemoFakeLLM()
        self.generation_temperatures: list[float] = []
        self.classifier_raw: str = (
            '{"is_correction": true, "corrected_question": "maximum rpm of truck-102 yesterday?"}'
        )

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        lowered = user.lower()
        if ("user corrects" in lowered or "corrected" in lowered) and "ROWS:" not in user:
            self.generation_temperatures.append(temperature)
            return GenResult(
                sql=MAX_SQL, raw=f"```sql\n{MAX_SQL}\n```", prompt_tokens=8, completion_tokens=9
            )
        if "classify messages" in system.lower():
            verdict = (
                '{"is_correction": false, "corrected_question": ""}'
                if "no data" in lowered
                else self.classifier_raw
            )
            return GenResult(sql="", raw=verdict, prompt_tokens=3, completion_tokens=3)
        if "QUESTION:" in user and "ROWS:" not in user:
            self.generation_temperatures.append(temperature)
        return self.inner.generate(system, user, temperature=temperature)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.inner.embed(texts)


@pytest.fixture
def flags_on(tmp_path: Path) -> Iterator[tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]]:
    memory = InMemoryMemoryStore()
    llm = CorrectableLLM()
    app = create_app(
        settings=Settings(),
        config_path=flags_on_config(tmp_path),
        store=DemoStore(),
        knowledge_query=demo_knowledge_query,
        llm=llm,
        memory=memory,
    )
    with TestClient(app) as client:
        yield client, memory, llm


def typed_data(response: Response) -> QueryResponseData:
    """Parse a success response into the typed payload."""
    envelope = Envelope[QueryResponseData].model_validate_json(response.content)
    assert envelope.data is not None
    return envelope.data


class TestCorrectionAcceptance:
    def test_correction_regenerates_at_03_with_lineage(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        client, memory, llm = flags_on
        first = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm of truck-102 yesterday?"}
        ).json()["data"]

        second = client.post(
            "/v1/query/sql",
            json={
                "tenant": "demo",
                "query": "no, that is wrong",
                "sessionId": first["sessionId"],
            },
        ).json()["data"]

        assert second["sql"] == EXPECTED_MAX_SQL
        assert llm.generation_temperatures[-1] == pytest.approx(0.3)

        page = memory.list_history("demo")
        turns = page[0].turns
        assert turns[-1].supersedes_id == first["historyId"]

    def test_second_consecutive_complaint_clarifies(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        client, _memory, _llm = flags_on
        first = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
        ).json()["data"]
        client.post(
            "/v1/query/sql",
            json={"tenant": "demo", "query": "no wrong", "sessionId": first["sessionId"]},
        )
        third = client.post(
            "/v1/query/sql",
            json={"tenant": "demo", "query": "still wrong", "sessionId": first["sessionId"]},
        )

        assert third.status_code == 200
        body: dict[str, object] = third.json()
        assert body["message"] == "clarification needed"
        follow_ups = TypeAdapter(list[str]).validate_python(body["followUpQuestions"])
        assert len(follow_ups) > 0
        assert body["data"] is None

    def test_no_data_guard_is_not_a_correction(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        client, _memory, llm = flags_on
        first = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
        ).json()["data"]

        response = client.post(
            "/v1/query/sql",
            json={
                "tenant": "demo",
                "query": "there is no data for speed",
                "sessionId": first["sessionId"],
            },
        )

        data = response.json()["data"]
        assert data is not None
        assert data["sql"] == EXPECTED_SQL
        assert llm.generation_temperatures[-1] == pytest.approx(0.0)


class TestFollowupAcceptance:
    def test_entity_followup_resolves_metric(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        client, _memory, _llm = flags_on
        first = client.post(
            "/v1/query/sql", json={"tenant": "demo", "query": "average rpm of truck-102?"}
        ).json()["data"]

        second = client.post(
            "/v1/query/sql",
            json={
                "tenant": "demo",
                "query": "what about truck-103?",
                "sessionId": first["sessionId"],
            },
        )

        assert second.status_code == 200


class TestAssumeFirstAcceptance:
    def test_untimed_question_answers_with_assumption_note(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        client, _memory, _llm = flags_on
        response = client.post("/v1/query/sql", json={"tenant": "demo", "query": "average speed"})

        data = typed_data(response)
        assert data.assumptionNote is not None
        assert "today" in data.assumptionNote
        assert "fleet" in data.assumptionNote.lower()


class TestGreetingAcceptance:
    def test_greeting_short_circuits_without_sql(
        self, flags_on: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM]
    ) -> None:
        response = client_post(flags_on, "hello!")

        assert response.status_code == 200
        body: dict[str, object] = response.json()
        assert body["message"] == "greeting"
        assert body["data"] is None
        assert isinstance(body["reply"], str)


def client_post(
    fixture: tuple[TestClient, InMemoryMemoryStore, CorrectableLLM], query: str
) -> Response:
    """POST one demo-tenant question."""
    client = fixture[0]
    return client.post("/v1/query/sql", json={"tenant": "demo", "query": query})


class TestFlagsOffParity:
    def test_flags_off_reproduces_phase2_contract(self, tmp_path: Path) -> None:
        app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=DemoFakeLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(app) as client:
            first = client.post(
                "/v1/query/sql", json={"tenant": "demo", "query": "average rpm?"}
            ).json()["data"]

        assert first["sql"] == EXPECTED_SQL
        assert first["assumptionNote"] is None
        assert first["followUpQuestions"] == []

        strict_app = create_app(
            settings=Settings(),
            config_path=DEFAULT_CONFIG_PATH,
            store=DemoStore(),
            knowledge_query=demo_knowledge_query,
            llm=DemoFakeLLM(),
            memory=InMemoryMemoryStore(),
        )
        with TestClient(strict_app) as strict:
            reply = strict.post("/v1/query/sql", json={"tenant": "demo", "query": "hello!"})
        assert reply.status_code == 200
        hello: dict[str, object] = reply.json()
        assert hello["message"] == "query answered"

    def test_shipped_flags_default_off(self) -> None:
        config = load_app_config(DEFAULT_CONFIG_PATH)

        assert config.flags.correction_loop is False
        assert config.flags.assume_first is False
        assert config.flags.session_digest is False
        assert config.flags.followup is False
        assert config.flags.greeting is False
