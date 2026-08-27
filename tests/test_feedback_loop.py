"""Feedback store tests: capture, mining, promotion, decay, golden export."""

from collections.abc import Sequence
from dataclasses import replace

import pytest

from agent.pipeline import QuerySuccess
from agent.validator import SQLValidator
from config.settings import ColumnMapping
from database.dialects.clickhouse import ClickHouseDialect
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

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT  avg(value)\nFROM demo_telemetry",
            "select avg(value) from demo_telemetry",
            "SELECT AVG(value)   FROM demo_telemetry",
        ],
    )
    def test_variants_collapse_to_one_canonical_form(self, sql: str) -> None:
        canonical = normalize_sql(sql)

        assert canonical == normalize_sql("select avg(value) from demo_telemetry")

    @pytest.mark.parametrize(
        ("sql", "bare"),
        [
            ("SELECT 1 -- c", "SELECT 1"),
            ("SELECT 1 /* c */", "SELECT 1"),
        ],
    )
    def test_comment_variants_collapse(self, sql: str, bare: str) -> None:
        assert normalize_sql(sql) == normalize_sql(bare)

    @pytest.mark.parametrize(
        ("garbage", "legacy"),
        [
            ("NOT SQL AT ALL", "not sql at all"),
            ("SELECT 'unterminated", "select 'unterminated"),
        ],
    )
    def test_unparseable_input_falls_back_without_raising(self, garbage: str, legacy: str) -> None:
        # ParseError path first; TokenError (tokenizer) second — generic
        # dialect empirically raises both (probed 2026-08-24, sqlglot 27.29.0).
        assert normalize_sql(garbage) == legacy

    def test_garbage_that_parses_canonicalizes_deterministically(self) -> None:
        # 'NOT SQL $$$' parses under the GENERIC dialect (postgres TokenErrors
        # on it — generic does not); output is garbage but deterministic, which
        # is all dedupe requires. Documents the divergence from Phase 3 lock (b).
        assert normalize_sql("NOT SQL $$$") == normalize_sql("NOT SQL $$$")

    def test_ch_idiom_normalization_is_deterministic(self) -> None:
        sql = "SELECT argMax(value, timestamp) FROM demo_telemetry"

        assert normalize_sql(sql) == normalize_sql(sql)

    def test_validator_normalized_sql_stays_the_repaired_string(self) -> None:
        repaired = "SELECT avg(toFloat64OrNull(value)) FROM demo_telemetry WHERE key = 'speed'"

        result = SQLValidator(
            ClickHouseDialect(),
            ColumnMapping(
                table="demo_telemetry",
                timestamp="timestamp",
                entity_id="device_id",
                key="key",
                value="value",
            ),
            ("demo_telemetry",),
        ).validate("SELECT avg(value) FROM demo_telemetry WHERE key = 'speed'")

        assert result.normalized_sql == repaired


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


class TestStructuralSimilarity:
    def test_alias_rename_and_column_reorder_collapse(self) -> None:
        from feedback.similarity import similar, structural_signature

        a = "SELECT t.key, avg(t.value) FROM demo_telemetry t WHERE t.key = 'rpm' GROUP BY t.key"
        b = "SELECT avg(x.value), x.key FROM demo_telemetry AS x WHERE x.key = 'rpm' GROUP BY x.key"

        assert structural_signature(a) == structural_signature(b)
        assert similar(a, b)

    def test_literal_or_aggregate_changes_stay_different(self) -> None:
        from feedback.similarity import similar

        base = "SELECT key, avg(value) FROM demo_telemetry WHERE key = 'rpm'"
        assert not similar(base, "SELECT key, avg(value) FROM demo_telemetry WHERE key = 'oil'")
        assert not similar(base, "SELECT key, max(value) FROM demo_telemetry WHERE key = 'rpm'")

    def test_unparseable_never_matches_parseable(self) -> None:
        from feedback.similarity import similar

        assert not similar("SELECT 1 FROM demo_telemetry", "GARBAGE $$$")

    def test_correction_delta_labels_roles(self) -> None:
        from feedback.similarity import correction_delta

        delta = correction_delta(
            "SELECT key, avg(value) FROM demo_telemetry WHERE key = 'rpm'",
            "SELECT key, max(value) FROM demo_telemetry "
            "WHERE key = 'rpm' AND timestamp >= now() - INTERVAL 1 DAY",
        )

        assert delta["aggregation_changes"] == ["Avg -> Max"]
        assert len(delta["added_time_windows"]) == 1
        assert "timestamp" in delta["added_time_windows"][0]

    def test_correction_delta_column_and_filter_changes(self) -> None:
        from feedback.similarity import correction_delta

        delta = correction_delta(
            "SELECT key FROM demo_telemetry WHERE key = 'a'",
            "SELECT key, device_id FROM demo_telemetry WHERE key = 'a' AND key = 'b'",
        )

        assert delta["added_columns"] == ["device_id"]
        assert delta["added_filters"] == ['"demo_telemetry"."key" = \'b\'']

    def test_correction_delta_unparseable_is_empty(self) -> None:
        from feedback.similarity import correction_delta

        assert correction_delta("GARBAGE $$$", "SELECT 1") == {}
        assert correction_delta("SELECT 1", "GARBAGE $$$") == {}


class TestStructuralDecay:
    def test_correction_decays_structurally_identical_example(self) -> None:
        feedback = InMemoryFeedbackStore()
        feedback.upsert_approved_example(
            "demo",
            "avg rpm question",
            "SELECT t.key, avg(t.value) FROM demo_telemetry t GROUP BY t.key",
            [0.5, 0.5],
            0,
            "embed-m",
        )
        promoted = next(iter(feedback.examples))

        corrected = "SELECT x.key, avg(x.value) FROM demo_telemetry x GROUP BY x.key"
        after_correction(
            feedback,
            "demo",
            "avg rpm question",
            success(sql=corrected),
            previous_sql=corrected,
            history_id=7,
            structural=True,
        )

        assert feedback.examples[promoted]["status"] == "review"


class TestStructuralPromotionDedupe:
    def test_alias_renamed_duplicate_rejected_at_promote_time(self) -> None:
        feedback = InMemoryFeedbackStore()
        first = feedback.insert(
            FeedbackInsert(
                tenant="demo",
                nl_query="average rpm?",
                generated_sql="SELECT t.key, avg(t.value) FROM demo_telemetry t GROUP BY t.key",
                feedback_type="positive",
                status="pending",
            )
        )
        approved = approve(feedback, EmbeddingLLM(), "embed-m", first, "admin")
        assert approved.promoted

        second = feedback.insert(
            FeedbackInsert(
                tenant="demo",
                nl_query="mean rpm per key?",
                generated_sql=(
                    "SELECT x.key, avg(x.value) FROM demo_telemetry AS x GROUP BY x.key"
                ),
                feedback_type="positive",
                status="pending",
            )
        )
        duplicate = approve(feedback, EmbeddingLLM(), "embed-m", second, "admin", structural=True)

        assert duplicate.approved
        assert not duplicate.promoted
        assert "structural duplicate" in duplicate.action
        examples = feedback.approved_example_sqls("demo")
        assert len(examples) == 1

    def test_structural_off_promotes_duplicates_as_before(self) -> None:
        feedback = InMemoryFeedbackStore()
        first = feedback.insert(
            FeedbackInsert(
                tenant="demo",
                nl_query="average rpm?",
                generated_sql="SELECT t.key, avg(t.value) FROM demo_telemetry t GROUP BY t.key",
                feedback_type="positive",
                status="pending",
            )
        )
        approve(feedback, EmbeddingLLM(), "embed-m", first, "admin")
        second = feedback.insert(
            FeedbackInsert(
                tenant="demo",
                nl_query="mean rpm per key?",
                generated_sql=(
                    "SELECT x.key, avg(x.value) FROM demo_telemetry AS x GROUP BY x.key"
                ),
                feedback_type="positive",
                status="pending",
            )
        )
        result = approve(feedback, EmbeddingLLM(), "embed-m", second, "admin")

        assert result.promoted
        assert len(feedback.approved_example_sqls("demo")) == 2


class TestDeltaMiningNonBlocking:
    def test_sabotaged_diff_path_never_breaks_mining(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def _boom(previous: str, corrected: str) -> dict[str, list[str]]:
            raise RuntimeError("diff exploded")

        import feedback.hooks as feedback_hooks

        monkeypatch.setattr(feedback_hooks, "correction_delta", _boom)
        feedback = InMemoryFeedbackStore()

        after_correction(
            feedback,
            "demo",
            "average rpm?",
            success(sql="SELECT key FROM demo_telemetry"),
            previous_sql="SELECT key, value FROM demo_telemetry",
            history_id=9,
            structural=True,
        )

        rows = feedback.list_by_status(("auto_pending",))
        assert len(rows) == 1
        assert rows[0].correction_delta is None

    def test_mined_delta_persisted_on_row(self) -> None:
        feedback = InMemoryFeedbackStore()
        after_correction(
            feedback,
            "demo",
            "average rpm?",
            success(sql="SELECT key, max(value) FROM demo_telemetry WHERE key = 'rpm'"),
            previous_sql="SELECT key, avg(value) FROM demo_telemetry WHERE key = 'rpm'",
            history_id=11,
            structural=True,
        )

        (row,) = feedback.list_by_status(("auto_pending",))
        assert row.correction_delta is not None
        assert row.correction_delta["aggregation_changes"] == ["Avg -> Max"]
