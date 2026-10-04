"""Observed SIM-0 paths through the Agent, customer inbox and world adapter."""
import asyncio

import pytest

from backend.auth import Auth
from backend.autonomous import AutonomousService
from backend.business import encoded, ident
from backend.knowledge import transaction
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from contracts.synthetic_users import SyntheticUserInput
from simulator.world import advance, initial_world


@pytest.mark.parametrize("scenario,fixture,impact,zone,ticks", [
    ("s1a", "s1a-foundation-v1", "aisle_obstruction", "aisle-west", 100),
    ("s1b", "s1b-blocked-v1", "exit_blocked", "B01", 230),
    ("s1c", "s1c-overlap-v1", "bay_intrusion", "B01", 300),
])
def test_observed_violation_synthetic_reply_then_clearance(tmp_path, scenario, fixture, impact, zone, ticks):
    async def exercise():
        runtime = Runtime(tmp_path / f"{scenario}.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            authenticate = lambda: auth.require(token)
            runtime.world = initial_world(2, fixture)
            for _ in range(60):
                advance(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            run = runtime.world["run_id"]
            before = runtime.business.impact_assessment(impact, "obj-car-02", zone)
            assert before["support_status"] == "supported" and before["violation_candidate"]
            runtime.synthetic_users.configure(operator, run,
                SyntheticUserInput(mode="will_move", expected_state_version=runtime.world["state_version"]),
                f"{scenario}-consumer")
            service = AutonomousService(runtime)
            result = await service.control(operator, AutonomousControl(run_id=run, action="process",
                mode="mock", scenario=scenario), f"{scenario}-first", authenticate)
            assert result["status"] == "accepted" and result["incident_id"]
            assert runtime.store.db.execute("SELECT type FROM incident_impacts").fetchone()[0] == impact
            assert runtime.world["action_queue"] == []
            assert await runtime.business.deliver_one()
            runtime.synthetic_users.process()
            response = runtime.store.db.execute("SELECT response FROM notification_responses").fetchone()
            assert response[0] == "will_move"
            assert runtime.store.db.execute("SELECT status FROM synthetic_inbox_events").fetchone()[0] == "queued"
            for _ in range(ticks):
                runtime.advance_candidate(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            after = runtime.business.impact_assessment(impact, "obj-car-02", zone)
            assert after["clearance_sustained"] and not after["violation_candidate"]
            followup = await service.control(operator, AutonomousControl(run_id=run, action="process",
                mode="mock", scenario=scenario), f"{scenario}-followup", authenticate)
            assert followup["status"] == "resolved"
            assert runtime.store.db.execute("SELECT status FROM incidents").fetchone()[0] == "resolved"
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_s2_safety_claim_and_agent_report_are_independent(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path / "s2.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            runtime.world = initial_world(6, "s2-crossing-v1")
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            candidate = None
            for _ in range(45):
                runtime.advance_candidate(runtime.world)
                runtime.safety.step(runtime.world)
                matches = [x for x in runtime.operating_candidates("s2") if x["assessment"]["violation_candidate"]]
                if len(matches) == 1:
                    candidate = matches[0]
                    break
            assert candidate and candidate["assessment"]["support_status"] == "supported"
            assert runtime.world["safety_state"]["claims"]
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            service = AutonomousService(runtime)
            result = await service.control(operator, AutonomousControl(run_id=runtime.world["run_id"],
                action="process", mode="mock", scenario="s2"), "risk-report", lambda: auth.require(token))
            assert result["status"] == "accepted" and result["report"]["status"] == "accepted"
            assert runtime.store.db.execute("SELECT type FROM incident_impacts").fetchone()[0] == "approach_risk"
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 1
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_logout_between_agent_plan_and_device_call_blocks_execution(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path / "logout.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, owner = auth.login("demo-owner", "parking-demo-only", "test")
            runtime.world = initial_world(3, "s3-closing-v1")
            for _ in range(60):
                runtime.advance_candidate(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            run = runtime.world["run_id"]
            command_id = ident("command")
            with transaction(runtime.store.db):
                runtime.business.insert("commands", command_id=command_id, facility_id="fac-demo-01",
                    run_id=run, requester_id=owner.username, request_text="영업 종료 후 입차 제한, 출차 허용",
                    purpose="operational_goal", target_vehicle_id=None, aggregate_status="pending",
                    normalized_goal_json=None)
            service = AutonomousService(runtime)
            body = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s3",
                                     command_id=command_id)
            preview = await service.control(owner, body, "preview", lambda: auth.require(token))
            assert preview["status"] == "confirmation_required"
            with transaction(runtime.store.db):
                runtime.business.changed("plans", "plan_id", preview["plan_id"], run, status="active")
                runtime.business.changed("commands", "command_id", command_id, run,
                    normalized_goal_json=encoded({"kind": "closing", "confirmed": True,
                                                  "clarified": False}), aggregate_status="running")
            original = runtime.execute_device_action
            async def logout_before_device(*args, **kwargs):
                auth.sessions.pop(token)
                return await original(*args, **kwargs)
            runtime.execute_device_action = logout_before_device
            with pytest.raises(Exception) as error:
                await service.control(owner, body, "broadcast-after-logout", lambda: auth.require(token))
            assert getattr(error.value, "code", None) == "UNAUTHENTICATED"
            assert runtime.store.db.execute("SELECT count(*) FROM executions WHERE tool_name='play_announcement'").fetchone()[0] == 0
        finally:
            runtime.store.close()
    asyncio.run(exercise())
