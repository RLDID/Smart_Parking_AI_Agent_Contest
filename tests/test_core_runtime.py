"""SIM-0 end-to-end core checks against the integrated Runtime.

These tests use disposable SQLite databases and no model, network, or browser.
"""

import asyncio
from copy import deepcopy
import json

import pytest

from backend.auth import ApiError, Auth, Session
from backend.runtime import Runtime
from contracts.environment_controls import DeviceFaultInput, S2ReactionInput
from contracts.synthetic_users import SyntheticUserInput
from simulator.world import FACILITY


OPERATOR = Session("demo-operator", "test_operator", "test-only", 999999999)


def run(coroutine):
    return asyncio.run(coroutine)


async def create(runtime, fixture, key="create"):
    return await runtime.mutate(OPERATOR, key, "create", {
        "facility_id": FACILITY, "fixture_ref": fixture,
        "config_ref": "foundation-v1" if fixture == "s1a-foundation-v1" else "sim0-v1",
        "seed": 19,
    })


async def ticks(runtime, count):
    for _ in range(count):
        await runtime.tick()


@pytest.mark.parametrize("fixture,mode,faults,expect_contact,expect_claim", [
    ("s2-crossing-v1", "brake_on_alarm", (), False, True),
    ("s2-crossing-v1", "no_response", (), True, True),
    ("s2-crossing-v1", "brake_on_alarm", ("visual", "audio"), True, True),
    ("s2-offset-v1", "brake_on_alarm", (), False, False),
])
def test_independent_safety_feedback_and_contact_outcomes(
        tmp_path, monkeypatch, fixture, mode, faults, expect_contact, expect_claim):
    runtime = Runtime(tmp_path / "safety.sqlite3")
    try:
        async def exercise():
            created = await create(runtime, fixture)
            run_id = created["run_id"]
            if mode == "no_response":
                body = S2ReactionInput(expected_state_version=runtime.world["state_version"],
                                       mode=mode)
                await runtime.environment_control(OPERATOR, run_id, body, "reaction", "s2_reaction")
            for channel in faults:
                body = DeviceFaultInput(expected_state_version=runtime.world["state_version"],
                                        channel=channel, failed=True)
                await runtime.environment_control(OPERATOR, run_id, body,
                                                  "fault-" + channel, "device_fault")
            # The autonomous/model path and knowledge index cannot authorize
            # or suppress the independent safety tick.
            runtime.autonomous.enabled = None
            monkeypatch.setattr(runtime.knowledge, "_index", lambda *_: (_ for _ in ()).throw(RuntimeError("RAG down")))
            await runtime.mutate(OPERATOR, "start", "control", {"action": "start"}, run_id)
            await ticks(runtime, 50)
            claims = runtime.world.get("safety_state", {}).get("claims", {})
            assert bool(claims) is expect_claim or (expect_claim and
                    runtime.world["device_state"]["alarms"][0]["resource_version"] > 0)
            assert (runtime.world["s2_contact_at_ms"] is not None) is expect_contact
            alarm = runtime.world["device_state"]["alarms"][0]
            if faults:
                assert alarm["visual"] == alarm["audio"] == "failed"
                assert runtime.world["s2_alarm_seen_ms"] is None
            if fixture == "s2-crossing-v1" and not faults:
                assert runtime.world.get("safety_state", {}).get("analysis_status")
        run(exercise())
    finally:
        runtime.store.close()


def test_runtime_step_storage_failure_rolls_back_world_and_database(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "rollback.sqlite3")
    try:
        created = run(create(runtime, "s1a-foundation-v1"))
        before = deepcopy(runtime.world)
        events_before = len(runtime.store.events())
        original = runtime.store.commit

        def fail_after_writes(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic disk failure before transaction commit")

        monkeypatch.setattr(runtime.store, "commit", fail_after_writes)
        with pytest.raises(RuntimeError):
            run(runtime.mutate(OPERATOR, "step-fail", "control", {"action": "step"}, created["run_id"]))
        assert runtime.world == before
        assert runtime.store.load() == before
        assert len(runtime.store.events()) == events_before
        assert runtime.store.previous_request(OPERATOR.username, "step-fail") is None
    finally:
        runtime.store.close()


def test_reset_replay_isolate_current_run_and_recorded_inputs(tmp_path):
    runtime = Runtime(tmp_path / "replay.sqlite3")
    try:
        async def exercise():
            created = await create(runtime, "s1b-blocked-v1")
            first_id = created["run_id"]
            await runtime.mutate(OPERATOR, "move", "control", {
                "action": "step", "action_params": {"request_vehicle_move": "obj-car-02"}}, first_id)
            for index in range(250):
                await runtime.mutate(OPERATOR, "step-" + str(index), "control", {"action": "step"}, first_id)
            source_time = runtime.world["sim_time_ms"]
            source_x = next(a["x"] for a in runtime.world["actors"] if a["object_id"] == "obj-car-02")
            assert runtime.world["recorded_inputs"]
            replayed = await runtime.mutate(OPERATOR, "replay", "control", {"action": "replay"}, first_id)
            replay_id = replayed["run_id"]
            assert replay_id != first_id and runtime.world["sim_time_ms"] == 0
            with pytest.raises(ApiError) as error:
                await runtime.mutate(OPERATOR, "stale", "control", {"action": "step"}, first_id)
            assert error.value.code == "RUN_NOT_FOUND"
            with pytest.raises(ApiError) as error:
                await runtime.mutate(OPERATOR, "inject", "control", {
                    "action": "step", "action_params": {"request_vehicle_move": "obj-car-02"}}, replay_id)
            assert error.value.code == "REPLAY_INPUT_REJECTED"
            for index in range(source_time // 100):
                await runtime.mutate(OPERATOR, "replay-step-" + str(index), "control",
                                     {"action": "step"}, replay_id)
            assert runtime.world["sim_time_ms"] == source_time
            assert runtime.world["run_status"] == "paused"
            assert next(a["x"] for a in runtime.world["actors"] if a["object_id"] == "obj-car-02") == pytest.approx(source_x)
            reset = await runtime.mutate(OPERATOR, "reset", "control", {"action": "reset"}, replay_id)
            assert reset["run_id"] != replay_id and runtime.world["sim_time_ms"] == 0
            assert runtime.world.get("replay_state") is None
        run(exercise())
    finally:
        runtime.store.close()


def test_queued_synthetic_response_is_cancelled_after_relationship_revocation(tmp_path):
    runtime = Runtime(tmp_path / "recipient.sqlite3")
    try:
        operator = Auth(runtime.store).login("demo-operator", "parking-demo-only", "test")[1]

        async def business_tool(name, args, key, run_id):
            task = runtime.read_task(operator, run_id)
            return await runtime.business_tool(operator, name, args, key, task)

        async def exercise():
            created = await create(runtime, "s1a-foundation-v1")
            run_id = created["run_id"]
            for index in range(60):
                await runtime.mutate(operator, "warm-" + str(index), "control",
                                     {"action": "step"}, run_id)
            base = {"facility_id": FACILITY, "run_id": run_id,
                    "based_on_state_version": runtime.world["state_version"], "policy_version": 2}
            incident = (await business_tool("create_or_update_incident", base | {
                "primary_object_id": "obj-car-02", "status": "active",
                "impacts": [{"type": "aisle_obstruction", "zone_id": "aisle-west"}],
                "evidence_ids": runtime.business.analysis().observation_ids,
                "reason_summary": "서측 통로 차단 정지 관측",
            }, "incident", run_id))["result"]
            task = runtime.read_task(operator, run_id)
            search = await runtime.read_tool(operator, "search_operating_knowledge", {
                "facility_id": FACILITY, "run_id": run_id,
                "query": "통로 차단 이동 요청과 미응답", "topic": "parking_order"}, task)
            recipient = await runtime.recipient_tool(operator, "obj-car-02", task)
            accepted = await runtime.business_tool(operator, "notify_vehicle_user", base | {
                "incident_id": incident["incident_id"],
                "expected_resource_version": incident["resource_version"],
                "recipient_ref": recipient["recipient_ref"], "contact_sequence": 1,
                "template_args": {"zone_label": "서측 통로"},
                "knowledge_evidence": {"retrieval_id": search["retrieval_id"],
                                       "reference_ids": [r["reference_id"] for r in search["references"]]},
            }, "notify", task)
            assert accepted["status"] == "accepted"
            assert await runtime.business.deliver_one()
            policy = SyntheticUserInput(expected_state_version=runtime.world["state_version"],
                                        mode="will_move", movement_delay_ms=5000)
            runtime.synthetic_users.configure(operator, run_id, policy, "consumer")
            runtime.synthetic_users.process()
            queued = next(a for a in runtime.world["action_queue"]
                          if a["action_key"].startswith("synthetic-action:"))
            assert queued["status"] == "queued"
            before = next(a["y"] for a in runtime.world["actors"] if a["object_id"] == "obj-car-02")
            runtime.store.db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'")
            await runtime.mutate(operator, "after-revoke", "control", {"action": "step"}, run_id)
            current = next(a for a in runtime.world["action_queue"]
                           if a["action_key"] == queued["action_key"])
            assert current["status"] == "cancelled"
            assert next(a["y"] for a in runtime.world["actors"] if a["object_id"] == "obj-car-02") == before
            assert runtime.store.db.execute("SELECT status FROM synthetic_inbox_events").fetchone()[0] == "held"
        run(exercise())
    finally:
        runtime.store.close()
