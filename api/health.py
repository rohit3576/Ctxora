"""Health endpoints: liveness for probes, readiness for gateways.

The router is built by a factory that closes over the typed Settings and the
readiness check, avoiding the untyped ``request.app.state`` ambient bag.
"""

from collections.abc import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.schemas import Envelope, HealthLive, HealthReady
from config.settings import Settings

ReadinessCheck = Callable[[Settings], tuple[bool, str]]


def healthz() -> Envelope[HealthLive]:
    """Liveness: instant, no dependencies. Used by probes."""
    return Envelope(status="Success", message="api is alive", data=HealthLive(status="ok"))


def _readyz_payload(ok: bool) -> JSONResponse:
    """Render the readiness envelope with the matching status code."""
    if ok:
        body = Envelope[HealthReady](
            status="Success",
            message="all dependencies ready",
            data=HealthReady(status="ready", metadata_db="ok"),
        )
        content: dict[str, object] = body.model_dump()
        return JSONResponse(status_code=200, content=content)

    body = Envelope[HealthReady](
        status="Failure",
        message="metadata database unreachable",
        data=HealthReady(status="unavailable", metadata_db="unreachable"),
    )
    failure_content: dict[str, object] = body.model_dump()
    return JSONResponse(status_code=503, content=failure_content)


def build_health_router(settings: Settings, check_db: ReadinessCheck) -> APIRouter:
    """Build the health router with settings and the readiness check closed over."""

    def readyz() -> JSONResponse:
        """Readiness: metadata DB must answer SELECT 1."""
        ok, _detail = check_db(settings)
        return _readyz_payload(ok)

    router = APIRouter(tags=["health"])
    router.add_api_route("/healthz", healthz, methods=["GET"])
    router.add_api_route("/readyz", readyz, methods=["GET"])
    return router
