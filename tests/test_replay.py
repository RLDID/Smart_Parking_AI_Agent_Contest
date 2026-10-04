import asyncio
from copy import deepcopy

import pytest

from backend.auth import ApiError, Auth
from backend.runtime import Runtime
from agent.manual import ManualS1
from simulator.environment import apply_device_command, queue_vehicle_response
from simulator.replay import prepare_replay
from simulator.world import initial_world, public_state, utc_now


def test_recorded_movement_replays_at_its_tick_in_new_run_without_business(tmp_path):
    r = Runtime(tmp_path / "replay.sqlite3")
    session = Auth(r.store).login("demo-operator", "parking-demo-only", "test")[1]
    try:
        r.world = initial_world(7)
        for _ in range(30):
            r.advance_candidate(r.world)
        queue_vehicle_response(r.world, "obj-car-02", "will_move", action_key="recorded-move", delay_ms=200)
        for _ in range(75):
            r.advance_candidate(r.world)
        source = deepcopy(r.world)
        r.store.commit(source, r.event(source))
        r.autonomous.enabled = {"run_id": source["run_id"]}
        result = asyncio.run(r.mutate(session, "replay-1", "control", {"action": "replay"}, source["run_id"]))
        assert result["run_id"] != source["run_id"] and r.autonomous.enabled is None
        before = r.world["actors"][1]["y"]
        for _ in range(30):
            r.advance_candidate(r.world)
        assert r.world["actors"][1]["y"] == before and not r.world.get("action_queue")
        for _ in range(75):
            r.advance_candidate(r.world)
        assert r.world["actors"] == source["actors"]
        assert r.world["action_queue"] == source["action_queue"]
        assert r.world["run_status"] == "paused"
        assert r.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
        with pytest.raises(ApiError) as error:
            asyncio.run(r.mutate(session, "new-input", "control", {"action": "step",
                "action_params": {"request_vehicle_move": "obj-car-02"}}, r.world["run_id"]))
        assert error.value.code == "REPLAY_INPUT_REJECTED"
        assert "recorded_inputs" not in str(public_state(r.world))
    finally:
        r.store.close()


def test_device_input_replay_preserves_recorded_utc_cooldown_and_feedback():
    source = initial_world(1, "s3-closing-v1")
    apply_device_command(source, {"action": "broadcast", "operation_id": "a",
        "zone_id": "announcement-a", "message_id": "closing_notice"}, now_utc=utc_now())
    from simulator.world import advance
    advance(source)
    apply_device_command(source, {"action": "broadcast", "operation_id": "duplicate",
        "zone_id": "announcement-a", "message_id": "closing_notice"}, now_utc=utc_now())
    candidate = initial_world(1, "s3-closing-v1")
    prepare_replay(source, candidate)
    from simulator.replay import apply_replay_inputs
    apply_replay_inputs(candidate)
    advance(candidate)
    apply_replay_inputs(candidate)
    assert candidate["device_state"]["broadcasts"] == source["device_state"]["broadcasts"]
    assert candidate["device_state"]["operations"][-1]["outcome"] == "held"


def test_reset_and_cached_old_requests_cannot_restore_old_run(tmp_path):
    r = Runtime(tmp_path / "reset.sqlite3")
    session = Auth(r.store).login("demo-operator", "parking-demo-only", "test")[1]
    try:
        created = asyncio.run(r.mutate(session, "create", "create", {"seed": 1, "fixture_ref": "s1c-overlap-v1"}))
        run = created["run_id"]
        asyncio.run(r.mutate(session, "step", "control", {"action": "step"}, run))
        reset = asyncio.run(r.mutate(session, "reset", "control", {"action": "reset"}, run))
        assert asyncio.run(r.mutate(session, "reset", "control", {"action": "reset"}, run)) == reset
        for key, op, args in (("create", "create", {"seed": 1, "fixture_ref": "s1c-overlap-v1"}),
                              ("step", "control", {"action": "step"})):
            with pytest.raises(ApiError) as error:
                asyncio.run(r.mutate(session, key, op, args, None if op == "create" else run))
            assert error.value.code == "REQUEST_RUN_CHANGED"
        assert r.world["run_id"] == reset["run_id"]
    finally:
        r.store.close()


def test_recorded_replay_rejects_manual_business_and_new_device_effects(tmp_path):
    r = Runtime(tmp_path / "business-replay.sqlite3")
    session = Auth(r.store).login("demo-operator", "parking-demo-only", "test")[1]
    try:
        r.world = initial_world(1)
        for _ in range(60):
            r.advance_candidate(r.world)
        r.store.commit(r.world, r.event(r.world))
        asyncio.run(r.mutate(session, "replay", "control", {"action": "replay"}, r.world["run_id"]))
        for _ in range(60):
            r.advance_candidate(r.world)
        body = ManualS1(run_id=r.world["run_id"], action="notify")
        with pytest.raises(ApiError) as caught:
            asyncio.run(r.manual_s1a(session, body, "manual-replay"))
        assert caught.value.code == "REPLAY_INPUT_REJECTED"
        with pytest.raises(ApiError) as caught:
            asyncio.run(r.business_tool(session, "report_to_owner", {}, "business-replay", None))
        assert caught.value.code == "REPLAY_INPUT_REJECTED"
        with pytest.raises(ApiError) as caught:
            asyncio.run(r.execute_device_action(session, run_id=r.world["run_id"],
                command_id="command-none", plan_id="plan-none", action="play_announcement",
                zone_id="announcement-a", message_id="closing_notice", knowledge_evidence=None, key="device-replay"))
        assert caught.value.code == "REPLAY_INPUT_REJECTED"
        for table in ("incidents", "notifications", "executions", "plans"):
            assert r.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        r.store.close()


@pytest.mark.parametrize("fixture", ("s1a-foundation-v1", "s1b-blocked-v1", "s1c-overlap-v1"))
def test_restart_keeps_delayed_action_and_requires_explicit_recovery_without_duplicate(tmp_path, fixture):
    database = tmp_path / "pending-restart.sqlite3"
    runtime = Runtime(database)
    reopened = None
    try:
        runtime.world = initial_world(66, fixture)
        queue_vehicle_response(runtime.world, "obj-car-02", "will_move", action_key="pending", delay_ms=1000)
        for _ in range(2):
            runtime.advance_candidate(runtime.world)
        original_actors = deepcopy(runtime.world["actors"])
        original_queue = deepcopy(runtime.world["action_queue"])
        runtime.store.commit(runtime.world, runtime.event(runtime.world))
        runtime.store.close()
        reopened = Runtime(database)
        assert reopened.world["run_status"] == "paused"
        assert reopened.world["recovery_required"] is True
        assert reopened.world["action_queue"] == original_queue
        asyncio.run(reopened.tick())
        assert reopened.world["actors"] == original_actors
        operator = Auth(reopened.store).login("demo-operator", "parking-demo-only", "test")[1]
        asyncio.run(reopened.mutate(operator, "resume", "control", {"action": "start"}, reopened.world["run_id"]))
        for _ in range(2):
            asyncio.run(reopened.tick())
        assert reopened.world["actors"] == original_actors
        for _ in range(8):
            asyncio.run(reopened.tick())
        assert len(reopened.world["action_queue"]) == 1
        assert reopened.world["action_queue"][0]["apply_at_ms"] == 1000
        assert reopened.world["action_queue"][0]["status"] == "moving"
        before = next(a for a in original_actors if a["object_id"] == "obj-car-02")
        after = next(a for a in reopened.world["actors"] if a["object_id"] == "obj-car-02")
        assert (after["x"], after["y"]) != (before["x"], before["y"])
    finally:
        if reopened is not None:
            reopened.store.close()
        else:
            runtime.store.close()


def test_unknown_gate_restart_retains_operation_and_cannot_blindly_close_again(tmp_path):
    database = tmp_path / "unknown-gate.sqlite3"
    runtime = Runtime(database)
    reopened = None
    try:
        world = initial_world(67, "s3-closing-v1")
        runtime.world = world

        def command(action, key, **fields):
            if "gate_id" in fields:
                fields["expected_version"] = world["device_state"]["gates"][0]["resource_version"]
            return apply_device_command(world, {"action": action, "operation_id": key, **fields},
                now_utc=world["device_state"]["now_utc"])

        assert command("broadcast", "restart-notice", zone_id="announcement-a", message_id="closing_notice").outcome == "accepted"
        runtime.advance_candidate(world)
        assert command("set_entry_policy", "restart-deny", gate_id="gate-in-01", target="deny",
            outbound_clear=True, broadcast_operation_id="restart-notice").outcome == "accepted"
        assert command("tick", "restart-sensor", gate_id="gate-in-01").outcome == "accepted"
        assert command("command_gate", "original-close", gate_id="gate-in-01", target="closed").outcome == "accepted"
        assert command("gate_feedback", "lost-feedback", gate_id="gate-in-01", feedback="unknown").outcome == "unknown"
        runtime.store.commit(world, runtime.event(world))
        runtime.store.close()
        reopened = Runtime(database)
        assert reopened.world["recovery_required"] is True
        gate = reopened.world["device_state"]["gates"][0]
        assert gate["physical_state"] == "unknown" and gate["active_operation_id"] == "original-close"
        operator = Auth(reopened.store).login("demo-operator", "parking-demo-only", "test")[1]
        asyncio.run(reopened.mutate(operator, "resume-unknown", "control", {"action": "start"}, reopened.world["run_id"]))
        for _ in range(12):
            asyncio.run(reopened.tick())
        world = reopened.world
        gate = world["device_state"]["gates"][0]
        assert gate["physical_state"] == "unknown" and gate["active_operation_id"] == "original-close"
        retry = command("command_gate", "blind-retry", gate_id="gate-in-01", target="closed")
        assert retry.outcome == "held" and retry.reason == "gate_result_pending"
        assert world["device_state"]["gates"][1]["physical_state"] == "open"
        assert world["device_state"]["broadcasts"][0]["browser_playback"] == "not_requested"
        assert sum(op["operation_id"] == "original-close" for op in world["device_state"]["operations"]) == 1
    finally:
        if reopened is not None:
            reopened.store.close()
        else:
            runtime.store.close()
