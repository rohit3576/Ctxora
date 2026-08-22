"""History API: GET /v1/history grouped by session, newest first."""

import logging
from typing import Annotated, ClassVar

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from api.schemas import Envelope
from memory.contracts import MemoryStore

_logger = logging.getLogger("datamind.history")

_ROW_TYPE = dict[str, float | int | str | bool | None]


class TurnView(BaseModel):
    """One persisted turn on the wire."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    historyId: int
    nlQuery: str
    sql: str
    rows: list[_ROW_TYPE]
    summary: str
    tokenUsage: int


class SessionView(BaseModel):
    """One session with its turns on the wire."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    sessionId: str
    title: str
    createdAt: str | None = None
    turns: list[TurnView]


def build_history_router(memory: MemoryStore) -> APIRouter:
    """Build the history router with memory closed over."""

    def history(
        tenant: Annotated[str, Query(min_length=1, max_length=50)],
    ) -> JSONResponse:
        """List sessions newest-first with their oldest-first turns."""
        try:
            page = memory.list_history(tenant)
        except Exception as exc:  # noqa: BLE001 (boundary: report unavailability)
            _logger.warning("history listing failed: %s", exc)
            body = Envelope[None](status="Failure", message="history unavailable", data=None)
            content: dict[str, object] = body.model_dump()
            content["errorType"] = "MEMORY_UNAVAILABLE"
            content["statusCode"] = 503
            return JSONResponse(status_code=503, content=content)

        sessions = [
            SessionView(
                sessionId=item.session.id,
                title=item.session.title,
                createdAt=item.session.created_at.isoformat() if item.session.created_at else None,
                turns=[
                    TurnView(
                        historyId=turn.id,
                        nlQuery=turn.nl_query,
                        sql=turn.sql,
                        rows=[dict(row) for row in turn.data],
                        summary=turn.summary,
                        tokenUsage=turn.token_usage,
                    )
                    for turn in item.turns
                ],
            )
            for item in page
        ]
        envelope = Envelope[list[SessionView]](
            status="Success", message="history fetched", data=sessions
        )
        ok_content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok_content)

    router = APIRouter(tags=["memory"])
    router.add_api_route("/v1/history", history, methods=["GET"])
    return router
