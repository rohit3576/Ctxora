"""RAG core tests: chunker, parsers, ingest dedupe, retrieval, answer, advisor, router."""

from collections.abc import Sequence

import pytest

from config.settings import RagConfig, RoutingConfig
from llm.client import GenResult
from rag.chunker import ChunkedPage, chunk_pages
from rag.contracts import ChunkInsert, RagStore
from rag.fake import InMemoryRagStore
from rag.ingest import IngestError, ingest
from rag.parsers import UnsupportedFormatError, parse
from rag.rag_flow import UngroundedError, advise, answer_grounded, retrieve
from routing.router import classify

MANUAL = b"""# Coolant Guidelines

The acceptable coolant temperature range is 70 to 95 degrees Celsius.
Sustained operation above 95 requires inspection.

# Battery Notes

Battery voltage below 11.8 volts indicates a failing battery.
"""


class RagLLM:
    """Deterministic embedder: [1,0] for coolant-ish text, [0,1] otherwise."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers: list[str] = answers or []
        self.embedded: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        if self.answers:
            return GenResult(sql="", raw=self.answers.pop(0), prompt_tokens=1, completion_tokens=1)
        return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            self.embedded.append(text)
            vectors.append([1.0, 0.0] if "coolant" in text.lower() else [0.0, 1.0])
        return vectors


CONFIG = RagConfig()


class TestChunker:
    def test_sections_become_chunks_with_hash(self) -> None:
        pages = (ChunkedPage(1, (("Coolant", "word " * 30),)),)

        chunks = chunk_pages(pages, chunk_size=10, chunk_overlap=2)

        assert all(chunk.section_title == "Coolant" for chunk in chunks)
        assert len(chunks) >= 3
        assert all(len(chunk.chunk_hash) == 64 for chunk in chunks)

    def test_overlap_carries_words_between_chunks(self) -> None:
        pages = (ChunkedPage(1, (("S", " ".join(f"w{i}" for i in range(20))),)),)

        chunks = chunk_pages(pages, chunk_size=10, chunk_overlap=4)

        first_words = chunks[0].chunk_text.split()
        second_words = chunks[1].chunk_text.split()
        assert second_words[:4] == first_words[-4:]


class TestParsers:
    def test_markdown_sections_split_on_headings(self) -> None:
        pages = parse("manual.md", MANUAL)

        titles = [title for _page in pages for title, _text in _page.sections]
        assert titles == ["Coolant Guidelines", "Battery Notes"]

    def test_html_extracts_headings_and_paragraphs(self) -> None:
        html = b"<html><body><h1>Range</h1><p>Keep it between 70 and 95.</p></body></html>"

        pages = parse("doc.html", html)

        assert pages[0].sections[0][0] == "Range"
        assert "70 and 95" in pages[0].sections[0][1]

    def test_unsupported_suffix_raises(self) -> None:
        with pytest.raises(UnsupportedFormatError, match="exe"):
            parse("thing.exe", b"bin")


class TestIngest:
    def test_round_trip_embeds_and_dedupes_by_hash(self) -> None:
        store = InMemoryRagStore()
        llm = RagLLM()

        first = ingest(store, llm, CONFIG, "demo", "manual.md", MANUAL, "embed-m")
        second = ingest(store, llm, CONFIG, "demo", "renamed.md", MANUAL, "embed-m")

        assert second.id == first.id
        assert first.chunk_count > 0
        assert len(llm.embedded) == first.chunk_count

    def test_empty_parse_is_typed_error(self) -> None:
        store = InMemoryRagStore()

        with pytest.raises(IngestError, match="no extractable"):
            ingest(store, RagLLM(), CONFIG, "demo", "empty.md", b"# only a heading", "m")

    def test_oversized_upload_is_typed_error(self) -> None:
        tiny = RagConfig(max_upload_mb=0)
        with pytest.raises(IngestError, match="MB"):
            ingest(InMemoryRagStore(), RagLLM(), tiny, "demo", "big.md", b"x" * 2048, "m")


class TestRetrieval:
    def test_tenant_scope_and_ranking(self) -> None:
        store = InMemoryRagStore()
        ingest(store, RagLLM(), CONFIG, "demo", "coolant.md", MANUAL, "m")

        hits = retrieve(store, RagLLM(), CONFIG, "demo", "coolant temperature range")

        assert hits
        assert hits[0].score >= hits[-1].score
        assert hits[0].document == "coolant.md"

    def test_other_tenant_documents_are_isolated(self) -> None:
        store = InMemoryRagStore()
        ingest(store, RagLLM(), CONFIG, "other", "coolant.md", MANUAL, "m")

        hits = retrieve(store, RagLLM(), CONFIG, "demo", "coolant temperature range")

        assert hits == []


class TestAnswerGrounded:
    def test_grounded_answer_carries_sources(self) -> None:
        from rag.contracts import RetrievedChunk

        chunks = [
            RetrievedChunk("manual.md", 1, "Coolant", "range is 70-95 C", 0.9),
        ]
        llm = RagLLM(answers=["The range is 70 to 95 C."])

        text, sources = answer_grounded(llm, "coolant range?", chunks)

        assert text == "The range is 70 to 95 C."
        assert sources == [{"document": "manual.md", "page": 1}]

    def test_refusal_raises_ungrounded(self) -> None:
        from rag.contracts import RetrievedChunk

        chunks = [RetrievedChunk("manual.md", 1, "X", "irrelevant", 0.1)]
        llm = RagLLM(answers=["NO_GROUNDED_ANSWER"])

        with pytest.raises(UngroundedError):
            answer_grounded(llm, "tire pressure?", chunks)


class TestAdvisor:
    def test_structured_json_with_sources(self) -> None:
        store = InMemoryRagStore()
        ingest(store, RagLLM(), CONFIG, "demo", "coolant.md", MANUAL, "m")
        verdict = (
            '{"summary": "overheating", "possible_causes": ["low coolant"], '
            '"possible_consequences": ["engine damage"], "immediate_actions": ["stop"], '
            '"inspection_checklist": ["hoses"], "recommended_action": "inspect", '
            '"estimated_risk": "high", "can_continue": false, "confidence": 0.9}'
        )
        llm = RagLLM(answers=[verdict])

        advice, sources = advise(
            store, llm, CONFIG, "demo", "coolant overheating", {"temp": 102}, "template"
        )

        assert advice["estimated_risk"] == "high"
        assert advice["can_continue"] is False
        assert sources

    def test_invalid_json_raises_ungrounded(self) -> None:
        llm = RagLLM(answers=["not json"])
        with pytest.raises(UngroundedError):
            advise(InMemoryRagStore(), llm, CONFIG, "demo", "event", {}, "template")


ROUTING = RoutingConfig(
    sql_indicators=("average", "rpm", "yesterday"),
    rag_indicators=("manual", "how do i", "specification"),
)


class TestRouter:
    def test_pure_data(self) -> None:
        assert classify("average rpm yesterday?", ROUTING).intent == "data"

    def test_pure_docs(self) -> None:
        assert classify("what does the manual say about service?", ROUTING).intent == "docs"

    def test_hybrid_when_both_hit(self) -> None:
        decision = classify("average rpm per the maintenance manual?", ROUTING)
        assert decision.intent == "hybrid"
        assert decision.data_indicators and decision.docs_indicators

    def test_no_indicators_defaults_to_data(self) -> None:
        assert classify("hello?", ROUTING).intent == "data"

    def test_word_boundary_not_substring(self) -> None:
        assert classify("manuals are heavy", ROUTING).intent == "data"


class TestProtocolConformance:
    def test_fake_satisfies_rag_store(self) -> None:
        assert isinstance(InMemoryRagStore(), RagStore)

    def test_chunk_insert_is_frozen(self) -> None:
        chunk = ChunkInsert(1, 0, "t", "text", "h", [0.0])
        attribute = "chunk_text"

        with pytest.raises(AttributeError):
            setattr(chunk, attribute, "other")
