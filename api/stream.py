"""SSE streaming: POST /v1/query/sql/stream.

Worker thread runs the pipeline; events flow through a queue. Event order:
stage (pipeline order) -> summary_delta chunks -> final | error. ': ping'
heartbeat when the queue is silent past the interval.
"""

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter, ValidationError

from agent.pipeline import AgentDeps
from api.query import SQLQueryRequest, run_recorded
from memory.contracts import MemoryStore

_logger = logging.getLogger("querypulse.stream")

_MEDIA_TYPE: Final = "text/event-stream"
_DEFAULT_PING_INTERVAL_S: Final = 15.0
_DELTA_WORDS: Final = 4


@dataclass(frozen=True, slots=True)
class _Event:
    """One SSE event."""

    name: str
    payload: str


def _format(event: _Event) -> str:
    """Render one SSE frame."""
    return f"event: {event.name}\ndata: {event.payload}\n\n"


_OBJECT_DICT: Final = TypeAdapter(dict[str, object])


def _summary_of(data: object) -> str:
    """Extract the summary string from a success payload."""
    try:
        holder = _OBJECT_DICT.validate_python(data)
    except ValidationError:
        return ""
    value: object = holder.get("summary", "")
    return value if isinstance(value, str) else ""


def _summary_deltas(summary: str) -> list[str]:
    """Split a completed summary into word-group chunks."""
    words = summary.split()
    groups = [" ".join(words[i : i + _DELTA_WORDS]) for i in range(0, len(words), _DELTA_WORDS)]
    return groups or [summary]


def _worker(
    request: SQLQueryRequest,
    deps: AgentDeps,
    memory: MemoryStore,
    outbox: "queue.Queue[_Event | None]",
) -> None:
    """Run the pipeline off-thread and push SSE events; always terminates the queue."""
    try:
        stages: list[str] = []

        def on_stage(stage: str) -> None:
            stages.append(stage)
            outbox.put(_Event("stage", json.dumps({"stage": stage})))

        content, _status = run_recorded(request, deps, memory, on_stage=on_stage)
        if content.get("status") == "Success":
            summary_text = _summary_of(content.get("data"))
            for delta in _summary_deltas(summary_text) if summary_text else ():
                outbox.put(_Event("summary_delta", json.dumps({"text": delta})))
            outbox.put(_Event("final", json.dumps(content)))
        else:
            outbox.put(_Event("error", json.dumps(content)))
    except Exception as exc:  # noqa: BLE001 (worker boundary: surfaced as SSE error)
        _logger.warning("stream worker failed: %s", exc)
        outbox.put(_Event("error", json.dumps({"status": "Failure", "message": str(exc)})))
    finally:
        outbox.put(None)


def build_stream_router(
    deps: AgentDeps,
    memory: MemoryStore,
    ping_interval_s: float = _DEFAULT_PING_INTERVAL_S,
) -> APIRouter:
    """Build the SSE router with deps, memory, and a ping interval closed over."""

    def query_sql_stream(request: SQLQueryRequest) -> StreamingResponse:
        """Stream the pipeline as SSE events."""
        outbox: queue.Queue[_Event | None] = queue.Queue()
        threading.Thread(target=_worker, args=(request, deps, memory, outbox), daemon=True).start()

        def stream() -> Iterator[str]:
            started = time.monotonic()
            while True:
                try:
                    item = outbox.get(timeout=ping_interval_s)
                except queue.Empty:
                    _logger.debug("ping after %.1fs", time.monotonic() - started)
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                yield _format(item)

        return StreamingResponse(
            stream(),
            media_type=_MEDIA_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    router = APIRouter(tags=["query"])
    router.add_api_route("/v1/query/sql/stream", query_sql_stream, methods=["POST"])
    return router
