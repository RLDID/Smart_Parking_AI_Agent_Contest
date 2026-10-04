"""Local authenticated development surface for autonomous work and S3 review."""
import json

from fastapi import Request

from backend.auth import ApiError
from backend.knowledge import transaction
from agent.tools import validate_session
from agent.loop import ModelFailure
from contracts.autonomous import AutonomousControl, CommandClarification
from contracts.business import CancelInput
from simulator.world import FACILITY


def install_autonomous_routes(app, authenticate, facility_check, settings):
    def service():
        runtime = app.state.runtime
        worker = getattr(runtime, "autonomous", None)
        if worker is None:
            raise ApiError(503, "AGENT_UNAVAILABLE", "업무 Agent가 연결되지 않았습니다.")
        return runtime, worker

    def local_control():
        if not settings.test_control:
            raise ApiError(403, "TEST_CONTROL_DISABLED", "로컬 업무 시험이 비활성화되어 있습니다.")

    @app.post("/api/v1/test/agent/operations")
    async def autonomous_control(body: AutonomousControl, request: Request):
        local_control()
        session = authenticate(request, ["owner", "test_operator"], mutation=True)
        runtime, worker = service()
        token = request.cookies.get("parking_session")
        def still_authenticated():
            if app.state.auth.lookup(token) is not session:
                raise ApiError(401, "UNAUTHENTICATED", "업무 세션을 다시 확인하세요.")
        try:
            return await worker.control(session, body, request.headers.get("idempotency-key"), still_authenticated)
        except ModelFailure as error:
            message = ("제공자 호출 한도 또는 결제 한도에 걸렸습니다. 대시보드에서 잔액·할당량을 확인하고 잔액이 부족하면 충전하세요."
                       if error.reason_code == "MODEL_RATE_LIMIT" else
                       "모델 업무를 완료하지 못했습니다. 작업 상태와 비용 장부를 확인하세요.")
            raise ApiError(503, error.reason_code,
                           message) from None

    @app.get("/api/v1/test/agent/operations")
    async def autonomous_status(run_id: str, request: Request):
        local_control()
        session = authenticate(request, ["owner", "test_operator"])
        runtime, worker = service()
        async with runtime.lock:
            validate_session(runtime, session)
            runtime.ensure_run(run_id)
            enabled = worker.enabled
            return {"run_id": run_id, "enabled": bool(enabled and enabled["run_id"] == run_id),
                    "mode": enabled["mode"] if enabled and enabled["run_id"] == run_id else None,
                    "active_jobs": len(worker.active)}

    @app.get("/api/v1/test/agent/operations/jobs")
    async def autonomous_jobs(run_id: str, request: Request, limit: int = 20):
        local_control()
        session = authenticate(request, ["owner", "test_operator"])
        runtime, _ = service()
        if not 1 <= limit <= 50:
            raise ApiError(422, "INVALID_INPUT", "조회 개수를 확인하세요.")
        async with runtime.lock:
            validate_session(runtime, session)
            runtime.ensure_run(run_id)
            rows = runtime.store.db.execute("""SELECT job_id,mode,scenario,trigger_key,status,result_json,created_at,updated_at
                FROM autonomous_jobs WHERE facility_id=? AND run_id=? ORDER BY rowid DESC LIMIT ?""",
                (FACILITY, run_id, limit)).fetchall()
            return {"run_id": run_id, "items": [{**dict(row), "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "result_json": None} for row in rows]}

    @app.post("/api/v1/commands/{command_id}/clarify")
    async def clarify_command(command_id: str, body: CommandClarification, request: Request):
        local_control()
        session = authenticate(request, ["owner", "test_operator"], mutation=True)
        runtime, _ = service()
        async with runtime.lock:
            validate_session(runtime, session)
            row = runtime.business.scoped("commands", command_id, "command_id")
            runtime.ensure_run(row["run_id"])
            key = request.headers.get("idempotency-key")
            fingerprint, old = runtime.business.key(session.username, key, "clarify_command",
                {"command_id": command_id, **body.model_dump()})
            if old:
                return old
            if row["resource_version"] != body.expected_resource_version or row["cancellation_requested_at"]:
                raise ApiError(409, "COMMAND_CHANGED", "지시 버전과 취소 상태를 확인하세요.")
            if row["purpose"] != "operational_goal":
                raise ApiError(422, "INVALID_PURPOSE", "운영 명령만 명확화할 수 있습니다.")
            with transaction(runtime.store.db):
                for plan in runtime.store.db.execute("SELECT plan_id FROM plans WHERE command_id=? AND status IN ('proposed','active','held')", (command_id,)).fetchall():
                    runtime.business.changed("plans", "plan_id", plan["plan_id"], row["run_id"], status="cancelled")
                runtime.business.changed("commands", "command_id", command_id, row["run_id"],
                    normalized_goal_json=json.dumps({"kind": body.goal, "zone_id": body.zone_id,
                                                     "clarified": True, "confirmed": False}),
                    aggregate_status="pending")
                current = runtime.business.scoped("commands", command_id, "command_id")
                result = {"command_id": command_id, "aggregate_status": "pending",
                          "resource_version": current["resource_version"], "goal": body.goal}
                runtime.business.audit(session.username, "clarify_command", command_id, row["run_id"])
                runtime.business.save_key(session.username, key, fingerprint, result)
                return result

    @app.get("/api/v1/commands/{command_id}/plan")
    async def command_plan(command_id: str, request: Request):
        session = authenticate(request, ["owner", "test_operator"])
        runtime, _ = service()
        async with runtime.lock:
            validate_session(runtime, session)
            row = runtime.business.scoped("commands", command_id, "command_id")
            runtime.ensure_run(row["run_id"])
            plan = runtime.store.db.execute("SELECT * FROM plans WHERE command_id=? AND status!='cancelled' ORDER BY rowid DESC LIMIT 1", (command_id,)).fetchone()
            if not plan:
                return {"command_id": command_id, "status": "not_ready", "command_version": row["resource_version"]}
            return {"command_id": command_id, "command_version": row["resource_version"],
                    "plan_id": plan["plan_id"], "plan_version": plan["resource_version"],
                    "status": plan["status"], "steps": json.loads(plan["steps_json"]),
                    "goal": json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else None}

    @app.post("/api/v1/commands/{command_id}/confirm")
    async def confirm_command(command_id: str, body: CancelInput, request: Request):
        local_control()
        session = authenticate(request, ["owner", "test_operator"], mutation=True)
        runtime, worker = service()
        async with runtime.lock:
            validate_session(runtime, session)
            row = runtime.business.scoped("commands", command_id, "command_id")
            runtime.ensure_run(row["run_id"])
            key = request.headers.get("idempotency-key")
            fingerprint, old = runtime.business.key(session.username, key, "confirm_command",
                {"command_id": command_id, **body.model_dump()})
            if old:
                return old
            if row["resource_version"] != body.expected_resource_version or row["cancellation_requested_at"]:
                raise ApiError(409, "COMMAND_CHANGED", "지시 버전과 취소 상태를 확인하세요.")
            plan = runtime.store.db.execute("SELECT * FROM plans WHERE command_id=? AND status='proposed' ORDER BY rowid DESC LIMIT 1", (command_id,)).fetchone()
            if not plan:
                raise ApiError(409, "PLAN_NOT_READY", "검토 가능한 계획이 아직 없습니다.")
            goal = json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else None
            if not goal or goal.get("kind") not in ("closing", "zone_notice") or goal.get("confirmed") is True:
                raise ApiError(409, "GOAL_NOT_READY", "명확한 운영 목표를 확인하세요.")
            if not worker._plan_matches(plan, goal["kind"]):
                raise ApiError(409, "PLAN_NOT_READY", "현재 목표에 맞는 계획을 다시 검토하세요.")
            if runtime.knowledge.current_policy(FACILITY).policy_version != plan["policy_version"]:
                raise ApiError(409, "POLICY_CHANGED", "현재 운영 기준으로 계획을 다시 검토하세요.")
            with transaction(runtime.store.db):
                runtime.business.changed("plans", "plan_id", plan["plan_id"], row["run_id"], status="active")
                runtime.business.changed("commands", "command_id", command_id, row["run_id"],
                    normalized_goal_json=json.dumps(goal | {"confirmed": True}), aggregate_status="running")
                current = runtime.business.scoped("commands", command_id, "command_id")
                result = {"command_id": command_id, "aggregate_status": "running",
                          "resource_version": current["resource_version"], "plan_id": plan["plan_id"]}
                runtime.business.audit(session.username, "confirm_command", command_id, row["run_id"])
                runtime.business.save_key(session.username, key, fingerprint, result)
                return result
