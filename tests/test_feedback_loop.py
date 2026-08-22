"""Feedback store tests: capture, mining, promotion, decay, golden export."""

from collections.abc import Sequence
from dataclasses import replace

import pytest

from agent.pipeline import QuerySuccess
from feedback.capture import HistoryNotFoundError, capture
from feedback.contracts import FeedbackInsert, FeedbackStore
from feedback.fake import InMemoryFeedbackStore
from feedback.hooks import after_correction, normalize_sql
from feedback.promotion import approve, auto_promote_positive, decay_matches, reject
from knowledge.store import KnowledgeStore
from llm.client import GenResult
from memory.fake import InMemoryMemoryStore
from tests.test_knowledge_store import canned_query


def insert(tenant: str = "demo", **overrides: object) -> FeedbackInsert:
    """One negative signal with comment by default."""
    base = FeedbackInsert(
        tenant=tenant,
        nl_query="average rpm?",
        generated_sql="SELECT 1",
        feedback_type="negative",
        user_comment="used max instead",
        corrected_sql="SELECT 2",
    )
    return replace(base, **overrides) if overrides else base


def success(supersedes: int | None = None, sql: str = "SELECT max(1)") -> QuerySuccess:
    """One successful pipeline outcome."""
    return QuerySuccess(
        sql=sql,
        rows=[],
        row_count=0,
        summary="ok",
        resolved_keys=("engine.rpm",),
        repairs_applied=(),
        execution_time_ms=1.0,
        prompt_tokens=1,
        completion_tokens=1,
        supersedes_id=supersedes,
    )


class EmbeddingLLM:
    """Fake LLM recording embedded texts."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[0.5, 0.5] for _ in texts]

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)


class TestProtocolConformance:
    def test_fake_satisfies_feedback_store(self) -> None:
        assert isinstance(InMemoryFeedbackStore(), FeedbackStore)


class TestNormalizeSql:
    def test_whitespace_and_case_insensitive(self) -> None:
        assert normalize_sql("SELECT  a\nFROM t") == normalize_sql("select a from t")

    def test_different_sql_differs(self) -> None:
        assert normalize_sql("SELECT 1") != normalize_sql("SELECT 2")


class TestMining:
    def test_correction_creates_auto_pending_row(self) -> None:
        feedback = InMemoryFeedbackStore()

        after_correction(
            feedback, "demo", "no wrong", success(), previous_sql="SELECT old", history_id=9
        )

        rows = feedback.list_by_status(("auto_pending",))
        assert len(rows) == 1
        assert rows[0].corrected_sql == "SELECT max(1)"
        assert rows[0].history_id == 9

    def test_no_history_id_skips_mining(self) -> None:
        feedback = InMemoryFeedbackStore()

        after_correction(
            feedback, "demo", "no wrong", success(), previous_sql="SELECT old", history_id=None
        )

        assert feedback.stats() == {}

    def test_decay_demotes_matching_approved_example(self) -> None:
        feedback = InMemoryFeedbackStore()
        llm = EmbeddingLLM()
        feedback_id = feedback.insert(insert())
        approve(feedback, llm, "model-x", feedback_id, "admin")
        example_id, example_sql = feedback.approved_example_sqls("demo")[0]
        assert example_sql == "SELECT 2"

        demoted = decay_matches(feedback, "demo", "  SELECT   2 ")

        assert demoted == 1
        assert feedback.approved_example_sqls("demo") == []
        example = feedback.examples[example_id]
        assert example["status"] == "review"
        assert example["corrections_after_use"] == 1


class TestApprove:
    def test_approve_promotes_example_with_embedding_and_invalidates_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        KnowledgeStore.reset_state()
        feedback = InMemoryFeedbackStore()
        llm = EmbeddingLLM()
        feedback_id = feedback.insert(insert())
        KnowledgeStore(query=canned_query).load("demo")
        before = KnowledgeStore.metrics()["cache_invalidations"]

        result = approve(feedback, llm, "text-embed", feedback_id, "admin")
        after = KnowledgeStore.metrics()["cache_invalidations"]

        assert result.approved is True
        assert result.promoted is True
        assert llm.embedded == ["average rpm?"]
        example = feedback.examples[1]
        assert example["status"] == "approved"
        assert example["embedding"] == [0.5, 0.5]
        assert example["provenance_feedback_id"] == feedback_id
        assert after == before + 1
        assert KnowledgeStore.metrics()["cache_misses"] == 1

    def test_reject_keeps_examples_untouched(self) -> None:
        feedback = InMemoryFeedbackStore()
        feedback_id = feedback.insert(insert())

        result = reject(feedback, feedback_id, "admin")

        assert result.rejected
        assert feedback.examples == {}
        row = feedback.get(feedback_id)
        assert row is not None
        assert row.status == "rejected"

    def test_rejected_row_is_not_approvable(self) -> None:
        feedback = InMemoryFeedbackStore()
        llm = EmbeddingLLM()
        feedback_id = feedback.insert(insert())
        reject(feedback, feedback_id, "admin")

        result = approve(feedback, llm, "m", feedback_id, "admin")

        assert result.approved is False

    def test_auto_promote_positive_only_touches_positive(self) -> None:
        feedback = InMemoryFeedbackStore()
        llm = EmbeddingLLM()
        feedback.insert(insert(nl_query="good one", feedback_type="positive"))
        feedback.insert(insert(nl_query="bad one", feedback_type="negative"))

        promoted = auto_promote_positive(feedback, llm, "m", "admin")

        assert promoted == 1
        assert len(feedback.examples) == 1


class TestGoldenExport:
    def test_golden_rows_carry_question_sql_tenant(self) -> None:
        feedback = InMemoryFeedbackStore()
        llm = EmbeddingLLM()
        approve(feedback, llm, "m", feedback.insert(insert()), "admin")

        rows = feedback.golden_rows()

        assert len(rows) == 1
        assert rows[0].question == "average rpm?"
        assert rows[0].sql == "SELECT 2"
        assert rows[0].tenant == "demo"


class TestCaptureIsolation:
    def test_capture_requires_existing_history(self) -> None:
        memory = InMemoryMemoryStore()

        with pytest.raises(HistoryNotFoundError):
            capture(InMemoryFeedbackStore(), memory, 999, "up")
