"""Local command review and agent endpoints with a disposable server state."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from backend.auth import ApiError, Auth
from backend.autonomous import AutonomousService
from backend.autonomous_routes import install_autonomous_routes
from backend.business import ident
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from simulator.environment import set_synthetic_fault, queue_portal_attempt
from simulator.world import FACILITY, advance, initial_world


@pytest.mark.parametrize("failure_code", ["MODEL_TIMEOUT", "MODEL_RATE_LIMIT"])
def test_sent_fallback_failure_is_structured_and_cannot_replay(tmp_path, failure_code):
    from agent.providers import ProviderError
    from backend.app import Settings, create_app
    from test_live_agent import configuration

    class FailingClient:
        def __init__(self, ready):
            self.ready, self.calls = ready, 0
        def credentials_ready(self):
            return self.ready
        def input_token_bound(self, _input):
            return 2048
        async def complete(self, _input):
            self.calls += 1
            raise ProviderError(failure_code)

    app = create_app(Settings(database=tmp_path / "failure.sqlite3", test_control=True,
        origins=("http://testserver",), background_ticks=False,
        live_configuration=configuration(), budget_database=tmp_path / "cost.sqlite3"))
    async def exercise():
        async with app.router.lifespan_context(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            runtime = app.state.runtime
            clients = {"openai": FailingClient(False), "gemini": FailingClient(True)}
            models = runtime.queries.live_models
            models.client_factory = lambda provider, *_: clients[provider]
            token, session = app.state.auth.login("demo-operator", "parking-demo-only", "test")
            client.cookies.set("parking_session", token)
            runtime.world = initial_world(42)
            for _ in range(60):
                advance(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            headers = {"origin": "http://testserver", "x-csrf-token": session.csrf,
                       "idempotency-key": "failed-fallback"}
            body = {"run_id": runtime.world["run_id"], "action": "process", "mode": "live", "scenario": "s1a"}
            response = await client.post("/api/v1/test/agent/operations", json=body, headers=headers)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == failure_code
            if failure_code == "MODEL_RATE_LIMIT":
                assert "잔액" in response.json()["error"]["message"]
            row = runtime.store.db.execute("SELECT status,result_json FROM autonomous_jobs").fetchone()
            saved = json.loads(row["result_json"])
            assert row["status"] == "pending" and saved["reconciliation_required"]
            assert saved["reason_code"] == failure_code
            assert saved["model"]["provider"] == "gemini"
            assert saved["model"]["usage_status"] == "unknown"
            assert saved["model"]["cost_pending_krw"] > 0
            same = await client.post("/api/v1/test/agent/operations", json=body, headers=headers)
            assert same.status_code == 409
            assert same.json()["error"]["code"] == "JOB_RECONCILIATION_REQUIRED"
            other = await client.post("/api/v1/test/agent/operations", json=body,
                                headers=headers | {"idempotency-key": "new-key"})
            assert other.status_code == 503
            assert other.json()["error"]["code"] == failure_code
            assert clients["openai"].calls == 0 and clients["gemini"].calls == 2
            assert models.public_status()["budget"]["unknown_count"] == 2
            assert not runtime.autonomous.active
            assert runtime.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0

    asyncio.run(exercise())


def test_local_s3_clarify_preview_confirm_uses_same_command(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path / "routes.sqlite3")
        runtime.world = initial_world(3, "s3-closing-v1")
        for _ in range(60):
            advance(runtime.world)
        runtime.store.commit(runtime.world, Runtime.event(runtime.world))
        assert runtime.store.db.execute("PRAGMA user_version").fetchone()[0] == 6
        runtime.autonomous = AutonomousService(runtime)
        auth = Auth(runtime.store)
        token, session = auth.login("demo-owner", "parking-demo-only", "test")
        app = FastAPI()
        app.state.runtime, app.state.auth = runtime, auth

        @app.exception_handler(ApiError)
        async def handle(_request: Request, error: ApiError):
            return JSONResponse({"code": error.code}, status_code=error.status)

        def authenticate(request, roles=None, mutation=False):
            value = auth.require(request.cookies.get("parking_session"), roles)
            if mutation and request.headers.get("x-csrf-token") != value.csrf:
                raise ApiError(403, "CSRF_REJECTED", "CSRF")
            return value

        install_autonomous_routes(app, authenticate, lambda facility: None,
                                  SimpleNamespace(test_control=True))
        cid = ident("command")
        runtime.business.insert("commands", command_id=cid, facility_id=FACILITY,
            run_id=runtime.world["run_id"], requester_id=session.username,
            request_text="문 닫아", purpose="operational_goal", target_vehicle_id=None,
            aggregate_status="pending", normalized_goal_json=None)
        runtime.store.db.commit()
        headers = {"x-csrf-token": session.csrf}
        body = {"run_id": runtime.world["run_id"], "action": "process", "mode": "mock",
                "scenario": "s3", "command_id": cid}
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                                   cookies={"parking_session": token}) as client:
                first = await client.post("/api/v1/test/agent/operations", json=body,
                                          headers=headers | {"idempotency-key": "s3-first"})
                assert first.status_code == 200 and first.json()["status"] == "clarification_required"
                clarified = await client.post(f"/api/v1/commands/{cid}/clarify",
                    json={"expected_resource_version": 1, "goal": "closing"},
                    headers=headers | {"idempotency-key": "clarify-s3"})
                assert clarified.status_code == 200 and clarified.json()["resource_version"] == 2
                proposed = await client.post("/api/v1/test/agent/operations", json=body,
                    headers=headers | {"idempotency-key": "s3-plan"})
                assert proposed.status_code == 200 and proposed.json()["status"] == "confirmation_required"
                preview = await client.get(f"/api/v1/commands/{cid}/plan")
                assert preview.json()["status"] == "proposed"
                assert len(preview.json()["steps"]) == 3
                original_step_ids = [step["step_id"] for step in preview.json()["steps"]]
                assert len(set(original_step_ids)) == 3
                confirmed = await client.post(f"/api/v1/commands/{cid}/confirm",
                    json={"expected_resource_version": preview.json()["command_version"]},
                    headers=headers | {"idempotency-key": "confirm-s3"})
                assert confirmed.status_code == 200 and confirmed.json()["plan_id"] == preview.json()["plan_id"]
                assert (await client.get(f"/api/v1/commands/{cid}/plan")).json()["status"] == "active"
                jobs = await client.get("/api/v1/test/agent/operations/jobs", params={"run_id": runtime.world["run_id"]})
                assert jobs.status_code == 200 and len(jobs.json()["items"]) == 2
                def command_context():
                    control = AutonomousControl.model_validate(body)
                    return runtime.autonomous._snapshot(session, control,
                        runtime.read_task(session, body["run_id"]))["command"]["normalized_goal"]
                for index, zone in enumerate(("announcement-a", "announcement-b"), 1):
                    action = await client.post("/api/v1/test/agent/operations", json=body,
                        headers=headers | {"idempotency-key": f"s3-broadcast-{index}"})
                    assert action.status_code == 200 and action.json()["status"] == "accepted"
                    waiting = command_context()
                    assert waiting["action"] == "hold"
                    assert zone not in waiting["execution_evidence"]["played_zones"]
                    assert runtime.store.db.execute("SELECT target_ref FROM executions WHERE tool_name='play_announcement' ORDER BY rowid DESC LIMIT 1").fetchone()[0] == zone
                    for _ in range(2):
                        runtime.advance_candidate(runtime.world)
                    runtime.store.commit(runtime.world, Runtime.event(runtime.world))
                    runtime.devices.reconcile()
                    played = command_context()
                    assert played["execution_evidence"]["played_zones"] == list(("announcement-a", "announcement-b")[:index])
                    assert played["action"] == ("announce" if index == 1 else "restrict_entry")
                set_synthetic_fault(runtime.world, "gate", True)
                for _ in range(3):
                    runtime.advance_candidate(runtime.world)
                runtime.store.commit(runtime.world, Runtime.event(runtime.world))
                held = await client.post("/api/v1/test/agent/operations", json=body,
                    headers=headers | {"idempotency-key": "s3-entry-held"})
                assert held.status_code == 200 and held.json()["status"] == "held"
                assert next(g for g in runtime.public_devices(runtime.world)["gates"] if g["direction"] == "entry")["entry_policy"] == "allow"
                await runtime.autonomous.control(session,
                    AutonomousControl(run_id=runtime.world["run_id"], action="start", mode="mock"),
                    "s3-watch-start", lambda: auth.require(token))
                for _ in range(4):
                    await runtime.autonomous.tick()
                    await asyncio.sleep(0.03)
                held_attempts = runtime.store.db.execute("SELECT count(*) FROM executions WHERE tool_name='set_entry_policy'").fetchone()[0]
                assert held_attempts <= 2
                await runtime.autonomous.control(session,
                    AutonomousControl(run_id=runtime.world["run_id"], action="stop", mode="mock"),
                    "s3-watch-stop", lambda: auth.require(token))
                set_synthetic_fault(runtime.world, "gate", False)
                for _ in range(2):
                    runtime.advance_candidate(runtime.world)
                runtime.store.commit(runtime.world, Runtime.event(runtime.world))
                gate = await client.post("/api/v1/test/agent/operations", json=body,
                    headers=headers | {"idempotency-key": "s3-entry-deny"})
                assert gate.status_code == 200 and gate.json()["status"] == "succeeded"
                linked_plan = runtime.business.scoped("plans", preview.json()["plan_id"], "plan_id")
                linked_steps = json.loads(linked_plan["steps_json"])
                assert [step["step_id"] for step in linked_steps] == original_step_ids
                assert len(linked_steps[2]["execution_ids"]) == held_attempts + 1
                for step in linked_steps:
                    for execution_id in step["execution_ids"]:
                        linked_execution = runtime.business.scoped("executions", execution_id, "execution_id")
                        assert json.loads(linked_execution["payload_json"])["step_id"] == step["step_id"]
                devices = runtime.public_devices(runtime.world)
                assert next(g for g in devices["gates"] if g["direction"] == "entry")["entry_policy"] == "deny"
                assert next(g for g in devices["gates"] if g["direction"] == "exit")["physical_state"] == "open"
                queue_portal_attempt(runtime.world, "obj-car-s3-u", action_key="new-entry")
                queue_portal_attempt(runtime.world, "obj-car-s3-w", action_key="existing-exit")
                for _ in range(70):
                    runtime.advance_candidate(runtime.world)
                assert not runtime.world["s3_entered"] and runtime.world["s3_exited"]
                entry_action, exit_action = runtime.world["action_queue"]
                assert entry_action["status"] == "waiting_at_gate"
                assert exit_action["status"] == "completed"
        finally:
            runtime.store.close()
    asyncio.run(exercise())
