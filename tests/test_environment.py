"""SIM-0 fixtures retain the foundation run and separate internal future data."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from contracts.models import CreateRun
from contracts.s1c_bay_geometry import BayGeometrySettings
from contracts.exit_geometry import ExitGeometrySettings
from simulator.environment import (FIXTURE_REFS, apply_device_command,
                                   cancel_vehicle_action, public_devices,
                                   queue_portal_attempt, queue_vehicle_departure,
                                   queue_vehicle_response, set_synthetic_fault)
from simulator.exit_geometry import analyze_exit_candidate
from simulator.s1c_bay_geometry import analyze_bay_footprint
from simulator.world import (MAP, advance, initial_world, public_state,
                             valid_behavior_digest)


def _step(world, ticks):
    for _ in range(ticks):
        advance(world)


def _bay(world):
    snap = world["observation"]
    return analyze_bay_footprint(
        MAP, snap, object_id="obj-car-02", bay_id="B01",
        settings=BayGeometrySettings(tolerance_m=0.1, max_uncertainty_m=0,
                                     freshness_ms=1000),
        expected_run_id=world["run_id"], expected_state_version=snap["state_version"],
        current_sim_time_ms=world["sim_time_ms"], run_status="paused",
        recovery_required=False, observation_ready=True,
        now=datetime.now(timezone.utc))


def _exit(world):
    return analyze_exit_candidate(
        MAP, world["observation"],
        settings=ExitGeometrySettings(side_clearance_m=0, max_observation_uncertainty_m=0,
                                      freshness_ms=1000, max_discretization_error_m=0.05),
        current_sim_time_ms=world["sim_time_ms"], run_status="paused",
        now=datetime.now(timezone.utc))


def test_fixture_contract_digest_json_and_public_projection():
    for ref in FIXTURE_REFS:
        world = initial_world(7, ref)
        assert valid_behavior_digest(world)
        assert world["device_state"]["kind"] == "synthetic_device_state"
        assert json.loads(json.dumps(world))["fixture_ref"] == ref
        public = json.dumps(public_state(world))
        assert all(k not in public for k in (
            "fixture_ref", "seed", "action_queue", "behavior_digest",
            "physical_contacts", "device_faults", "future", "expected"))
        assert ref not in public
        devices = json.dumps(public_devices(world))
        assert "fixture_ref" not in devices and "payload_digest" not in devices
    with pytest.raises(ValueError):
        initial_world(7, "unknown")
    with pytest.raises(Exception):
        CreateRun(facility_id="fac-demo-01", fixture_ref="s2-crossing-v1",
                  seed=1, config_ref="foundation-v1")
    assert CreateRun(facility_id="fac-demo-01", fixture_ref="s2-crossing-v1",
                     seed=1, config_ref="sim0-v1")
    legacy = initial_world(7)
    legacy["configuration_digest"] = "0" * 64
    assert not valid_behavior_digest(legacy)


def test_legacy_response_queue_idempotency_and_no_promise_teleport():
    world = initial_world(1)
    start = deepcopy(world["actors"][1])
    result = queue_vehicle_response(world, "obj-car-02", "acknowledged", action_key="ack")
    assert result["status"] == "no_movement"
    _step(world, 2)
    assert world["actors"][1]["y"] == start["y"]
    queued = queue_vehicle_response(world, "obj-car-02", "will_move",
                                    action_key="move-1", delay_ms=200)
    assert queued["status"] == "queued"
    _step(world, 3)
    repeated = queue_vehicle_response(world, "obj-car-02", "will_move",
                                      action_key="move-1", delay_ms=200)
    assert repeated["action_key"] == queued["action_key"]
    assert len(world["action_queue"]) == 2
    assert world["actors"][1]["y"] > start["y"]
    with pytest.raises(ValueError):
        queue_vehicle_response(world, "obj-car-02", "will_move",
                               action_key="move-1", delay_ms=300)
    y = world["actors"][1]["y"]
    assert cancel_vehicle_action(world, "move-1")["status"] == "cancelled"
    _step(world, 2)
    assert world["actors"][1]["y"] == y


def test_s1b_support_geometry_and_response_route_then_a_departure():
    world = initial_world(2, "s1b-blocked-v1")
    assert _exit(world).candidate_passage == "blocked"
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="b-move")
    _step(world, 230)
    assert world["action_queue"][0]["status"] == "completed"
    assert _exit(world).candidate_passage == "clear"
    queue_vehicle_departure(world, "obj-car-01", action_key="a-exit")
    _step(world, 380)
    assert world["action_queue"][1]["status"] == "completed"
    assert all(a["object_id"] != "obj-car-01" for a in world["actors"])


def test_s1c_intrusion_is_supported_then_repark_reaches_bay():
    world = initial_world(3, "s1c-overlap-v1")
    assert _bay(world).geometry_relation == "overlap"
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="repark")
    _step(world, 300)
    assert world["action_queue"][0]["status"] == "completed"
    assert _bay(world).geometry_relation == "within"
    assert world["actors"][0]["x"] == pytest.approx(9.5)
    assert world["actors"][0]["y"] == pytest.approx(26.5)


def test_device_command_same_sim_time_replaces_history_frame():
    world = initial_world(4, "s3-closing-v1")
    first = world["observation"]["observation_id"]
    result = apply_device_command(world, {
        "action": "broadcast", "operation_id": "notice-1",
        "zone_id": "announcement-a", "message_id": "closing_notice",
    }, now_utc=world["device_state"]["now_utc"])
    assert result.outcome == "accepted"
    assert len(world["observation_history"]) == 1
    assert world["observation"]["observation_id"] != first
    assert world["observation_history"][-1] == world["observation"]


def test_completed_action_retry_after_clock_advance_keeps_original_schedule():
    world = initial_world(5, "s1b-blocked-v1")
    first = queue_vehicle_response(world, "obj-car-02", "will_move",
                                   action_key="delay-1", delay_ms=300)
    scheduled = world["action_queue"][0]["apply_at_ms"]
    _step(world, 240)
    again = queue_vehicle_response(world, "obj-car-02", "will_move",
                                   action_key="delay-1", delay_ms=300)
    assert first["status"] == "queued" and again["status"] == "completed"
    assert len(world["action_queue"]) == 1
    assert world["action_queue"][0]["apply_at_ms"] == scheduled
    assert world["action_queue"][0]["delay_ms"] == 300


def test_s2_alarm_reaction_requires_confirmed_channel_feedback():
    outcomes = []
    for failed in (False, True):
        world = initial_world(6, "s2-crossing-v1")
        if failed:
            set_synthetic_fault(world, "visual", True)
            set_synthetic_fault(world, "audio", True)
        result = apply_device_command(world, {
            "action": "claim_alarm", "operation_id": "claim-1",
            "zone_id": "announcement-a", "incident_id": "risk-1",
            "evidence_version": 1, "expected_version": 0,
        }, now_utc=world["device_state"]["now_utc"])
        assert result.outcome == "accepted"
        _step(world, 50)
        outcomes.append(world)
    good, failed = outcomes
    assert good["s2_alarm_seen_ms"] is not None
    assert good["s2_contact_at_ms"] is None
    assert good["actors"][0]["x"] < 17
    assert failed["s2_alarm_seen_ms"] is None
    assert failed["s2_contact_at_ms"] is not None
    assert failed["device_state"]["alarms"][0]["visual"] == "failed"
    assert failed["device_state"]["alarms"][0]["audio"] == "failed"


def test_s3_late_entry_stops_gate_closure_before_next_observation():
    world = initial_world(8, "s3-closing-v1")
    queue_portal_attempt(world, "obj-car-s3-u", action_key="u-entry")
    _step(world, 16)
    assert world["actors"][0]["y"] == pytest.approx(0.2)

    def command(action, operation_id, **fields):
        if "gate_id" in fields:
            fields["expected_version"] = world["device_state"]["gates"][0]["resource_version"]
        return apply_device_command(world, {"action": action,
                                            "operation_id": operation_id, **fields},
                                    now_utc=world["device_state"]["now_utc"])

    assert command("broadcast", "notice", zone_id="announcement-a",
                   message_id="closing_notice").outcome == "accepted"
    advance(world)  # Synthetic playback confirms separately from receipt.
    assert world["device_state"]["broadcasts"][0]["simulated_playback"] == "played"
    assert world["device_state"]["broadcasts"][0]["browser_playback"] == "not_requested"
    assert command("set_entry_policy", "deny", gate_id="gate-in-01",
                   target="deny", outbound_clear=True,
                   broadcast_operation_id="notice").outcome == "accepted"
    assert command("tick", "clear-sensor", gate_id="gate-in-01").outcome == "accepted"
    assert command("command_gate", "close", gate_id="gate-in-01",
                   target="closed").outcome == "accepted"
    previous_frame_time = world["observation"]["sim_time_ms"]
    advance(world)
    gate = world["device_state"]["gates"][0]
    assert world["sim_time_ms"] - previous_frame_time == 100
    assert gate["physical_state"] == "stopped"
    assert gate["last_feedback"] == "safety_stop"
    assert gate["obstacle_detected"] is True


def test_s3_exit_stays_independent_and_checkpoint_preserves_action():
    world = initial_world(9, "s3-closing-v1")
    queue_portal_attempt(world, "obj-car-s3-w", action_key="w-exit")
    _step(world, 12)
    restored = json.loads(json.dumps(world))
    assert valid_behavior_digest(restored)
    _step(restored, 50)
    assert restored["s3_exited"] is True
    assert restored["action_queue"][0]["status"] == "completed"
    assert all(a["object_id"] != "obj-car-s3-w" for a in restored["actors"])


S1_ACCEPTANCE_FIXTURES = ("s1a-foundation-v1", "s1b-blocked-v1", "s1c-overlap-v1")


def _acceptance_car_pose(world):
    car = next(a for a in world["actors"] if a["object_id"] == "obj-car-02")
    return car["x"], car["y"], car["heading_deg"]


@pytest.mark.parametrize("fixture", S1_ACCEPTANCE_FIXTURES)
@pytest.mark.parametrize("response", (None, "acknowledged", "cannot_move", "question"))
def test_s1_receipt_non_movement_response_or_silence_keeps_vehicle_still(fixture, response):
    world = initial_world(61, fixture)
    original = _acceptance_car_pose(world)
    if response is not None:
        result = queue_vehicle_response(world, "obj-car-02", response, action_key="non-movement")
        assert result["status"] == "no_movement"
    _step(world, 25)
    assert _acceptance_car_pose(world) == original
    assert not any(a["status"] == "completed" for a in world["action_queue"])
    assert not world["pending_events"]


@pytest.mark.parametrize("fixture", S1_ACCEPTANCE_FIXTURES)
def test_s1_delayed_response_cannot_move_before_its_fixed_deadline(fixture):
    world = initial_world(62, fixture)
    original = _acceptance_car_pose(world)
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="delayed", delay_ms=1000)
    _step(world, 9)
    assert world["sim_time_ms"] == 900
    assert _acceptance_car_pose(world) == original
    assert world["action_queue"][0]["status"] == "queued"
    _step(world, 2)
    assert _acceptance_car_pose(world) != original
    assert world["action_queue"][0]["status"] == "moving"


@pytest.mark.parametrize("fixture,axis,sign", (("s1b-blocked-v1", "x", 1), ("s1c-overlap-v1", "y", -1)))
def test_s1_mid_route_obstacle_stops_before_contact_and_needs_new_input(fixture, axis, sign):
    world = initial_world(63, fixture)
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="obstructed")
    _step(world, 15)
    car = next(a for a in world["actors"] if a["object_id"] == "obj-car-02")
    obstacle = {"actor_id": "internal-late-obstacle", "object_id": "obj-late-obstacle",
                "object_type": "pedestrian", "x": car["x"], "y": car["y"],
                "length_m": 0.6, "width_m": 0.6, "heading_deg": 0}
    obstacle[axis] += sign * 2.7
    world["actors"].append(obstacle)
    _step(world, 12)
    assert world["action_queue"][0]["status"] == "blocked"
    # These paths still have their initial axis-aligned heading. Compare the
    # independent body edges, rather than the product collision predicate.
    assert sign * (obstacle[axis] - car[axis]) > 2.6
    stopped = _acceptance_car_pose(world)
    world["actors"].remove(obstacle)
    _step(world, 20)
    assert _acceptance_car_pose(world) == stopped
    assert world["action_queue"][0]["status"] == "blocked"


@pytest.mark.parametrize("failed_channel,working_channel", (("audio", "visual"), ("visual", "audio")))
def test_s2_one_confirmed_channel_can_brake_without_claiming_other_channel_success(failed_channel, working_channel):
    world = initial_world(64, "s2-crossing-v1")
    set_synthetic_fault(world, failed_channel, True)
    apply_device_command(world, {"action": "claim_alarm", "operation_id": "partial-alarm",
        "zone_id": "announcement-a", "incident_id": "risk-partial",
        "evidence_version": 1, "expected_version": 0}, now_utc=world["device_state"]["now_utc"])
    _step(world, 50)
    alarm = world["device_state"]["alarms"][0]
    assert alarm[failed_channel] == "failed"
    assert alarm[working_channel] == "on"
    assert world["s2_alarm_seen_ms"] is not None
    assert world["s2_contact_at_ms"] is None
    assert world["actors"][0]["x"] < 17


def test_s3_failed_announcement_cannot_restrict_entry_or_block_independent_exit():
    world = initial_world(65, "s3-closing-v1")
    set_synthetic_fault(world, "simulated_playback", True)
    sent = apply_device_command(world, {"action": "broadcast", "operation_id": "failed-notice",
        "zone_id": "announcement-a", "message_id": "closing_notice"}, now_utc=world["device_state"]["now_utc"])
    assert sent.outcome == "accepted"
    advance(world)
    broadcast = world["device_state"]["broadcasts"][0]
    assert broadcast["receipt"] == "accepted"
    assert broadcast["simulated_playback"] == "failed"
    assert broadcast["browser_playback"] == "not_requested"
    denied = apply_device_command(world, {"action": "set_entry_policy", "operation_id": "deny-after-failure",
        "gate_id": "gate-in-01", "target": "deny", "outbound_clear": True,
        "broadcast_operation_id": "failed-notice", "expected_version": world["device_state"]["gates"][0]["resource_version"]},
        now_utc=world["device_state"]["now_utc"])
    assert denied.outcome == "held" and denied.reason == "closure_announcement_unconfirmed"
    assert world["device_state"]["gates"][0]["entry_policy"] == "allow"
    queue_portal_attempt(world, "obj-car-s3-w", action_key="preserved-exit")
    _step(world, 65)
    assert world["s3_exited"] is True
    assert world["device_state"]["gates"][1]["physical_state"] == "open"


@pytest.mark.parametrize("fixture,axis,sign,supported_retry", (
    ("s1b-blocked-v1", "x", 1, True),
    ("s1c-overlap-v1", "y", -1, False),
))
def test_s1_obstacle_removal_requires_new_input_and_unsupported_retry_stays_still(fixture, axis, sign, supported_retry):
    world = initial_world(68, fixture)
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="first-obstructed")
    _step(world, 15)
    car = next(a for a in world["actors"] if a["object_id"] == "obj-car-02")
    obstacle = {"actor_id": "internal-retry-obstacle", "object_id": "obj-retry-obstacle",
        "object_type": "pedestrian", "x": car["x"], "y": car["y"],
        "length_m": 0.6, "width_m": 0.6, "heading_deg": 0}
    obstacle[axis] += sign * 2.7
    world["actors"].append(obstacle)
    _step(world, 12)
    assert world["action_queue"][0]["status"] == "blocked"
    stopped = _acceptance_car_pose(world)
    world["actors"].remove(obstacle)
    _step(world, 20)
    assert _acceptance_car_pose(world) == stopped
    queue_vehicle_response(world, "obj-car-02", "will_move", action_key="explicit-retry")
    _step(world, 230)
    assert world["action_queue"][0]["status"] == "blocked"
    assert len(world["action_queue"]) == 2
    if supported_retry:
        assert world["action_queue"][1]["status"] == "completed"
        assert _acceptance_car_pose(world) == (27.0, 21.7, 0.0)
    else:
        # S1-c only supports its fixed repark route from the original pose.
        # A new input at a mid-route pose cannot invent a new movement route.
        assert world["action_queue"][1]["status"] == "blocked"
        assert _acceptance_car_pose(world) == stopped
