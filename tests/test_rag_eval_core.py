"""RAG eval core: golden-set loading, hit scoring, metrics, and the fake-mode
end-to-end smoke over the real golden set (deterministic by construction).
"""

from pathlib import Path

import pytest

from rag.contracts import RetrievedChunk
from rag.fake import HashEmbedLLM, InMemoryRagStore
from tools.rag_eval import evaluate
from tools.rag_eval_core import (
    GoldenSetError,
    first_hit_rank,
    is_hit,
    load_golden,
    recall_at,
    summarize,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "rag_golden.yaml"


def _chunk(document: str, section: str, page: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        document=document, page_number=page, section_title=section, chunk_text="", score=0.5
    )


class TestGoldenSetFile:
    def test_loads_with_expected_size_and_tags(self) -> None:
        cases = load_golden(GOLDEN_PATH)

        assert len(cases) >= 30
        tag_counts: dict[str, int] = {}
        for case in cases:
            for tag in case.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        assert tag_counts["identifier"] >= 8
        assert tag_counts["paraphrase"] >= 6
        assert tag_counts["table"] >= 5
        assert tag_counts["procedure"] >= 5
        assert tag_counts["followup"] >= 3
        assert tag_counts["multi-hop"] >= 3

    def test_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "- { id: a, tenant: demo, question: q, expect: { document: d.md }, tags: [table] }\n"
            "- { id: a, tenant: demo, question: q, expect: { document: d.md }, tags: [table] }\n"
        )

        with pytest.raises(GoldenSetError, match="duplicate"):
            load_golden(bad)

    def test_rejects_unknown_tags(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "- { id: a, tenant: demo, question: q, expect: { document: d.md }, tags: [nope] }\n"
        )

        with pytest.raises(GoldenSetError, match="schema errors"):
            load_golden(bad)

    def test_rejects_both_and_neither_document_selectors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "- { id: a, tenant: demo, question: q,"
            " expect: { document: d.md, documents: [d.md] }, tags: [table] }\n"
        )
        with pytest.raises(GoldenSetError, match="schema errors"):
            load_golden(bad)


class TestHitScoring:
    def test_document_match_without_section(self) -> None:
        from tools.rag_eval_core import GoldenExpectation

        expectation = GoldenExpectation(document="manual.md")
        assert is_hit(_chunk("manual.md", "Anything"), expectation)

    def test_documents_any_match(self) -> None:
        from tools.rag_eval_core import GoldenExpectation

        expectation = GoldenExpectation(documents=["a.md", "b.md"])
        assert is_hit(_chunk("b.md", "Error Codes"), expectation)
        assert not is_hit(_chunk("c.md", "Error Codes"), expectation)

    def test_section_is_case_insensitive_substring(self) -> None:
        from tools.rag_eval_core import GoldenExpectation

        expectation = GoldenExpectation(document="m.md", section="error codes")
        assert is_hit(_chunk("m.md", "Error Codes"), expectation)
        assert not is_hit(_chunk("m.md", "Maintenance Procedure"), expectation)

    def test_first_hit_rank_is_one_based_and_none_on_miss(self) -> None:
        from tools.rag_eval_core import GoldenExpectation

        chunks = [_chunk("x.md", "A"), _chunk("m.md", "Error Codes"), _chunk("m.md", "Other")]
        assert first_hit_rank(chunks, GoldenExpectation(document="m.md")) == 2
        assert first_hit_rank(chunks, GoldenExpectation(document="z.md")) is None


class TestMetrics:
    def test_recall_at_k_counts_only_ranks_within_k(self) -> None:
        from tools.rag_eval_core import CaseResult, GoldenCase, GoldenExpectation

        def result(rank: int | None) -> CaseResult:
            case = GoldenCase(
                id=f"c{rank}",
                tenant="demo",
                question="q",
                expect=GoldenExpectation(document="m.md"),
                tags=("table",),
            )
            return CaseResult(case=case, rank=rank, top_document="m.md")

        results = [result(1), result(3), result(5), result(None)]

        assert recall_at(results, 1) == pytest.approx(0.25)
        assert recall_at(results, 3) == pytest.approx(0.5)
        assert recall_at(results, 5) == pytest.approx(0.75)

    def test_mrr_averages_reciprocal_ranks(self) -> None:
        from tools.rag_eval_core import CaseResult, GoldenCase, GoldenExpectation

        def result(rank: int | None) -> CaseResult:
            case = GoldenCase(
                id=f"c{rank}",
                tenant="demo",
                question="q",
                expect=GoldenExpectation(document="m.md"),
                tags=("table",),
            )
            return CaseResult(case=case, rank=rank, top_document="m.md")

        summary = summarize([result(1), result(4), result(None)], ks=(1, 5))

        assert summary.overall.mrr == pytest.approx((1.0 + 0.25 + 0.0) / 3)
        assert summary.overall.recall_at_k == ((1, pytest.approx(1 / 3)), (5, pytest.approx(2 / 3)))


class TestHashEmbedLLM:
    def test_satisfies_llm_client_protocol(self) -> None:
        from llm.client import LLMClient

        assert isinstance(HashEmbedLLM(), LLMClient)

    def test_deterministic_and_dimension_locked(self) -> None:
        embedder = HashEmbedLLM()

        first = embedder.embed(["error code E-302 torque"])[0]
        second = embedder.embed(["error code E-302 torque"])[0]

        assert len(first) == 1536  # rag_chunks.embedding VECTOR(1536)
        assert first == second
        assert first != embedder.embed(["something else entirely"])[0]


class TestFakeEndToEnd:
    def test_fake_mode_over_golden_set_is_deterministic(self) -> None:
        from config.settings import load_app_config
        from demo.seed_docs import HASH_EMBEDDING_MODEL, seed_documents

        cases = load_golden(GOLDEN_PATH)

        def run_once() -> list[float]:
            store = InMemoryRagStore()
            seed_documents(
                store, HashEmbedLLM(), load_app_config().rag, "demo", HASH_EMBEDDING_MODEL
            )
            results = evaluate(store, HashEmbedLLM(), cases, ks=(1, 3, 5, 10))
            return [result.rank if result.rank is not None else 0.0 for result in results]

        assert run_once() == run_once()

    def test_fake_mode_identifier_recall_beats_followup_recall(self) -> None:
        from config.settings import load_app_config
        from demo.seed_docs import HASH_EMBEDDING_MODEL, seed_documents

        cases = load_golden(GOLDEN_PATH)
        store = InMemoryRagStore()
        seed_documents(store, HashEmbedLLM(), load_app_config().rag, "demo", HASH_EMBEDDING_MODEL)
        summary = summarize(evaluate(store, HashEmbedLLM(), cases, ks=(5,)), ks=(5,))

        by_tag = {tag.label: tag for tag in summary.per_tag}
        identifier_r5 = dict(by_tag["identifier"].recall_at_k)[5]
        followup_r5 = dict(by_tag["followup"].recall_at_k)[5]

        assert identifier_r5 >= followup_r5
