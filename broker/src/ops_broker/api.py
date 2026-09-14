"""Fail-closed FastAPI adapter for a private UDS or explicit loopback development."""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal
import unicodedata

import uvicorn
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import AuthorizationDenied, BrokerError
from .factory import build_service
from .service import BrokerService


ActorId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")]
RequestId = Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")]
_API_MODES = frozenset({"approvals-only", "full"})
_APPROVALS_ONLY_ROUTES = frozenset(
    {
        ("/healthz", frozenset({"GET"})),
        ("/v1/zulip/approvals/pending", frozenset({"GET"})),
        ("/v1/zulip/missions", frozenset({"POST"})),
        ("/v1/zulip/missions/{mission_id}/status", frozenset({"GET"})),
        ("/v1/zulip/missions/{mission_id}/resume", frozenset({"POST"})),
        ("/v1/zulip/missions/{mission_id}/answers", frozenset({"POST"})),
        ("/v1/actions/{action_id}/approvals", frozenset({"POST"})),
    }
)
_ZULIP_PUBLISHER_ACTOR_ID = "zulip-publisher"
_ZULIP_MOBILE_ACTOR_ID = "zulip-mobile"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MissionCreate(StrictModel):
    actor_id: ActorId
    request_id: RequestId
    project_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=240)]


class IncidentCreate(MissionCreate):
    severity: Literal["info", "warning", "critical"]


class ActionCreate(StrictModel):
    actor_id: ActorId
    request_id: RequestId
    runbook_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,127}$")]
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: Annotated[str, Field(min_length=3, max_length=1000)]
    incident_id: str | None = None
    mission_id: str | None = None


class ActionExecute(StrictModel):
    actor_id: ActorId


class ApprovalCreate(StrictModel):
    actor_id: ActorId
    request_id: RequestId
    decision: Literal["approve", "reject"]
    source: Annotated[str, Field(pattern=r"^[a-z][a-z0-9._-]{0,31}$")] = "zulip"
    source_event_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ZulipMissionCreate(StrictModel):
    request_id: RequestId
    project_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=240)]


class ZulipMissionResume(StrictModel):
    request_id: RequestId


class ZulipMissionAnswer(StrictModel):
    request_id: RequestId
    source_user_id: Annotated[int, Field(gt=0, le=9_223_372_036_854_775_807)]
    answer: Annotated[str, Field(min_length=1, max_length=500)]


def _require_header_actor(body_actor: str, header_actor: str) -> None:
    if body_actor != header_actor:
        raise AuthorizationDenied("X-Actor-ID does not match the request actor_id")


def create_app(
    service: BrokerService | None = None,
    *,
    api_mode: str | None = None,
) -> FastAPI:
    selected_mode = api_mode or os.environ.get("OPS_BROKER_API_MODE", "approvals-only")
    if selected_mode not in _API_MODES:
        raise RuntimeError("OPS_BROKER_API_MODE must be approvals-only or full")
    approvals_only = selected_mode == "approvals-only"
    app = FastAPI(
        title="ops-broker",
        version="0.1.0",
        description="Local broker for allowlisted operational runbooks. No secret-read API exists.",
        docs_url=None if approvals_only else "/docs",
        redoc_url=None if approvals_only else "/redoc",
        openapi_url=None if approvals_only else "/openapi.json",
    )
    app.state.service = service or build_service()
    app.state.api_mode = selected_mode

    @app.exception_handler(BrokerError)
    async def broker_error_handler(_: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default response can echo the rejected input.  Return only
        # structural diagnostics so malformed requests cannot reflect secrets.
        errors = [
            {
                "type": error.get("type", "validation_error"),
                "location": [str(item) for item in error.get("loc", ())],
                "message": error.get("msg", "request validation failed"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "request validation failed",
                    "details": {"errors": errors},
                }
            },
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/audit/verify")
    def verify_audit() -> dict[str, Any]:
        return app.state.service.database.verify_audit_chain().public()

    @app.get("/v1/runbooks")
    def list_runbooks(x_actor_id: Annotated[str, Header(alias="X-Actor-ID")]) -> list[dict[str, Any]]:
        return app.state.service.list_runbooks(x_actor_id)

    @app.post("/v1/missions", status_code=201)
    def create_mission(
        body: MissionCreate,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        _require_header_actor(body.actor_id, x_actor_id)
        return app.state.service.create_mission(**body.model_dump())

    @app.get("/v1/missions/{mission_id}")
    def get_mission(
        mission_id: str,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        return app.state.service.get_mission(x_actor_id, mission_id)

    @app.post("/v1/incidents", status_code=201)
    def create_incident(
        body: IncidentCreate,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        _require_header_actor(body.actor_id, x_actor_id)
        return app.state.service.create_incident(**body.model_dump())

    @app.get("/v1/incidents/{incident_id}")
    def get_incident(
        incident_id: str,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        return app.state.service.get_incident(x_actor_id, incident_id)

    @app.post("/v1/actions")
    def request_action(
        body: ActionCreate,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        _require_header_actor(body.actor_id, x_actor_id)
        return app.state.service.request_action(**body.model_dump())

    @app.get("/v1/actions/{action_id}")
    def get_action(
        action_id: str,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        return app.state.service.get_action(x_actor_id, action_id)

    @app.post("/v1/actions/{action_id}/execute")
    def execute_action(
        action_id: str,
        body: ActionExecute,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        _require_header_actor(body.actor_id, x_actor_id)
        return app.state.service.execute_action(actor_id=body.actor_id, action_id=action_id)

    @app.get("/v1/approvals/pending")
    def pending_approvals(
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> list[dict[str, Any]]:
        return app.state.service.pending_approvals(x_actor_id)

    @app.get("/v1/zulip/approvals/pending")
    def pending_public_approvals(
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> dict[str, list[dict[str, str]]]:
        # The filesystem-restricted Unix peer is the primary authentication
        # boundary. This fixed identity prevents the bridge from selecting a
        # broader actor and the response contains no generic action fields.
        if x_actor_id != _ZULIP_PUBLISHER_ACTOR_ID:
            raise AuthorizationDenied("publisher actor is not authorized")
        return {
            "items": app.state.service.pending_public_approvals(
                limit=limit,
                offset=offset,
            )
        }

    @app.post("/v1/zulip/missions", status_code=201)
    def create_zulip_mission(
        body: ZulipMissionCreate,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        # The bridge performs the fresh Zulip human-ID verification.  This
        # route nevertheless pins a least-privilege broker identity, so a
        # compromised bridge can enqueue missions only in explicitly granted
        # projects and can never create or execute actions.
        if x_actor_id != _ZULIP_MOBILE_ACTOR_ID:
            raise AuthorizationDenied("mobile bridge actor is not authorized")
        if any(unicodedata.category(character).startswith("C") for character in body.title):
            raise BrokerError("invalid_request", "mission title contains control characters", status_code=422)
        mission = app.state.service.create_mission(
            actor_id=_ZULIP_MOBILE_ACTOR_ID,
            request_id=body.request_id,
            project_id=body.project_id,
            title=body.title,
        )
        return {
            key: mission[key]
            for key in ("id", "project_id", "title", "status", "created_at", "updated_at")
        }

    @app.get("/v1/zulip/missions/{mission_id}/status")
    def get_zulip_mission_status(
        mission_id: str,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        if x_actor_id != _ZULIP_MOBILE_ACTOR_ID:
            raise AuthorizationDenied("mobile bridge actor is not authorized")
        mission = app.state.service.get_mission(_ZULIP_MOBILE_ACTOR_ID, mission_id)
        runtime = app.state.service.mission_control.get_mission_state(
            _ZULIP_MOBILE_ACTOR_ID, mission_id
        )
        # Never expose the mission's free-form context, files, resources,
        # model traces or action parameters to the chat bridge.
        return {
            "id": mission["id"],
            "project_id": mission["project_id"],
            "title": mission["title"],
            "status": mission["status"],
            "queue_state": runtime["queue_state"],
            "updated_at": mission["updated_at"],
        }

    @app.post("/v1/zulip/missions/{mission_id}/resume")
    def resume_zulip_mission(
        mission_id: str,
        body: ZulipMissionResume,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        if x_actor_id != _ZULIP_MOBILE_ACTOR_ID:
            raise AuthorizationDenied("mobile bridge actor is not authorized")
        mission = app.state.service.get_mission(_ZULIP_MOBILE_ACTOR_ID, mission_id)
        if mission["status"] == "completed":
            raise AuthorizationDenied("a completed mission cannot be resumed from Zulip")
        if mission["status"] == "paused":
            app.state.service.set_mission_status(
                actor_id=_ZULIP_MOBILE_ACTOR_ID,
                request_id=body.request_id,
                mission_id=mission_id,
                status="open",
            )
        return get_zulip_mission_status(mission_id, x_actor_id)

    @app.post("/v1/zulip/missions/{mission_id}/answers", status_code=201)
    def answer_zulip_mission(
        mission_id: str,
        body: ZulipMissionAnswer,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        if x_actor_id != _ZULIP_MOBILE_ACTOR_ID:
            raise AuthorizationDenied("mobile bridge actor is not authorized")
        if any(unicodedata.category(character).startswith("C") for character in body.answer):
            raise BrokerError("invalid_request", "mobile answer contains control characters", status_code=422)
        # Numeric identity was freshly validated by the bridge. Recording it
        # preserves human attribution but never expands broker authorization.
        record = app.state.service.mission_control.add_record(
            actor_id=_ZULIP_MOBILE_ACTOR_ID,
            request_id=body.request_id,
            mission_id=mission_id,
            kind="decision",
            content=f"Réponse Zulip utilisateur {body.source_user_id}: {body.answer}",
            evidence=[],
        )
        return {
            key: record[key]
            for key in ("id", "mission_id", "kind", "created_at", "replayed")
        }

    @app.post("/v1/actions/{action_id}/approvals", status_code=201)
    def decide_approval(
        action_id: str,
        body: ApprovalCreate,
        x_actor_id: Annotated[str, Header(alias="X-Actor-ID")],
    ) -> dict[str, Any]:
        _require_header_actor(body.actor_id, x_actor_id)
        return app.state.service.decide_approval(action_id=action_id, **body.model_dump())

    if approvals_only:
        # The Unix peer is a trusted bridge process, not an authenticated human
        # actor. Keep that process technically unable to create or execute an
        # action even if it forges X-Actor-ID. Codex/Hermes use fixed-identity
        # MCP stdio processes for every non-approval operation.
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if (
                getattr(route, "path", None),
                frozenset(getattr(route, "methods", frozenset())),
            )
            in _APPROVALS_ONLY_ROUTES
        ]

    return app


def main() -> None:
    inherited_fd = os.environ.get("OPS_BROKER_FD")
    unix_socket = os.environ.get("OPS_BROKER_SOCKET")
    if inherited_fd and unix_socket:
        raise SystemExit("configure either OPS_BROKER_FD or OPS_BROKER_SOCKET, not both")
    if inherited_fd:
        try:
            descriptor = int(inherited_fd)
        except ValueError as exc:
            raise SystemExit("OPS_BROKER_FD must be an integer") from exc
        if descriptor < 3:
            raise SystemExit("OPS_BROKER_FD must be an inherited descriptor")
        uvicorn.run(create_app(), fd=descriptor, access_log=False)
        return
    if unix_socket:
        if not unix_socket.startswith("/"):
            raise SystemExit("OPS_BROKER_SOCKET must be an absolute path")
        uvicorn.run(create_app(), uds=unix_socket, access_log=False)
        return

    host = os.environ.get("OPS_BROKER_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("ops-broker refuses a non-loopback bind address")
    port = int(os.environ.get("OPS_BROKER_PORT", "8788"))
    uvicorn.run(create_app(), host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
