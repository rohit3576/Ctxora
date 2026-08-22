"""Onboarding API: probe + readiness endpoints."""

import logging
from typing import ClassVar

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from agent.pipeline import AgentDeps
from api.schemas import Envelope
from knowledge.store import NotOnboardedError, Query
from onboarding.state import OnboardingStateStore
from onboarding.wizard import (
    promote_candidate,
    promotion_plan,
    set_activation,
    stage_suggestions,
    suggest_name,
)

_logger = logging.getLogger("datamind.onboarding")


class KeyView(BaseModel):
    """One telemetry key discovered by the probe."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    key: str
    sampleCount: int
    firstSeen: str | None = None
    lastSeen: str | None = None


class EventTypeView(BaseModel):
    """One event type discovered by the probe."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    eventType: str
    sampleCount: int


class ProbeData(BaseModel):
    """Probe result payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    keys: list[KeyView]
    eventTypes: list[EventTypeView]


class StageCandidate(BaseModel):
    """One suggested mapping to stage."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    physicalKey: str
    alias: str


class StageRequest(BaseModel):
    """A batch of suggestions to stage."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source: str = "onboarding"
    candidates: list[StageCandidate]


class ReadinessData(BaseModel):
    """Readiness checklist payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    keysRegistered: bool
    probeCached: bool
    ready: bool


def build_onboarding_router(deps: AgentDeps, executor: Query) -> APIRouter:
    """Build the onboarding router with deps and the metadata executor closed over."""
    state = OnboardingStateStore(executor)
    router = APIRouter(tags=["onboarding"])

    def probe(tenant: str) -> JSONResponse:
        """Introspect the tenant's telemetry: distinct keys + event types."""
        try:
            key_stats = deps.store.introspect_keys(tenant)
            event_stats = deps.store.introspect_event_types(tenant)
        except Exception as exc:  # noqa: BLE001 (boundary: store outage -> typed 503)
            _logger.warning("probe store unavailable: %s", exc)
            detail = str(exc).splitlines()[0]
            body = Envelope[None](status="Failure", message=detail, data=None)
            unavailable: dict[str, object] = body.model_dump()
            unavailable["errorType"] = "STORE_UNAVAILABLE"
            unavailable["statusCode"] = 503
            return JSONResponse(status_code=503, content=unavailable)
        data = ProbeData(
            keys=[
                KeyView(
                    key=stat.key,
                    sampleCount=stat.sample_count,
                    firstSeen=stat.first_seen.isoformat() if stat.first_seen else None,
                    lastSeen=stat.last_seen.isoformat() if stat.last_seen else None,
                )
                for stat in key_stats
            ],
            eventTypes=[
                EventTypeView(eventType=stat.event_type, sampleCount=stat.sample_count)
                for stat in event_stats
            ],
        )
        try:
            state.save_probe(tenant, data.model_dump())
        except Exception as exc:  # noqa: BLE001 (boundary: probe stays answerable)
            _logger.warning("probe cache write failed (non-blocking): %s", exc)
        envelope = Envelope[ProbeData](status="Success", message="probe complete", data=data)
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def readiness(tenant: str) -> JSONResponse:
        """Report the tenant's Phase-2 readiness checklist (outage-tolerant)."""
        try:
            keys_registered = bool(deps.knowledge.load(tenant).keys)
        except NotOnboardedError:
            keys_registered = False
        except Exception as exc:  # noqa: BLE001 (boundary: readiness stays answerable)
            _logger.warning("knowledge load failed in readiness: %s", exc)
            keys_registered = False
        try:
            probe_cached = state.probe_cached(tenant)
        except Exception as exc:  # noqa: BLE001 (boundary: readiness stays answerable)
            _logger.warning("probe-cache read failed: %s", exc)
            probe_cached = False
        data = ReadinessData(
            keysRegistered=keys_registered,
            probeCached=probe_cached,
            ready=keys_registered,
        )
        envelope = Envelope[ReadinessData](status="Success", message="readiness checked", data=data)
        ok_content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok_content)

    def naming_suggestions(tenant: str) -> JSONResponse:
        """Rule-based friendly names for the tenant's probed keys."""
        try:
            key_stats = deps.store.introspect_keys(tenant)
        except Exception as exc:  # noqa: BLE001 (boundary: probe outage -> typed 503)
            _logger.warning("naming probe unavailable: %s", exc)
            detail = str(exc).splitlines()[0]
            unavailable = Envelope[None](status="Failure", message=detail, data=None)
            body: dict[str, object] = unavailable.model_dump()
            body["errorType"] = "STORE_UNAVAILABLE"
            body["statusCode"] = 503
            return JSONResponse(status_code=503, content=body)
        suggestions = [
            {
                "physicalKey": stat.key,
                "friendlyName": suggest_name(stat.key)[0],
                "alias": suggest_name(stat.key)[1],
            }
            for stat in key_stats
        ]
        envelope = Envelope[list[dict[str, str]]](
            status="Success", message="naming suggestions", data=suggestions
        )
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    def review_queue(tenant: str) -> JSONResponse:
        """Pending key-mapping candidates awaiting approval."""
        try:
            plan = promotion_plan(executor, tenant)
        except Exception as exc:  # noqa: BLE001 (boundary: typed failure)
            _logger.warning("review queue unavailable: %s", exc)
            return _wizard_unavailable(str(exc))
        envelope = Envelope[list[dict[str, object]]](
            status="Success", message="pending candidates", data=plan
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def stage_candidates(tenant: str, payload: StageRequest) -> JSONResponse:
        """Stage suggested candidates into the review queue."""
        staged = stage_suggestions(
            executor,
            tenant,
            tuple((c.physicalKey, c.alias, 0.9) for c in payload.candidates),
            payload.source,
        )
        envelope = Envelope[dict[str, int]](
            status="Success", message="candidates staged", data={"staged": staged}
        )
        staged_content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=staged_content)

    def approve_candidate(tenant: str, candidate_id: int) -> JSONResponse:
        """Promote one candidate into the registry (cache invalidated)."""
        try:
            promoted = promote_candidate(executor, tenant, candidate_id)
        except Exception as exc:  # noqa: BLE001 (boundary: typed failure)
            return _wizard_unavailable(str(exc))
        if not promoted:
            return _wizard_not_found(candidate_id)
        envelope = Envelope[dict[str, bool]](
            status="Success", message="candidate promoted", data={"promoted": True}
        )
        ok_content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok_content)

    def enable(tenant: str) -> JSONResponse:
        """Activate the tenant for query endpoints."""
        return _activation(tenant, enabled=True)

    def disable(tenant: str) -> JSONResponse:
        """Deactivate the tenant."""
        return _activation(tenant, enabled=False)

    def _activation(tenant: str, enabled: bool) -> JSONResponse:
        """Shared activation flip."""
        try:
            changed = set_activation(executor, tenant, enabled)
        except Exception as exc:  # noqa: BLE001 (boundary: typed failure)
            return _wizard_unavailable(str(exc))
        if not changed:
            return _wizard_not_found(tenant)
        envelope = Envelope[dict[str, object]](
            status="Success",
            message="enabled" if enabled else "disabled",
            data={"tenant": tenant, "active": enabled},
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def _wizard_unavailable(detail: str) -> JSONResponse:
        body = Envelope[None](status="Failure", message=detail.splitlines()[0], data=None)
        content = body.model_dump()
        content["errorType"] = "WIZARD_STORE_UNAVAILABLE"
        content["statusCode"] = 503
        return JSONResponse(status_code=503, content=content)

    def _wizard_not_found(what: object) -> JSONResponse:
        body = Envelope[None](status="Failure", message=f"not found: {what}", data=None)
        content = body.model_dump()
        content["errorType"] = "NOT_FOUND"
        content["statusCode"] = 404
        return JSONResponse(status_code=404, content=content)

    router.add_api_route("/v1/onboarding/{tenant}/probe", probe, methods=["GET"])
    router.add_api_route("/v1/onboarding/{tenant}/readiness", readiness, methods=["GET"])
    router.add_api_route(
        "/v1/onboarding/{tenant}/naming-suggestions", naming_suggestions, methods=["GET"]
    )
    router.add_api_route("/v1/onboarding/{tenant}/review", review_queue, methods=["GET"])
    router.add_api_route("/v1/onboarding/{tenant}/candidates", stage_candidates, methods=["POST"])
    router.add_api_route(
        "/v1/onboarding/{tenant}/candidates/{candidate_id}/approve",
        approve_candidate,
        methods=["POST"],
    )
    router.add_api_route("/v1/onboarding/{tenant}/enable", enable, methods=["POST"])
    router.add_api_route("/v1/onboarding/{tenant}/disable", disable, methods=["POST"])
    return router
