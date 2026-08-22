"""Query API: POST /v1/query/sql (sync NL->SQL endpoint)."""

import logging
from collections.abc import Callable
from typing import ClassVar

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.conversation import ConversationContext
from agent.generator import GenerationError
from agent.pipeline import (
    AgentDeps,
    QueryChitChat,
    QueryClarified,
    QueryExecutionFailed,
    QueryRejected,
    QuerySuccess,
    StageObserver,
    run_query,
)
from api.auth import TenantAuth, UnauthorizedError
from api.flow import conversation_context, record_turn, resolve_session
from api.ratelimit import TokenBucketLimiter
from api.schemas import Envelope
from feedback.contracts import FeedbackStore
from feedback.hooks import after_correction
from knowledge.store import NotOnboardedError
from llm.openai_compat import LLMError
from memory.contracts import MemoryStore
from memory.digest import DigestCache
from rag.contracts import RagStore
from rag.rag_flow import UngroundedError, answer_grounded, retrieve
from routing.router import RouteDecision, classify

_logger = logging.getLogger("querypulse.query")

_ROW_TYPE = dict[str, float | int | str | bool | None]
_HTTP_OK = 200


class SQLQueryRequest(BaseModel):
    """One natural-language question against one tenant."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(min_length=1, max_length=50)
    query: str = Field(min_length=1, max_length=2000)
    sessionId: str | None = None


class UnifiedQueryRequest(SQLQueryRequest):
    """One question routed to SQL, RAG, or both."""


class QueryResponseData(BaseModel):
    """Successful query payload (camelCase wire contract)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    sql: str
    rows: list[_ROW_TYPE]
    rowCount: int
    summary: str
    resolvedKeys: list[str]
    repairsApplied: list[str]
    executionTimeMs: float
    tokenUsage: int
    sessionId: str | None = None
    historyId: int | None = None
    assumptionNote: str | None = None
    followUpQuestions: list[str] = []


def failure_payload(status_code: int, error_type: str, message: str) -> dict[str, object]:
    """Build the Failure envelope content shared by sync and stream paths."""
    body = Envelope[None](status="Failure", message=message, data=None)
    content: dict[str, object] = body.model_dump()
    content["errorType"] = error_type
    content["statusCode"] = status_code
    return content


def failure_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Render the Failure envelope as an HTTP response."""
    return JSONResponse(
        status_code=status_code,
        content=failure_payload(status_code, error_type, message),
    )


def success_payload(
    success: QuerySuccess, session_id: str | None, history_id: int | None
) -> dict[str, object]:
    """Build the Success envelope content shared by sync and stream paths."""
    data = QueryResponseData(
        sql=success.sql,
        rows=success.rows,
        rowCount=success.row_count,
        summary=success.summary,
        resolvedKeys=list(success.resolved_keys),
        repairsApplied=list(success.repairs_applied),
        executionTimeMs=success.execution_time_ms,
        tokenUsage=success.prompt_tokens + success.completion_tokens,
        sessionId=session_id,
        historyId=history_id,
        assumptionNote=success.assumption_note,
        followUpQuestions=[],
    )
    envelope = Envelope[QueryResponseData](status="Success", message="query answered", data=data)
    content: dict[str, object] = envelope.model_dump()
    return content


def conversational_payload(
    outcome: QueryChitChat | QueryClarified, session_id: str | None
) -> dict[str, object]:
    """Envelope for chit-chat and clarification replies (no SQL produced)."""
    is_greeting_reply = isinstance(outcome, QueryChitChat)
    reply = outcome.reply if is_greeting_reply else outcome.question
    message = "greeting" if is_greeting_reply else "clarification needed"
    envelope = Envelope[None](status="Success", message=message, data=None)
    content: dict[str, object] = envelope.model_dump()
    content["reply"] = reply
    content["sessionId"] = session_id
    content["followUpQuestions"] = [] if is_greeting_reply else [reply]
    return content


def _pipeline_failure(exc: BaseException) -> tuple[dict[str, object], int]:
    """Map one pipeline exception to its (Failure content, status code)."""
    if isinstance(exc, NotOnboardedError):
        return failure_payload(422, "TENANT_NOT_ONBOARDED", str(exc)), 422
    if isinstance(exc, (GenerationError, LLMError)):
        detail = getattr(exc, "detail", str(exc))
        return failure_payload(502, "GENERATION_FAILED", detail), 502
    _logger.warning("pipeline dependency unavailable: %s", exc)
    detail = str(exc).splitlines()[0]
    return failure_payload(503, "PIPELINE_UNAVAILABLE", detail), 503


def run_recorded(
    request: SQLQueryRequest,
    deps: AgentDeps,
    memory: MemoryStore,
    on_stage: StageObserver | None = None,
    digest_cache: DigestCache | None = None,
    feedback: FeedbackStore | None = None,
) -> tuple[dict[str, object], int]:
    """Run the pipeline with session handling; return (content, status_code)."""
    resolution = resolve_session(
        memory, request.tenant, request.query, request.sessionId, deps.config
    )
    if resolution.error is not None:
        return failure_payload(400, "INVALID_SESSION", resolution.error), 400

    context = (
        conversation_context(memory, resolution.session) if resolution.session is not None else None
    )
    if digest_cache is None:
        digest_cache = DigestCache(deps.llm, deps.config.agent.digest_turn_threshold)

    try:
        outcome = run_query(
            request.query,
            request.tenant,
            deps,
            on_stage=on_stage,
            conversation=context,
            digest_cache=digest_cache,
        )
    except Exception as exc:  # noqa: BLE001 (boundary: typed failures, never a 500)
        return _pipeline_failure(exc)

    session_id = resolution.session.id if resolution.session else None
    match outcome:
        case QueryChitChat() | QueryClarified():
            return conversational_payload(outcome, session_id), 200
        case QueryRejected(errors=errors):
            message = "generated SQL failed validation: " + "; ".join(errors)
            return failure_payload(400, "SQL_VALIDATION_FAILED", message), 400
        case QueryExecutionFailed(error_kind=kind, error=error):
            status = 503 if kind == "connection" else 500
            return failure_payload(status, f"EXECUTION_{kind.upper()}", error), status
        case QuerySuccess():
            history_id = record_turn(memory, resolution, request.tenant, request.query, outcome)
            _mine_correction(feedback, deps, request, outcome, context, history_id)
            return success_payload(outcome, session_id, history_id), 200


def _mine_correction(
    feedback: FeedbackStore | None,
    deps: AgentDeps,
    request: SQLQueryRequest,
    outcome: QuerySuccess,
    context: ConversationContext | None,
    history_id: int | None,
) -> None:
    """Mine a successful correction into an auto_pending signal (non-blocking)."""
    if feedback is None or not deps.config.flags.feedback_capture:
        return
    if outcome.supersedes_id is None:
        return
    previous_sql = context.latest.sql if context is not None and context.latest else None
    try:
        after_correction(
            feedback,
            request.tenant,
            request.query,
            outcome,
            previous_sql=previous_sql,
            history_id=history_id,
        )
    except Exception as exc:  # noqa: BLE001 (boundary: mining must never break answering)
        _logger.warning("correction mining failed (non-blocking): %s", exc)


def _rag_answer(
    deps: AgentDeps, rag_store: RagStore, tenant: str, question: str
) -> dict[str, object] | None:
    """Grounded doc answer; None when unavailable or ungrounded (logged)."""
    try:
        chunks = retrieve(rag_store, deps.llm, deps.config.rag, tenant, question)
        if not chunks:
            return None
        text, sources = answer_grounded(deps.llm, question, chunks)
    except UngroundedError:
        return None
    except Exception as exc:  # noqa: BLE001 (boundary: hybrid degrades to SQL-only)
        _logger.warning("rag part degraded (non-blocking): %s", exc)
        return None
    return {
        "answer": text,
        "sources": list(sources),
    }


def build_query_router(
    deps: AgentDeps,
    memory: MemoryStore,
    feedback: FeedbackStore | None = None,
    rag_store: RagStore | None = None,
    auth: TenantAuth | None = None,
    limiter: TokenBucketLimiter | None = None,
    active_check: Callable[[str], bool] | None = None,
) -> APIRouter:
    """Build the sync query router with deps, memory, auth, and limits closed over."""

    def _gate(
        request: SQLQueryRequest, authorization: str | None
    ) -> tuple[JSONResponse | None, SQLQueryRequest]:
        """Auth + rate limit + activation.

        Returns (blocked_response_or_None, effective_request); the effective
        request carries the verified-claim tenant when auth is enforced.
        """
        effective = request
        if auth is not None:
            try:
                context = auth.resolve(request.tenant, authorization)
            except UnauthorizedError as exc:
                return failure_response(401, "UNAUTHORIZED", exc.detail), effective
            effective = request.model_copy(update={"tenant": context.tenant})
        if limiter is not None and deps.config.flags.ratelimit:
            verdict = limiter.admit(effective.tenant)
            if not verdict.allowed:
                detail = f"rate limit exceeded; retry after {verdict.retry_after_s}s"
                return failure_response(429, "RATE_LIMITED", detail), effective
        if active_check is not None:
            try:
                active = active_check(effective.tenant)
            except Exception as exc:  # noqa: BLE001 (fail-open: pipeline boundary gives typed errors)
                _logger.warning("activation check unavailable (fail-open): %s", exc)
                active = True
            if not active:
                detail = f"tenant '{effective.tenant}' is not activated"
                return failure_response(403, "TENANT_NOT_ACTIVE", detail), effective
        return None, effective

    def query_sql(
        request: SQLQueryRequest,
        authorization: str | None = Header(alias="Authorization", default=None),
    ) -> JSONResponse:
        """Run the NL->SQL pipeline for one question."""
        blocked, effective = _gate(request, authorization)
        if blocked is not None:
            return blocked
        content, status = run_recorded(effective, deps, memory, feedback=feedback)
        return JSONResponse(status_code=status, content=content)

    def unified(
        request: UnifiedQueryRequest,
        authorization: str | None = Header(alias="Authorization", default=None),
    ) -> JSONResponse:
        """Route to SQL and/or RAG by configured indicators."""
        blocked, effective = _gate(request, authorization)
        if blocked is not None:
            return blocked
        effective_unified = UnifiedQueryRequest(
            tenant=effective.tenant,
            query=effective.query,
            sessionId=effective.sessionId,
        )
        decision: RouteDecision = classify(request.query, deps.config.routing)
        rag_part: dict[str, object] | None = None
        if rag_store is not None and decision.intent in ("docs", "hybrid"):
            rag_part = _rag_answer(
                deps, rag_store, effective_unified.tenant, effective_unified.query
            )

        if decision.intent == "docs":
            if rag_part is None:
                return failure_response(
                    404,
                    "NO_GROUNDED_ANSWER",
                    "no ingested documents answer this question",
                )
            envelope = Envelope[dict[str, object]](
                status="Success", message="answer grounded", data=rag_part
            )
            docs_content: dict[str, object] = envelope.model_dump()
            docs_content["intent"] = "docs"
            return JSONResponse(status_code=200, content=docs_content)

        content, status = run_recorded(effective_unified, deps, memory, feedback=feedback)
        if decision.intent == "hybrid" and status == _HTTP_OK and rag_part is not None:
            data = content.get("data")
            if isinstance(data, dict):
                data["docAnswer"] = rag_part["answer"]
                data["sources"] = rag_part["sources"]
        content["intent"] = decision.intent
        return JSONResponse(status_code=status, content=content)

    router = APIRouter(tags=["query"])
    router.add_api_route("/v1/query/sql", query_sql, methods=["POST"])
    router.add_api_route("/v1/query", unified, methods=["POST"])
    return router
