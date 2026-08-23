"""Feedback admin API: review queue behind a fail-closed token gate."""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Annotated, ClassVar, ParamSpec

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from api.schemas import Envelope
from feedback.contracts import FeedbackStore
from feedback.promotion import approve, auto_promote_positive, reject
from llm.client import LLMClient

_logger = logging.getLogger("ctxora.feedback_admin")


class ReviewAction(BaseModel):
    """One reviewer decision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    reviewer: str = "admin"


class FeedbackRowView(BaseModel):
    """One review-queue row on the wire."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: int
    tenant: str
    nlQuery: str
    generatedSql: str
    feedbackType: str
    status: str
    userComment: str | None = None
    correctedSql: str | None = None


def _forbidden() -> JSONResponse:
    """Fail-closed 403 envelope."""
    body = Envelope[None](status="Failure", message="admin token required", data=None)
    content: dict[str, object] = body.model_dump()
    content["errorType"] = "FORBIDDEN"
    content["statusCode"] = 403
    return JSONResponse(status_code=403, content=content)


def _unavailable(exc: BaseException) -> JSONResponse:
    """Typed 503 when the feedback store is unreachable."""
    _logger.warning("feedback admin store unavailable: %s", exc)
    body = Envelope[None](status="Failure", message=str(exc).splitlines()[0], data=None)
    content: dict[str, object] = body.model_dump()
    content["errorType"] = "FEEDBACK_STORE_UNAVAILABLE"
    content["statusCode"] = 503
    return JSONResponse(status_code=503, content=content)


def build_feedback_admin_router(
    feedback: FeedbackStore, llm: LLMClient, admin_token: str | None, embedding_model: str
) -> APIRouter:
    """Build the token-gated review router; unset token means always 403."""
    router = APIRouter(tags=["feedback-admin"])

    p_spec = ParamSpec("p_spec")

    def _with_boundary(handler: Callable[p_spec, JSONResponse]) -> Callable[p_spec, JSONResponse]:
        """Apply the outage boundary around one route handler."""

        @wraps(handler)
        def wrapped(*args: p_spec.args, **kwargs: p_spec.kwargs) -> JSONResponse:
            try:
                return handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 (boundary: typed 503, never a 500)
                return _unavailable(exc)

        return wrapped

    def guarded(
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse | None:
        """Return the 403 response when the token does not match (fail-closed)."""
        if admin_token is None or x_admin_token != admin_token:
            return _forbidden()
        return None

    def pending(
        tenant: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """List signals awaiting review."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        rows = feedback.list_by_status(("pending", "auto_pending"), tenant=tenant, limit=limit)
        views = [
            FeedbackRowView(
                id=row.id,
                tenant=row.tenant,
                nlQuery=row.nl_query,
                generatedSql=row.generated_sql,
                feedbackType=row.feedback_type,
                status=row.status,
                userComment=row.user_comment,
                correctedSql=row.corrected_sql,
            )
            for row in rows
        ]
        envelope = Envelope[list[FeedbackRowView]](
            status="Success", message="review queue", data=views
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def stats(
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """Counts per status."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        envelope = Envelope[dict[str, int]](
            status="Success", message="feedback stats", data=feedback.stats()
        )
        stats_content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=stats_content)

    def approve_action(
        feedback_id: int,
        action: ReviewAction,
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """Approve one signal and promote its pair."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        result = approve(feedback, llm, embedding_model, feedback_id, action.reviewer)
        status_code = 200 if result.approved else 409
        message = result.action if result.approved else f"not approved: {result.action}"
        envelope = Envelope[dict[str, object]](
            status="Success" if result.approved else "Failure",
            message=message,
            data={
                "feedbackId": result.feedback_id,
                "approved": result.approved,
                "promoted": result.promoted,
                "tenant": result.example_tenant,
            },
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=status_code, content=content)

    def reject_action(
        feedback_id: int,
        action: ReviewAction,
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """Reject one signal."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        result = reject(feedback, feedback_id, action.reviewer)
        envelope = Envelope[dict[str, object]](
            status="Success" if result.rejected else "Failure",
            message="rejected" if result.rejected else "not rejectable",
            data={"feedbackId": result.feedback_id, "rejected": result.rejected},
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200 if result.rejected else 409, content=content)

    def auto_promote(
        action: ReviewAction,
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """Batch-approve every pending positive signal."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        promoted = auto_promote_positive(feedback, llm, embedding_model, action.reviewer)
        envelope = Envelope[dict[str, int]](
            status="Success", message="batch promotion complete", data={"promoted": promoted}
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def golden_eval(
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> JSONResponse:
        """Export approved pairs as a CI-consumable JSON list."""
        blocked = guarded(x_admin_token)
        if blocked is not None:
            return blocked
        rows = [
            {"question": row.question, "sql": row.sql, "tenant": row.tenant}
            for row in feedback.golden_rows()
        ]
        envelope = Envelope[list[dict[str, str]]](
            status="Success", message="golden eval export", data=rows
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    router.add_api_route("/admin/feedback/pending", _with_boundary(pending), methods=["GET"])
    router.add_api_route("/admin/feedback/stats", _with_boundary(stats), methods=["GET"])
    router.add_api_route(
        "/admin/feedback/{feedback_id}/approve", _with_boundary(approve_action), methods=["POST"]
    )
    router.add_api_route(
        "/admin/feedback/{feedback_id}/reject", _with_boundary(reject_action), methods=["POST"]
    )
    router.add_api_route(
        "/admin/feedback/auto-promote-positive", _with_boundary(auto_promote), methods=["POST"]
    )
    router.add_api_route(
        "/admin/feedback/golden-eval", _with_boundary(golden_eval), methods=["GET"]
    )
    return router
