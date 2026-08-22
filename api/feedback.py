"""Feedback API: POST /v1/feedback (public capture, flag-gated)."""

from typing import ClassVar, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.schemas import Envelope
from feedback.capture import HistoryNotFoundError, capture
from feedback.contracts import FeedbackStore, Rating
from memory.contracts import MemoryStore


class FeedbackRequest(BaseModel):
    """One thumbs up/down signal about an answered query."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    historyId: int = Field(gt=0)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=2000)
    sessionId: str | None = None


class FeedbackData(BaseModel):
    """Capture receipt."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    feedbackId: int
    tenant: str
    rating: Rating


def build_feedback_router(feedback: FeedbackStore, memory: MemoryStore) -> APIRouter:
    """Build the public feedback-capture router."""

    def submit(request: FeedbackRequest) -> JSONResponse:
        """Record one signal; tenant is derived server-side."""
        try:
            result = capture(
                feedback,
                memory,
                request.historyId,
                request.rating,
                session_id=request.sessionId,
                comment=request.comment,
            )
        except HistoryNotFoundError as exc:
            body = Envelope[None](status="Failure", message=str(exc), data=None)
            content: dict[str, object] = body.model_dump()
            content["errorType"] = "HISTORY_NOT_FOUND"
            content["statusCode"] = 404
            return JSONResponse(status_code=404, content=content)

        data = FeedbackData(
            feedbackId=result.feedback_id, tenant=result.tenant, rating=result.rating
        )
        envelope = Envelope[FeedbackData](status="Success", message="feedback recorded", data=data)
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    router = APIRouter(tags=["feedback"])
    router.add_api_route("/v1/feedback", submit, methods=["POST"])
    return router
