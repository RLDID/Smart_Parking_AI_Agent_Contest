import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.auth import ApiError, Auth
from backend.runtime import Runtime
from contracts.models import Control, CreateRun, DriverStateView, Login, RunView, StateView, VehicleList
from contracts.spatial import SpatialAnalysis
from contracts.business import CommandInput, CancelInput, ReceiptInput, ResponseInput
from backend.knowledge import transaction
from backend.business import ident, encoded
from agent.manual import ManualS1
from contracts.agent_loop import AgentQuery, LiveAgentQuery
from agent.live import LiveConfiguration, LiveModels
from simulator.spatial import analyze_spatial_context
from simulator.world import FACILITY, MAP, public_state
from backend.relationship_routes import install_relationship_routes
from backend.autonomous_routes import install_autonomous_routes
from backend.history_views import install_history_view_routes
from backend.vehicle_views import install_vehicle_view_routes
from contracts.synthetic_users import SyntheticUserInput
from agent.tools import validate_session
from contracts.environment_controls import DeviceFaultInput, S2ReactionInput

ROOT = Path(__file__).resolve().parents[2]
COOKIE = "parking_session"


def team_demo_origin(value: str) -> str:
    """Validate a DNS-only HTTPS origin and serialize it as browsers do.

    Paths (including /), credentials, IP literals, wildcards and URL escapes
    are deliberately unsupported. Only host case and port notation normalize.
    """
    invalid = "team_demo_origin must be an HTTPS DNS origin with an optional port (1..65535)"
    if not isinstance(value, str):
        raise ValueError(invalid)
    match = re.fullmatch(r"https://([a-z0-9.-]+)(?::([0-9]{1,5}))?", value, re.IGNORECASE | re.ASCII)
    if not match:
        raise ValueError(invalid)
    host, raw_port = match.groups()
    host = host.lower()
    labels = host.split(".")
    if (len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                               for label in labels)
            or labels[-1].isdigit() or re.fullmatch(r"0x[0-9a-f]+", labels[-1])):
        raise ValueError(invalid)
    port = int(raw_port) if raw_port is not None else 443
    if not 1 <= port <= 65535:
        raise ValueError(invalid)
    return f"https://{host}" + (f":{port}" if port != 443 else "")


@dataclass(frozen=True)
class Settings:
    database: Path = ROOT / "data/local/foundation.sqlite3"
    test_control: bool = False
    origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000")
    background_ticks: bool = True
    live_configuration: LiveConfiguration | None = None
    budget_database: Path = ROOT / "data/local/model-budget.sqlite3"
    team_demo_origin: str | None = None

    def __post_init__(self):
        if self.team_demo_origin is not None:
            if self.test_control is not True:
                raise ValueError("team_demo_origin requires explicit test_control=True demo mode")
            object.__setattr__(self, "team_demo_origin", team_demo_origin(self.team_demo_origin))


def create_app(settings: Settings | None = None):
    if settings is None:
        raw = os.environ.get("TEST_CONTROL_ENABLED", "false")
        if raw not in ("true", "false"):
            raise RuntimeError("TEST_CONTROL_ENABLED must be true or false")
        config_path = os.environ.get("PARKING_LIVE_CONFIG")
        port = int(os.environ.get("PARKING_LOCAL_PORT", "8000"))
        database = Path(os.environ.get("PARKING_LOCAL_DATABASE", str(Settings.database)))
        settings = Settings(test_control=raw == "true", database=database,
            origins=(f"http://127.0.0.1:{port}", f"http://localhost:{port}"),
            team_demo_origin=os.environ.get("PARKING_TEAM_DEMO_ORIGIN"),
            live_configuration=LiveConfiguration.read(config_path) if config_path else None)
    if settings.live_configuration is not None and not settings.test_control:
        raise ValueError("Live comparison requires explicit local development controls")

    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(settings.database)
        try:
            if settings.live_configuration is not None:
                runtime.queries.live_models = LiveModels(settings.live_configuration,
                    settings.budget_database)
        except Exception:
            runtime.store.close()
            raise
        app.state.runtime = runtime
        app.state.auth = Auth(runtime.store)
        task = asyncio.create_task(runtime.loop()) if settings.background_ticks else None
        business_task = asyncio.create_task(runtime.business_loop()) if settings.background_ticks else None
        try:
            yield
        finally:
            for active_task in (task, business_task):
                if not active_task:
                    continue
                active_task.cancel()
                with suppress(asyncio.CancelledError):
                    await active_task
            await runtime.queries.close()
            await runtime.autonomous.close()
            runtime.store.close()

    app = FastAPI(title="Parking foundation — local development", version="0.1.0", lifespan=lifespan)
    allowed_hosts = ["127.0.0.1", "localhost", "testserver"]
    if settings.team_demo_origin is not None:
        allowed_hosts.append(urlsplit(settings.team_demo_origin).hostname)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts,
                       www_redirect=settings.team_demo_origin is None)
    mutation_origins = (settings.team_demo_origin,) if settings.team_demo_origin is not None else settings.origins

    @app.middleware("http")
    async def local_boundary(request, call_next):
        # Both modes require a loopback transport peer. A demo HTTPS proxy may
        # preserve its Host/Origin, but forwarded headers never grant access.
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse({"error": {"code": "LOCAL_ONLY", "message": "로컬 개발 서버입니다.",
                                           "retryable": False, "details": {}},
                                 "correlation_id": uuid4().hex}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                       "retryable": exc.status == 429, "details": {}},
                             "correlation_id": uuid4().hex}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's raw errors contain input; never echo login secrets.
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "요청 형식을 확인하세요.",
                                       "retryable": False, "details": {}},
                             "correlation_id": uuid4().hex}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return JSONResponse({"error": {"code": "STORAGE_UNAVAILABLE", "message": "저장하지 못했습니다. 같은 요청 키로 결과를 확인하세요.",
                                       "retryable": True, "details": {}},
                             "correlation_id": uuid4().hex}, status_code=503)

    def origin_check(request):
        if request.headers.get("origin") not in mutation_origins:
            raise ApiError(403, "ORIGIN_REJECTED", "허용된 origin에서 요청하세요.")

    def authenticate(request, roles=None, mutation=False):
        session = app.state.auth.require(request.cookies.get(COOKIE), roles)
        if mutation:
            origin_check(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
                raise ApiError(403, "CSRF_REJECTED", "세션의 CSRF 토큰이 필요합니다.")
        return session

    def facility_check(facility_id):
        if facility_id != FACILITY:
            raise ApiError(404, "NOT_FOUND", "시설을 찾을 수 없습니다.")

    def test_operator(request):
        session = authenticate(request, ["test_operator"], mutation=True)
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "시험 제어가 비활성화되어 있습니다.")
        return session

    install_relationship_routes(app, authenticate, facility_check, settings)
    install_autonomous_routes(app, authenticate, facility_check, settings)
    install_history_view_routes(app, authenticate, facility_check, settings)
    install_vehicle_view_routes(app, authenticate, facility_check, settings)

    @app.get("/api/v1/test/runs/{run_id}/synthetic-users")
    async def synthetic_users(run_id: str, request: Request):
        session = authenticate(request, ["owner", "test_operator"])
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "합성 시험이 비활성화되어 있습니다.")
        runtime = app.state.runtime
        async with runtime.lock:
            validate_session(runtime, session)
            return runtime.synthetic_users.view(run_id)

    @app.put("/api/v1/test/runs/{run_id}/synthetic-users")
    async def configure_synthetic_users(run_id: str, body: SyntheticUserInput, request: Request):
        session = test_operator(request)
        runtime = app.state.runtime
        async with runtime.lock:
            session = test_operator(request)
            return runtime.synthetic_users.configure(session, run_id, body, request.headers.get("idempotency-key"))

    @app.get("/api/v1/facilities/{facility_id}/devices")
    async def devices(facility_id: str, run_id: str, request: Request):
        session = authenticate(request, ["owner", "test_operator"])
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            validate_session(runtime, session)
            runtime.ensure_run(run_id)
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "현재 장치 상태를 확인할 수 없습니다.")
            return runtime.public_devices(runtime.world)

    async def environment_control(run_id, body, request, operation):
        session = test_operator(request)
        return await app.state.runtime.environment_control(session, run_id, body,
            request.headers.get("idempotency-key"), operation, authenticate=lambda: test_operator(request))

    @app.put("/api/v1/test/runs/{run_id}/device-faults")
    async def device_fault(run_id: str, body: DeviceFaultInput, request: Request):
        return await environment_control(run_id, body, request, "device_fault")

    @app.put("/api/v1/test/runs/{run_id}/s2-reaction")
    async def s2_reaction(run_id: str, body: S2ReactionInput, request: Request):
        return await environment_control(run_id, body, request, "s2_reaction")

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        runtime = app.state.runtime
        return {"api": "ready", "observation": "unavailable" if runtime.failure or not runtime.world else runtime.world["run_status"],
                "db": "unavailable" if runtime.failure else "ready",
                "current_run_id": runtime.world["run_id"] if runtime.world else None,
                "recovery_required": runtime.world["recovery_required"] if runtime.world else False,
                "llm": "live_read_configured" if runtime.queries.live_models else "not_configured",
                "rag": runtime.knowledge_readiness(), "notification": "local_web_inbox",
                "business": runtime.business_readiness(),
                "test_control_enabled": settings.test_control, "mode": "local_foundation"}

    @app.post("/api/v1/auth/session")
    async def login(body: Login, request: Request, response: Response):
        origin_check(request)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ApiError(415, "JSON_REQUIRED", "JSON 요청이 필요합니다.")
        token, session = app.state.auth.login(body.username, body.password,
                                             request.client.host if request.client else "local")
        old = request.cookies.get(COOKIE)
        app.state.auth.sessions.pop(old, None)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                            secure=settings.team_demo_origin is not None, max_age=3600)
        return {"alias": session.username, "roles": [session.role]}

    @app.delete("/api/v1/auth/session", status_code=204)
    async def logout(request: Request, response: Response):
        origin_check(request)
        if app.state.auth.lookup(request.cookies.get(COOKIE)):
            authenticate(request, mutation=True)
        app.state.auth.sessions.pop(request.cookies.get(COOKIE), None)
        if settings.team_demo_origin is not None:
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        else:
            response.delete_cookie(COOKIE)

    @app.get("/api/v1/me")
    async def me(request: Request):
        session = authenticate(request)
        return {"user_id": session.username, "alias": session.username,
                "facility_roles": [{"facility_id": FACILITY, "roles": [session.role]}],
                "csrf_token": session.csrf}

    @app.get("/api/v1/facilities/{facility_id}/map")
    async def map_view(facility_id: str, request: Request, map_version: str | None = None):
        authenticate(request, ["owner", "test_operator"])
        facility_check(facility_id)
        if map_version is not None and map_version != MAP["map_version"]:
            raise ApiError(404, "MAP_NOT_FOUND", "지원하지 않는 지도 버전입니다.")
        return MAP

    @app.get("/api/v1/me/vehicles", response_model=VehicleList)
    async def my_vehicles(request: Request, facility_id: str):
        session = authenticate(request)
        facility_check(facility_id)
        return {"facility_id": facility_id, "vehicles": app.state.runtime.store.registry.vehicles(session.username)}

    @app.get("/api/v1/facilities/{facility_id}/state", response_model=DriverStateView | StateView)
    async def state(facility_id: str, run_id: str, request: Request):
        session = authenticate(request)
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "저장 실패로 최신 상태를 확인할 수 없습니다.")
            runtime.ensure_run(run_id)
            return runtime.store.registry.project(session.username, public_state(runtime.world), runtime.world["sim_time_ms"])

    @app.get("/api/v1/facilities/{facility_id}/spatial-analysis", response_model=SpatialAnalysis)
    async def spatial_analysis(facility_id: str, run_id: str, request: Request, zone_id: str = "aisle-west"):
        authenticate(request, ["owner", "test_operator"])
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "저장 실패로 최신 상태를 확인할 수 없습니다.")
            runtime.ensure_run(run_id)
            world = runtime.world
            return analyze_spatial_context(
                MAP, world.get("observation_history") or [world["observation"]],
                current_sim_time_ms=world["sim_time_ms"], run_status=world["run_status"],
                recovery_required=world["recovery_required"],
                observation_ready=bool(world.get("observation_history")), zone_id=zone_id)

    @app.post("/api/v1/test/runs", status_code=201, response_model=RunView)
    async def create_run(body: CreateRun, request: Request):
        session = test_operator(request)
        return await app.state.runtime.mutate(session, request.headers.get("idempotency-key"),
                                              "create", body.model_dump(), authenticate=lambda: test_operator(request))

    @app.post("/api/v1/test/runs/{run_id}/control", response_model=RunView)
    async def control(run_id: str, body: Control, request: Request):
        session = test_operator(request)
        arguments = body.model_dump()
        if arguments["action_params"] is not None and arguments["action_params"].get("observation_mode") is None:
            # Preserve hashes of pre-upgrade movement requests saved in SQLite.
            arguments["action_params"].pop("observation_mode", None)
        return await app.state.runtime.mutate(session, request.headers.get("idempotency-key"),
                                              "control", arguments, run_id, authenticate=lambda: test_operator(request))

    @app.post("/api/v1/test/s1a/manual")
    async def manual_s1a(body: ManualS1, request: Request):
        session = test_operator(request)
        return await app.state.runtime.manual_s1a(session, body, request.headers.get("idempotency-key"),
                                                 authenticate=lambda: test_operator(request))

    @app.post("/api/v1/test/agent/queries")
    async def mock_agent_query(body: AgentQuery, request: Request):
        session = authenticate(request, mutation=True)
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "모의 조회 시험이 비활성화되어 있습니다.")
        def still_authenticated():
            current = authenticate(request)
            if current is not session:
                raise ApiError(401, "UNAUTHENTICATED", "조회 세션을 다시 확인하세요.")
        return await app.state.runtime.queries.execute(session, body,
            request.headers.get("idempotency-key"), still_authenticated)

    @app.get("/api/v1/test/agent/config")
    async def agent_configuration(request: Request):
        authenticate(request)
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "조회 시험이 비활성화되어 있습니다.")
        models = app.state.runtime.queries.live_models
        return models.public_status() if models else {"mode": "mock", "providers": [], "budget": None}

    @app.post("/api/v1/test/agent/live-queries")
    async def live_agent_query(body: LiveAgentQuery, request: Request):
        session = authenticate(request, mutation=True)
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "실제 조회 시험이 비활성화되어 있습니다.")
        def still_authenticated():
            current = authenticate(request)
            if current is not session:
                raise ApiError(401, "UNAUTHENTICATED", "조회 세션을 다시 확인하세요.")
        return await app.state.runtime.queries.execute(session, body,
            request.headers.get("idempotency-key"), still_authenticated)

    @app.get("/api/v1/facilities/{facility_id}/events")
    async def events(facility_id: str, run_id: str, request: Request):
        initial_session = authenticate(request)
        facility_check(facility_id)
        token = request.cookies.get(COOKIE)
        cursor = request.headers.get("last-event-id")
        runtime = app.state.runtime
        if not runtime.world:
            raise ApiError(404, "RUN_NOT_FOUND", "먼저 시험 run을 생성하세요.")
        registry = runtime.store.registry
        scope = registry.scope_stamp(initial_session.username) if initial_session.role == "driver" else None

        def stream_access():
            try:
                session = app.state.auth.lookup(token)
                if not session or (scope is not None and registry.scope_stamp(session.username) != scope):
                    return None
                return session
            except sqlite3.Error:
                return None

        async def generate():
            nonlocal cursor, run_id
            idle = 0
            while not await request.is_disconnected():
                session = stream_access()
                if not session:
                    yield 'event: access.revoked\ndata: {"reason":"access_changed_or_unavailable"}\n\n'
                    break
                if runtime.failure:
                    break
                batch, cursor = await runtime.stream_batch(cursor, run_id)
                for event in batch:
                    # Recheck before each event, not just on subscription.
                    session = stream_access()
                    if not session:
                        yield 'event: access.revoked\ndata: {"reason":"access_changed_or_unavailable"}\n\n'
                        return
                    if event["type"] in ("state.snapshot", "run.updated"):
                        try:
                            event = {**event, "payload": registry.project(session.username, event["payload"], runtime.world["sim_time_ms"])}
                        except (sqlite3.Error, ApiError):
                            return
                    elif event["type"] != "reset_required":
                        event = runtime.business.project_event(session.username, event)
                        if event is None:
                            continue
                    run_id = event["run_id"]
                    # A reset is not an acknowledgement of its following snapshot.
                    # Clearing the browser cursor makes a disconnect here recoverable,
                    # including a paused run with no future events.
                    stream_id = "" if event["type"] == "reset_required" else event["event_id"]
                    yield f"id: {stream_id}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                idle = 0 if batch else idle + 1
                if idle >= 20:
                    yield ": heartbeat (not an observation)\n\n"
                    idle = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    @app.get("/api/v1/notifications")
    async def notifications(request: Request, cursor: int = 0, limit: int = 50):
        session = authenticate(request)
        if cursor < 0 or not 1 <= limit <= 100:
            raise ApiError(422, "INVALID_INPUT", "목록 범위를 확인하세요.")
        runtime = app.state.runtime
        async with runtime.lock:
            return runtime.business.notifications(session.username, cursor, limit)

    @app.post("/api/v1/notifications/{notification_id}/receipts")
    async def receipt(notification_id: str, body: ReceiptInput, request: Request):
        session = authenticate(request, mutation=True)
        runtime = app.state.runtime
        async with runtime.lock:
            return runtime.business.reply(session, notification_id, "receipt", body, request.headers.get("idempotency-key"))

    @app.post("/api/v1/notifications/{notification_id}/responses")
    async def response(notification_id: str, body: ResponseInput, request: Request):
        session = authenticate(request, mutation=True)
        runtime = app.state.runtime
        async with runtime.lock:
            return runtime.business.reply(session, notification_id, "response", body, request.headers.get("idempotency-key"))

    @app.get("/api/v1/facilities/{facility_id}/incidents")
    async def incidents(facility_id: str, run_id: str, request: Request, cursor: int = 0, limit: int = 50, status: str | None = None):
        authenticate(request, ["owner", "test_operator"])
        facility_check(facility_id)
        runtime = app.state.runtime
        if cursor < 0 or not 1 <= limit <= 100:
            raise ApiError(422, "INVALID_INPUT", "목록 범위를 확인하세요.")
        async with runtime.lock:
            runtime.ensure_run(run_id)
            rows = runtime.store.db.execute("SELECT rowid AS cursor,* FROM incidents WHERE facility_id=? AND run_id=? AND rowid>? AND (? IS NULL OR status=?) ORDER BY rowid LIMIT ?", (facility_id, run_id, cursor, status, status, limit)).fetchall()
            return {"items": [dict(r) for r in rows], "cursor": rows[-1]["cursor"] if rows else cursor}

    @app.get("/api/v1/incidents/{incident_id}")
    async def incident(incident_id: str, request: Request):
        authenticate(request, ["owner", "test_operator"])
        runtime = app.state.runtime
        async with runtime.lock:
            row = dict(runtime.business.scoped("incidents", incident_id, "incident_id"))
            row["impacts"] = [dict(r) for r in runtime.store.db.execute("SELECT * FROM incident_impacts WHERE incident_id=?", (incident_id,))]
            return row

    @app.post("/api/v1/facilities/{facility_id}/commands", status_code=201)
    async def command(facility_id: str, body: CommandInput, request: Request):
        session = authenticate(request, mutation=True)
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            validate_session(runtime, session)
            runtime.ensure_run(body.run_id)
            runtime.ensure_business_writable()
            session = authenticate(request, mutation=True)
            if session.role == "driver":
                vehicles = {v["registered_vehicle_id"] for v in runtime.store.registry.vehicles(session.username)}
                if body.purpose not in ("own_vehicle_query", "report_exit_blocked") or body.target_vehicle_id not in vehicles:
                    raise ApiError(403, "FORBIDDEN", "자기 차량 조회와 신고만 가능합니다.")
            elif body.purpose not in ("query", "operational_goal"):
                raise ApiError(403, "FORBIDDEN", "역할에 맞는 목적을 사용하세요.")
            key = request.headers.get("idempotency-key")
            fingerprint, old = runtime.business.key(session.username, key, "command", body.model_dump())
            if old:
                return old
            with transaction(runtime.store.db):
                cid = ident("command")
                runtime.business.insert("commands", command_id=cid, facility_id=facility_id, run_id=body.run_id,
                    requester_id=session.username, request_text=body.text, purpose=body.purpose, target_vehicle_id=body.target_vehicle_id,
                    aggregate_status="pending", normalized_goal_json=None)
                result = {"command_id": cid, "aggregate_status": "pending", "resource_version": 1}
                runtime.business.emit(body.run_id, "command.updated", **result)
                runtime.business.audit(session.username, "command", cid, body.run_id)
                runtime.business.save_key(session.username, key, fingerprint, result)
                return result

    @app.get("/api/v1/commands/{command_id}")
    async def get_command(command_id: str, request: Request):
        session = authenticate(request)
        runtime = app.state.runtime
        async with runtime.lock:
            row = runtime.business.scoped("commands", command_id, "command_id")
            if session.role == "driver" and row["requester_id"] != session.username:
                raise ApiError(404, "NOT_FOUND", "지시를 찾을 수 없습니다.")
            return dict(row)

    @app.get("/api/v1/executions/{execution_id}")
    async def get_execution(execution_id: str, request: Request):
        session = authenticate(request, ["owner", "test_operator"])
        runtime = app.state.runtime
        async with runtime.lock:
            return runtime.business.execution_view(runtime.business.scoped("executions", execution_id, "execution_id"))

    @app.post("/api/v1/commands/{command_id}/cancel")
    async def cancel_command(command_id: str, body: CancelInput, request: Request):
        session = authenticate(request, mutation=True)
        runtime = app.state.runtime
        async with runtime.lock:
            session = authenticate(request, mutation=True)
            validate_session(runtime, session)
            runtime.ensure_business_writable()
            row = runtime.business.scoped("commands", command_id, "command_id")
            runtime.ensure_run(row["run_id"])
            if session.role == "driver" and row["requester_id"] != session.username:
                raise ApiError(404, "NOT_FOUND", "지시를 찾을 수 없습니다.")
            key = request.headers.get("idempotency-key")
            fingerprint, old = runtime.business.key(session.username, key, "cancel_command", {"command_id": command_id, **body.model_dump()})
            if old:
                return old
            if row["resource_version"] != body.expected_resource_version:
                raise ApiError(409, "RESOURCE_CHANGED", "지시 버전이 바뀌었습니다.")
            candidate = deepcopy(runtime.world)
            device_changed = False
            with transaction(runtime.store.db):
                for execution in runtime.store.db.execute("SELECT * FROM executions WHERE command_id=? AND status IN ('accepted','running')", (command_id,)).fetchall():
                    fields = {"cancellation_requested_at": runtime.business.clock()}
                    if execution["status"] == "accepted":
                        if execution["tool_name"] == "play_announcement" and execution["mode"] == "synthetic_demo":
                            status, device_result, changed = runtime.devices.cancel_announcement(candidate, execution)
                            device_changed |= changed
                            fields.update(status=status, result_json=encoded(device_result))
                            if status == "cancelled":
                                fields["error_code"] = "CANCELLED"
                        else:
                            fields["status"] = "cancelled"
                            notice = runtime.store.db.execute("SELECT notification_id FROM notifications WHERE execution_id=?", (execution["execution_id"],)).fetchone()
                            if notice:
                                runtime.business.changed("notifications", "notification_id", notice[0], row["run_id"], delivery_status="failed")
                    runtime.business.changed("executions", "execution_id", execution["execution_id"], row["run_id"], **fields)
                # A command can own several successive plans after clarification.
                # The latest plan represents its current goal even after cancel.
                plan = runtime.store.db.execute(
                    "SELECT * FROM plans WHERE command_id=? "
                    "ORDER BY rowid DESC LIMIT 1", (command_id,)).fetchone()
                all_executions = runtime.store.db.execute(
                    "SELECT * FROM executions WHERE command_id=? ORDER BY rowid", (command_id,)).fetchall()
                executions = runtime.store.db.execute(
                    "SELECT * FROM executions WHERE plan_id=? ORDER BY rowid" if plan else
                    "SELECT * FROM executions WHERE command_id=? ORDER BY rowid",
                    (plan["plan_id"],) if plan else (command_id,)).fetchall()
                statuses = {execution["status"] for execution in executions}
                unresolved = {execution["status"] for execution in all_executions}
                aggregate = ("unknown" if "unknown" in unresolved else "running" if "running" in unresolved
                    else "partial" if len(statuses) > 1 and statuses & {"succeeded", "failed", "held"}
                    else "succeeded" if statuses == {"succeeded"}
                    else "failed" if statuses == {"failed"}
                    else "held" if statuses == {"held"} else "cancelled")
                if plan and aggregate not in ("unknown", "running"):
                    remaining = [(step["tool"], step.get("zone_id"))
                                 for step in json.loads(plan["steps_json"])]
                    for execution in executions:
                        if execution["status"] != "succeeded":
                            continue
                        step = (execution["tool_name"],
                                execution["target_ref"] if execution["tool_name"] == "play_announcement" else None)
                        if step in remaining:
                            remaining.remove(step)
                    if remaining and any(execution["status"] == "succeeded" for execution in executions):
                        aggregate = "partial"
                    elif remaining and aggregate == "succeeded":
                        aggregate = "cancelled"
                runtime.business.changed("commands", "command_id", command_id, row["run_id"],
                    cancellation_requested_at=runtime.business.clock(), aggregate_status=aggregate)
                for table, column in (("plans", "plan_id"), ("followups", "followup_id")):
                    for resource in runtime.store.db.execute(f"SELECT * FROM {table} WHERE command_id=? AND status NOT IN ('completed','cancelled')", (command_id,)).fetchall():
                        runtime.business.changed(table, column, resource[column], row["run_id"], status="cancelled")
                if device_changed:
                    runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
                result = {"command_id": command_id, "aggregate_status": aggregate, "resource_version": runtime.business.scoped("commands", command_id, "command_id")["resource_version"]}
                runtime.business.audit(session.username, "cancel_command", command_id, row["run_id"])
                runtime.business.save_key(session.username, key, fingerprint, result)
            if device_changed:
                runtime.world = candidate
            return result

    @app.post("/api/v1/executions/{execution_id}/cancel")
    async def cancel_execution(execution_id: str, body: CancelInput, request: Request):
        session = authenticate(request, ["owner", "test_operator"], mutation=True)
        runtime = app.state.runtime
        async with runtime.lock:
            session = authenticate(request, ["owner", "test_operator"], mutation=True)
            validate_session(runtime, session)
            runtime.ensure_business_writable()
            row = runtime.business.scoped("executions", execution_id, "execution_id")
            runtime.ensure_run(row["run_id"])
            key = request.headers.get("idempotency-key")
            fingerprint, old = runtime.business.key(session.username, key, "cancel_execution", {"execution_id": execution_id, **body.model_dump()})
            if old:
                return old
            if row["resource_version"] != body.expected_resource_version:
                raise ApiError(409, "RESOURCE_CHANGED", "실행 버전이 바뀌었습니다.")
            candidate = deepcopy(runtime.world)
            device_changed = False
            with transaction(runtime.store.db):
                if row["status"] == "accepted":
                    if row["tool_name"] == "play_announcement" and row["mode"] == "synthetic_demo":
                        status, device_result, device_changed = runtime.devices.cancel_announcement(candidate, row)
                        runtime.business.changed("executions", "execution_id", execution_id, row["run_id"],
                            status=status, result_json=encoded(device_result),
                            error_code="CANCELLED" if status == "cancelled" else row["error_code"],
                            cancellation_requested_at=runtime.business.clock())
                    else:
                        runtime.business.changed("executions", "execution_id", execution_id, row["run_id"], status="cancelled", error_code="CANCELLED", cancellation_requested_at=runtime.business.clock())
                        notice = runtime.store.db.execute("SELECT notification_id FROM notifications WHERE execution_id=?", (execution_id,)).fetchone()
                        if notice:
                            runtime.business.changed("notifications", "notification_id", notice[0], row["run_id"], delivery_status="failed")
                elif row["status"] == "running":
                    runtime.business.changed("executions", "execution_id", execution_id, row["run_id"], cancellation_requested_at=runtime.business.clock())
                if device_changed:
                    runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
                result = runtime.business.execution_view(runtime.business.scoped("executions", execution_id, "execution_id"))
                runtime.business.audit(session.username, "cancel_execution", execution_id, row["run_id"])
                runtime.business.save_key(session.username, key, fingerprint, result)
            if device_changed:
                runtime.world = candidate
            return result

    @app.get("/devtools", include_in_schema=False)
    async def devtools():
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        return FileResponse(ROOT / "code" / "devtools" / "index.html")

    @app.get("/devtools/app.js", include_in_schema=False)
    async def devtools_script():
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        return FileResponse(ROOT / "code" / "devtools" / "app.js", media_type="text/javascript")

    @app.get("/devtools/operations", include_in_schema=False)
    async def operations_console():
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        return FileResponse(ROOT / "code" / "devtools" / "operations.html")

    @app.get("/devtools/operations.js", include_in_schema=False)
    async def operations_script():
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        return FileResponse(ROOT / "code" / "devtools" / "operations.js", media_type="text/javascript")

    return app
