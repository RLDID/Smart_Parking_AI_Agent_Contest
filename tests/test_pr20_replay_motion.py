"""Clock-faithful SIM-0 replay and one movement per actor."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from backend.runtime import Runtime
from simulator.environment import (apply_device_command, cancel_vehicle_action, queue_portal_attempt,
                                   queue_vehicle_response, set_synthetic_fault,
                                   _queue_action)
from simulator.replay import apply_replay_inputs, prepare_replay
from simulator.world import advance, initial_world, public_state


def _outcome(world):
    alarm = world["device_state"]["alarms"][0]
    return {
        "actors": world["actors"], "contacts": world["physical_contacts"],
        "alarm": (alarm["desired_active"], alarm["visual"], alarm["audio"],
                  len(alarm["claims"])),
        "safety": (world.get("safety_state", {}).get("analysis_status"),
                   len(world.get("safety_state", {}).get("claims", {}))),
    }


@pytest.mark.parametrize("wall_step_ms,expected_contact", [(100, False), (1500, True)])
def test_replay_keeps_original_safety_freshness_and_contact(
        tmp_path, monkeypatch, wall_step_ms, expected_contact):
    clock = {"at": datetime(2026, 10, 2, tzinfo=timezone.utc)}
    monkeypatch.setattr("simulator.world.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    monkeypatch.setattr("simulator.environment.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    runtime = Runtime(tmp_path / "clock.sqlite3")
    try:
        source = initial_world(1, "s2-crossing-v1")
        assert source["recorded_epoch_utc"] == source["observation"]["observed_at"]
        runtime.world = source
        origin = clock["at"]
        for tick in range(1, 61):
            clock["at"] = origin + timedelta(milliseconds=tick * wall_step_ms)
            runtime.advance_candidate(source)
            runtime.safety.step(source)
        expected = deepcopy(_outcome(source))
        assert bool(source["s2_contact_at_ms"] is not None) is expected_contact
        assert source["recorded_clocks"]
        replay = initial_world(1, "s2-crossing-v1")
        prepare_replay(source, replay)
        # Wall speed and absolute date during replay must have no effect.
        for tick in range(1, 61):
            clock["at"] = origin + timedelta(days=100, milliseconds=tick)
            runtime.advance_candidate(replay)
            runtime.safety.step(replay)
        assert _outcome(replay) == expected
        assert replay["replay_state"]["clock_cursor"] == len(replay["recorded_clocks"])
    finally:
        runtime.store.close()


def test_legacy_s2_replay_requires_original_safety_clock():
    source = initial_world(1, "s2-crossing-v1")
    source.pop("recorded_clocks")
    source.pop("replay_clock_version")
    with pytest.raises(ValueError, match="Legacy checkpoint"):
        prepare_replay(source, initial_world(1, "s2-crossing-v1"))


def test_partial_v1_clock_trace_is_not_silently_upgraded():
    source = initial_world(1, "s3-closing-v1")
    source["replay_clock_version"] = 1
    with pytest.raises(ValueError, match="Legacy checkpoint"):
        prepare_replay(source, initial_world(1, "s3-closing-v1"))
    assert "recorded_clocks" not in str(public_state(source))


def test_full_clock_trace_does_not_stop_live_safety(tmp_path, monkeypatch):
    monkeypatch.setattr("simulator.replay.MAX_CLOCKS", 3)
    runtime = Runtime(tmp_path / "bound.sqlite3")
    try:
        world = initial_world(1, "s2-crossing-v1")
        for _ in range(20):
            runtime.advance_candidate(world)
            runtime.safety.step(world)
        assert world["sim_time_ms"] == 2000
        assert world["replay_clock_overflow"] is True
        assert len(world["recorded_clocks"]) == 3
        with pytest.raises(ValueError, match="exceeded its bound"):
            prepare_replay(world, initial_world(1, "s2-crossing-v1"))
    finally:
        runtime.store.close()


def test_replay_preserves_fault_feedback(tmp_path):
    runtime = Runtime(tmp_path / "fault.sqlite3")
    try:
        source = initial_world(1, "s2-crossing-v1")
        for channel in ("visual", "audio"):
            set_synthetic_fault(source, channel, True)
        for _ in range(60):
            runtime.advance_candidate(source)
            runtime.safety.step(source)
        replay = initial_world(1, "s2-crossing-v1")
        prepare_replay(source, replay)
        for _ in range(60):
            runtime.advance_candidate(replay)
            runtime.safety.step(replay)
        assert _outcome(replay) == _outcome(source)
        assert source["s2_contact_at_ms"] is not None
    finally:
        runtime.store.close()


def test_restart_boundary_clears_replay_evidence(tmp_path):
    database = tmp_path / "restart.sqlite3"
    before = Runtime(database)
    world = initial_world(1, "s2-crossing-v1")
    for _ in range(3):
        before.advance_candidate(world)
        before.safety.step(world)
    before.store.commit(world, before.event(world))
    before.store.close()
    after = Runtime(database)
    try:
        assert after.world["recovery_required"] is True
        assert after.world["recorded_inputs"][-1]["kind"] == "restart_boundary"
        from simulator.replay import record_input
        record_input(after.world, "recovery_resume", {})
        after.world["recovery_required"] = False
        for _ in range(5):
            after.advance_candidate(after.world)
            after.safety.step(after.world)
        replay = initial_world(1, "s2-crossing-v1")
        prepare_replay(after.world, replay)
        for _ in range(8):
            after.advance_candidate(replay)
            after.safety.step(replay)
        assert _outcome(replay) == _outcome(after.world)
    finally:
        after.store.close()


@pytest.mark.parametrize("object_id", ["obj-car-s3-u", "obj-car-s3-w"])
def test_portal_actor_cannot_accelerate_with_distinct_keys(object_id):
    world = initial_world(1, "s3-closing-v1")
    first = queue_portal_attempt(world, object_id, action_key="first")
    assert queue_portal_attempt(world, object_id, action_key="first") == first
    with pytest.raises(ValueError, match="active movement"):
        queue_portal_attempt(world, object_id, action_key="second")
    before = next(a["y"] for a in world["actors"] if a["object_id"] == object_id)
    advance(world)
    after = next(a["y"] for a in world["actors"] if a["object_id"] == object_id)
    assert abs(after - before) <= 0.2 + 1e-9
    assert len(world["action_queue"]) == 1
    cancel_vehicle_action(world, "first")
    assert queue_portal_attempt(world, object_id, action_key="second")["status"] == "queued"


def test_movement_conflict_spans_response_kind_and_allows_retry_after_cancel():
    world = initial_world(1, "s1a-foundation-v1")
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="move")
    with pytest.raises(ValueError, match="active movement"):
        queue_vehicle_response(world, "obj-car-02", "will_move", action_key="move-again")
    cancel_vehicle_action(world, "move")
    assert queue_vehicle_response(world, "obj-car-02", "will_move", action_key="move-again")["status"] == "queued"


def test_pending_action_conflicts_across_kinds_and_finished_action_can_retry():
    world = initial_world(1, "s3-closing-v1")
    queue_portal_attempt(world, "obj-car-s3-u", action_key="portal")
    with pytest.raises(ValueError, match="active movement"):
        _queue_action(world, object_id="obj-car-s3-u", kind="departure", action_key="other-kind")
    for _ in range(61):
        advance(world)
    assert world["action_queue"][0]["status"] == "completed"
    assert queue_portal_attempt(world, "obj-car-s3-u", action_key="retry")["status"] == "queued"


@pytest.mark.parametrize("fixture,object_id,action_key", [
    ("s3-closing-v1", "obj-car-s3-u", "enter"),
    ("s3-closing-v1", "obj-car-s3-w", "exit"),
    ("s1a-foundation-v1", "obj-car-02", "north-exit"),
])
def test_replay_preserves_public_portal_event_wall_time(monkeypatch, fixture, object_id, action_key):
    clock = {"at": datetime(2026, 10, 2, tzinfo=timezone.utc)}
    monkeypatch.setattr("simulator.world.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    monkeypatch.setattr("simulator.environment.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    source = initial_world(1, fixture)
    assert source["recorded_epoch_utc"] == source["observation"]["observed_at"]
    if fixture.startswith("s3-"):
        queue_portal_attempt(source, object_id, action_key=action_key)
    else:
        queue_vehicle_response(source, object_id, "will_move", action_key=action_key)
    for _ in range(170):
        clock["at"] += timedelta(milliseconds=100)
        advance(source)
        if any(frame["object_events"] for frame in source["observation_history"]):
            break
    source_events = [event for frame in source["observation_history"]
                     for event in frame["object_events"]]
    assert source_events
    replay = initial_world(1, fixture)
    prepare_replay(source, replay)
    for _ in range(source["sim_time_ms"] // 100):
        clock["at"] += timedelta(days=1)
        apply_replay_inputs(replay)
        advance(replay)
    replay_events = [event for frame in replay["observation_history"]
                     for event in frame["object_events"]]
    assert replay_events == source_events


def test_replay_preserves_automatic_device_feedback_wall_time(monkeypatch):
    clock = {"at": datetime.now(timezone.utc) + timedelta(days=1)}
    monkeypatch.setattr("simulator.world.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    monkeypatch.setattr("simulator.environment.utc_now", lambda: clock["at"].isoformat().replace("+00:00", "Z"))
    source = initial_world(1, "s3-closing-v1")
    assert source["recorded_epoch_utc"] == source["observation"]["observed_at"]
    command_time = clock["at"].isoformat().replace("+00:00", "Z")
    apply_device_command(source, {"action": "broadcast", "operation_id": "notice",
        "zone_id": "announcement-a", "message_id": "closing_notice"}, now_utc=command_time)
    clock["at"] += timedelta(milliseconds=100)
    advance(source)
    replay = initial_world(1, "s3-closing-v1")
    prepare_replay(source, replay)
    apply_replay_inputs(replay)
    advance(replay)
    assert replay["device_state"] == source["device_state"]
