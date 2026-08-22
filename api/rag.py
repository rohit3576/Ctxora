"""RAG API: grounded Q&A and structured incident advice."""

import logging
from typing import ClassVar

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.schemas import Envelope
from config.settings import RagConfig
from llm.client import LLMClient
from rag.contracts import RagStore
from rag.rag_flow import UngroundedError, advise, answer_grounded, retrieve

_logger = logging.getLogger("datamind.rag_api")

_ROW_TYPE = dict[str, float | int | str | bool | None]


class RAGQueryRequest(BaseModel):
    """One documentation question."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=1, max_length=2000)


class SourceView(BaseModel):
    """One cited document page."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    document: str
    page: int


class RAGAnswerData(BaseModel):
    """Grounded answer payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    answer: str
    sources: list[SourceView]


class AdvisorRequest(BaseModel):
    """One incident to analyze."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(min_length=1, max_length=50)
    event: str = Field(min_length=1, max_length=4000)
    telemetry: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class AdvisorData(BaseModel):
    """Structured incident advice payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")

    summary: str
    possible_causes: list[str]
    possible_consequences: list[str]
    immediate_actions: list[str]
    inspection_checklist: list[str]
    recommended_action: str
    estimated_risk: str
    can_continue: bool
    confidence: float
    sources: list[SourceView]


def _failure(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Render one Failure envelope."""
    body = Envelope[None](status="Failure", message=message, data=None)
    content: dict[str, object] = body.model_dump()
    content["errorType"] = error_type
    content["statusCode"] = status_code
    return JSONResponse(status_code=status_code, content=content)


def build_rag_router(rag_store: RagStore, llm: LLMClient, config: RagConfig) -> APIRouter:
    """Build the RAG query + advisor router with deps closed over."""

    def query(request: RAGQueryRequest) -> JSONResponse:
        """Answer one documentation question with cited sources."""
        try:
            chunks = retrieve(rag_store, llm, config, request.tenant, request.question)
        except Exception as exc:  # noqa: BLE001 (boundary: store outage -> typed 503)
            _logger.warning("rag store unavailable: %s", exc)
            return _failure(503, "RAG_STORE_UNAVAILABLE", str(exc).splitlines()[0])
        if not chunks:
            return _failure(404, "NO_DOCUMENTS", "no documents ingested for this tenant")
        try:
            text, sources = answer_grounded(llm, request.question, chunks)
        except UngroundedError:
            return _failure(404, "NOT_GROUNDED", "retrieved context cannot answer this question")
        data = RAGAnswerData(
            answer=text,
            sources=[
                SourceView(document=str(source["document"]), page=int(source["page"]))
                for source in sources
            ],
        )
        envelope = Envelope[RAGAnswerData](status="Success", message="answer grounded", data=data)
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    def advisor(request: AdvisorRequest) -> JSONResponse:
        """Produce structured advice for one incident."""
        try:
            advice, sources = advise(
                rag_store,
                llm,
                config,
                request.tenant,
                request.event,
                request.telemetry,
                config.advisor_template,
            )
            payload: dict[str, object] = dict(advice)
            payload["sources"] = [
                {"document": str(source["document"]), "page": int(source["page"])}
                for source in sources
            ]
            data = AdvisorData.model_validate(payload)
        except UngroundedError:
            return _failure(502, "ADVISOR_PARSE_FAILED", "advisor response was not parseable")
        except Exception as exc:  # noqa: BLE001 (boundary: typed failure, never a 500)
            _logger.warning("advisor failed: %s", exc)
            return _failure(502, "ADVISOR_FAILED", str(exc).splitlines()[0])
        envelope = Envelope[AdvisorData](status="Success", message="advice produced", data=data)
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    router = APIRouter(tags=["rag"])
    router.add_api_route("/v1/rag/query", query, methods=["POST"])
    router.add_api_route("/v1/rag/advisor", advisor, methods=["POST"])
    return router
