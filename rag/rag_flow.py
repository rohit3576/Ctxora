"""RAG retrieval + grounded answering + structured incident advice."""

import json
import logging
from collections.abc import Sequence
from typing import Final

from pydantic import TypeAdapter

from config.settings import RagConfig
from llm.client import LLMClient
from rag.contracts import RagFilters, RagStore, RetrievedChunk
from rag.rewrite import rewrite_query

_logger = logging.getLogger("ctxora.rag")

_ADVICE_ADAPTER: Final = TypeAdapter(dict[str, object])
_ANSWER_SYSTEM = (
    "You answer questions strictly from the provided context chunks. "
    "Cite the document and page for every claim. If the context does not "
    "contain the answer, say exactly: NO_GROUNDED_ANSWER. Never invent facts."
)


class UngroundedError(Exception):
    """The retrieved context cannot answer the question."""

    def __init__(self, detail: str = "no grounded answer in retrieved context") -> None:
        """Carry the refusal detail."""
        self.detail: str = detail
        super().__init__(detail)


def retrieve(
    store: RagStore,
    llm: LLMClient,
    config: RagConfig,
    tenant: str,
    question: str,
    recent_turns: Sequence[str] = (),
    filters: RagFilters | None = None,
) -> list[RetrievedChunk]:
    """Embed the (session-rewritten when enabled) question and search scopes.

    Rewrite fires only when the flag is on AND turns exist: the stateless
    path never pays an extra LLM call. filters, when set, restrict search
    to documents whose metadata contains every constraint.
    """
    query = question
    if config.query_rewrite and recent_turns:
        query = rewrite_query(llm, question, recent_turns[-config.rewrite_history_turns :])
    query_embedding = llm.embed([query])[0]
    return store.search(query_embedding, tenant, config.shared_scope, config.top_k, filters)


def answer_grounded(
    llm: LLMClient, question: str, chunks: list[RetrievedChunk]
) -> tuple[str, list[dict[str, str | int]]]:
    """Answer from chunks with sources; raise UngroundedError on refusal.

    Raises:
        UngroundedError: the model reports the context lacks the answer.
    """
    context = "\n\n".join(
        f"[{chunk.document} p.{chunk.page_number} | {chunk.section_title}]\n{chunk.chunk_text}"
        for chunk in chunks
    )
    result = llm.generate(
        _ANSWER_SYSTEM, f"CONTEXT:\n{context}\n\nQUESTION: {question}", temperature=0.0
    )
    if "NO_GROUNDED_ANSWER" in result.raw:
        raise UngroundedError
    sources = [{"document": chunk.document, "page": chunk.page_number} for chunk in chunks[:5]]
    return result.raw.strip(), sources


_ADVISOR_SYSTEM = (
    "You are an incident analysis assistant for telemetry systems. You receive an event "
    "description and a telemetry snapshot, plus optional maintenance context chunks. "
    "Reply with ONLY a JSON object with exactly these keys: summary, possible_causes, "
    "possible_consequences, immediate_actions, inspection_checklist, recommended_action, "
    "estimated_risk, can_continue, confidence. Lists must be arrays of short strings; "
    "estimated_risk is one of low|medium|high; can_continue is a boolean; confidence is 0..1."
)


def advise(
    store: RagStore,
    llm: LLMClient,
    config: RagConfig,
    tenant: str,
    event: str,
    telemetry: dict[str, float | int | str | bool | None],
    template: str,
) -> tuple[dict[str, object], list[dict[str, str | int]]]:
    """Produce structured incident advice grounded in any matching documents."""
    chunks = retrieve(store, llm, config, tenant, event)
    context = "\n\n".join(
        f"[{chunk.document} p.{chunk.page_number}]\n{chunk.chunk_text}" for chunk in chunks
    )
    snapshot = ", ".join(f"{key}={value}" for key, value in sorted(telemetry.items()))
    user = f"TASK: {template}\n\nEVENT: {event}\nTELEMETRY: {snapshot}" + (
        f"\n\nCONTEXT:\n{context}" if context else ""
    )
    result = llm.generate(_ADVISOR_SYSTEM, user, temperature=0.0)
    try:
        parsed: object = json.loads(result.raw)
    except json.JSONDecodeError as exc:
        detail = "advisor response was not valid JSON"
        raise UngroundedError(detail) from exc
    if not isinstance(parsed, dict):
        raise UngroundedError
    advice = _ADVICE_ADAPTER.validate_python(parsed)
    sources: list[dict[str, str | int]] = [
        {"document": chunk.document, "page": chunk.page_number} for chunk in chunks[:5]
    ]
    return advice, sources
