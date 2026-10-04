"""Regressions for watcher authority, command selection, and S1 follow-up."""
import asyncio
import json

import pytest

from backend.auth import ApiError, Auth
from backend.autonomous import AutonomousService
from backend.business import encoded, ident
from backend.knowledge import transaction
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from contracts.business import ResponseInput
from simulator.environment import queue_vehicle_response
from simulator.world import FACILITY, advance, initial_world


def setup(tmp_path, fixture="s1a-foundation-v1"):
    runtime = Runtime(tmp_path / "regression.sqlite3")
    auth = Auth(runtime.store)
    token, operator = auth.login("demo-operator", "parking-demo-only", "test")
    runtime.world = initial_world(2 if fixture != "s3-closing-v1" else 3, fixture)
    for _ in range(60):
        advance(runtime.world)
    runtime.store.commit(runtime.world, Runtime.event(runtime.world))
    return runtime, AutonomousService(runtime), auth, operator, lambda: auth.require(token)


def command(runtime, operator, text="문 닫아"):
    cid = ident("command")
    with transaction(runtime.store.db):
        runtime.business.insert("commands", command_id=cid, facility_id=FACILITY,
            run_id=runtime.world["run_id"], requester_id=operator.username,
            request_text=text, purpose="operational_goal", target_vehicle_id=None,
            aggregate_status="pending", normalized_goal_json=None)
    return cid


async def activate_notice(runtime, service, operator, authenticate):
    run = runtime.world["run_id"]
    cid = command(runtime, operator, "A구역에 쓰레기 버리지 말라고 안내해 줘")
    body = AutonomousControl(run_id=run, action="process", scenario="s3", command_id=cid)
    preview = await service.control(operator, body, "notice-preview", authenticate)
    assert preview["status"] == "confirmation_required"
    pid = preview["plan_id"]
    with transaction(runtime.store.db):
        runtime.business.changed("plans", "plan_id", pid, run, status="active")
        runtime.business.changed("commands", "command_id", cid, run,
            normalized_goal_json=encoded({"kind": "zone_notice", "zone_id": "announcement-a",
                                          "clarified": True, "confirmed": True}), aggregate_status="running")
    return cid, pid


def test_stop_then_restart_cannot_dispatch_old_watcher(tmp_path):
    async def exercise():
        runtime, service, _, operator, authenticate = setup(tmp_path, "s3-closing-v1")
        try:
            run = runtime.world["run_id"]
            cid, _ = await activate_notice(runtime, service, operator, authenticate)
            entered, release = asyncio.Event(), asyncio.Event()
            original = runtime.execute_device_action
            async def pause_before_writer(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)
            runtime.execute_device_action = pause_before_writer
            await service.control(operator, AutonomousControl(run_id=run, action="start"), "watch-start", authenticate)
            await service.tick()
            await asyncio.wait_for(entered.wait(), 5)
            await service.control(operator, AutonomousControl(run_id=run, action="stop"), "watch-stop", authenticate)
            await service.control(operator, AutonomousControl(run_id=run, action="start"), "watch-restart", authenticate)
            release.set()
            for _ in range(30):
                if not service.active:
                    break
                await asyncio.sleep(0.02)
            assert runtime.store.db.execute("SELECT count(*) FROM executions WHERE tool_name='play_announcement'").fetchone()[0] == 0
            assert runtime.store.db.execute("SELECT status FROM autonomous_jobs ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "held"
            manual = await service.control(operator, AutonomousControl(run_id=run, action="process",
                scenario="s3", command_id=cid), "manual-after-stop", authenticate)
            assert manual["status"] == "accepted"
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_cancelled_broadcast_does_not_spawn_replacement(tmp_path):
    async def exercise():
        runtime, service, _, operator, authenticate = setup(tmp_path, "s3-closing-v1")
        try:
            run = runtime.world["run_id"]
            cid, pid = await activate_notice(runtime, service, operator, authenticate)
            body = AutonomousControl(run_id=run, action="process", scenario="s3", command_id=cid)
            first = await service.control(operator, body, "notice-once", authenticate)
            assert first["status"] == "accepted"
            with transaction(runtime.store.db):
                runtime.business.changed("executions", "execution_id", first["execution"]["execution_id"],
                                         run, status="cancelled")
            await service.control(operator, AutonomousControl(run_id=run, action="start"), "watch-on", authenticate)
            for _ in range(6):
                await service.tick()
                await asyncio.sleep(0.04)
            assert runtime.store.db.execute("SELECT count(*) FROM executions WHERE tool_name='play_announcement'").fetchone()[0] == 1
            assert runtime.store.db.execute("SELECT status FROM plans WHERE plan_id=?", (pid,)).fetchone()[0] == "held"
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_waiting_commands_each_get_one_proposal_then_s1_runs(tmp_path):
    async def exercise():
        runtime, service, _, operator, authenticate = setup(tmp_path)
        try:
            run = runtime.world["run_id"]
            first = command(runtime, operator)
            second = command(runtime, operator)
            await service.control(operator, AutonomousControl(run_id=run, action="start"), "start", authenticate)
            for _ in range(8):
                await service.tick()
                await asyncio.sleep(0.08)
            scenarios = [row[0] for row in runtime.store.db.execute("SELECT scenario FROM autonomous_jobs ORDER BY rowid")]
            assert scenarios.count("s3") == 2
            assert any(item.startswith("s1") for item in scenarios)
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1
            assert first != second
        finally:
            runtime.store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("scenario,fixture,impact,zone,ticks", [
    ("s1a", "s1a-foundation-v1", "aisle_obstruction", "aisle-west", 100),
    ("s1b", "s1b-blocked-v1", "exit_blocked", "B01", 230),
    ("s1c", "s1c-overlap-v1", "bay_intrusion", "B01", 300),
])
@pytest.mark.parametrize("response", ["cannot_move", "question"])
def test_clearance_resolves_before_response_report(tmp_path, scenario, fixture, impact, zone, ticks, response):
    async def exercise():
        runtime, service, auth, operator, authenticate = setup(tmp_path, fixture)
        try:
            run = runtime.world["run_id"]
            first = await service.control(operator, AutonomousControl(run_id=run, action="process",
                mode="mock", scenario=scenario), "first", authenticate)
            assert first["status"] == "accepted"
            assert await runtime.business.deliver_one()
            notice = runtime.store.db.execute("SELECT notification_id,recipient_user_id FROM notifications WHERE purpose='move_request'").fetchone()
            driver = auth.login(notice["recipient_user_id"], "parking-demo-only", "test")[1]
            runtime.business.reply(driver, notice["notification_id"], "response",
                ResponseInput(client_request_id="driver-response", response=response), "driver-response")
            assert queue_vehicle_response(runtime.world, "obj-car-02", "will_move",
                                          action_key="independent-movement", delay_ms=0)["status"] == "queued"
            for _ in range(ticks):
                runtime.advance_candidate(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            assert runtime.business.impact_assessment(impact, "obj-car-02", zone)["clearance_sustained"]
            result = await service.control(operator, AutonomousControl(run_id=run, action="process",
                mode="mock", scenario=scenario), "clearance", authenticate)
            assert result["status"] == "resolved"
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 0
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_unchanged_report_is_idempotent_across_process_keys(tmp_path):
    async def exercise():
        runtime, service, auth, operator, authenticate = setup(tmp_path)
        try:
            run = runtime.world["run_id"]
            body = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s1a")
            await service.control(operator, body, "first", authenticate)
            await runtime.business.deliver_one()
            notice = runtime.store.db.execute("SELECT notification_id,recipient_user_id FROM notifications WHERE purpose='move_request'").fetchone()
            driver = auth.login(notice["recipient_user_id"], "parking-demo-only", "test")[1]
            async with runtime.lock:
                runtime.business.reply(driver, notice["notification_id"], "response",
                    ResponseInput(client_request_id="cannot", response="cannot_move"), "cannot")
            first = await service.control(operator, body, "report-one", authenticate)
            second = await service.control(operator, body, "report-two", authenticate)
            assert first["status"] == second["status"] == "accepted"
            assert first["report"]["execution_id"] == second["report"]["execution_id"]
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 1
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_cancelled_plan_is_not_reused_after_goal_change(tmp_path):
    async def exercise():
        runtime, service, _, operator, authenticate = setup(tmp_path, "s3-closing-v1")
        try:
            run = runtime.world["run_id"]
            cid = command(runtime, operator, "영업 종료 후 입차 제한, 출차 허용")
            body = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s3", command_id=cid)
            old = await service.control(operator, body, "closing-preview", authenticate)
            assert old["status"] == "confirmation_required"
            with transaction(runtime.store.db):
                runtime.business.changed("plans", "plan_id", old["plan_id"], run, status="cancelled")
                runtime.business.changed("commands", "command_id", cid, run,
                    normalized_goal_json=encoded({"kind": "zone_notice", "zone_id": "announcement-a",
                                                  "clarified": True, "confirmed": False}), aggregate_status="pending")
            new = await service.control(operator, body, "notice-preview", authenticate)
            assert new["status"] == "confirmation_required" and new["plan_id"] != old["plan_id"]
            assert runtime.store.db.execute("SELECT status FROM plans WHERE plan_id=?", (old["plan_id"],)).fetchone()[0] == "cancelled"
            assert len(json.loads(runtime.store.db.execute("SELECT steps_json FROM plans WHERE plan_id=?", (new["plan_id"],)).fetchone()[0])) == 1
        finally:
            runtime.store.close()
    asyncio.run(exercise())
