"""Synthetic device results and safety decisions; no real equipment is used."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from contracts.devices import DeviceCommand, DeviceState
from simulator.devices import initial_devices, operate_devices
from simulator.world import MAP


NOW = "2026-10-01T00:00:00Z"


def start():
    return initial_devices(MAP, NOW, broadcast_cooldown_s=30,
                           gate_transition_ms=500,
                           allowed_messages=["closing_notice", "safety_notice"])


def run(state, action, op, *, at=0, now=NOW, **fields):
    result = operate_devices(state, DeviceCommand(action=action, operation_id=op, **fields),
                             now_utc=now, sim_time_ms=at)
    return result.state, result


def test_alarm_claims_hold_independently_and_feedback_is_separate():
    s = start()
    s, a = run(s, "claim_alarm", "c1", zone_id="announcement-a",
               incident_id="I1", evidence_version=4, expected_version=0)
    assert a.outcome == "accepted"
    zone = s.alarms[0]
    assert zone.desired_active and zone.visual == "pending" and zone.audio == "pending"
    s, a = run(s, "claim_alarm", "c2", zone_id=zone.zone_id,
               incident_id="I2", evidence_version=5, expected_version=zone.resource_version)
    assert a.outcome == "accepted" and len(s.alarms[0].claims) == 2
    s, a = run(s, "alarm_feedback", "f1", zone_id=zone.zone_id, expected_version=2,
               channel="audio", feedback="failed")
    assert a.outcome == "accepted" and s.alarms[0].audio == "failed"
    s, a = run(s, "clear_alarm", "stale", zone_id=zone.zone_id, expected_version=3,
               incident_id="I1", evidence_version=4, current_observation=True)
    assert a.outcome == "held" and len(s.alarms[0].claims) == 2
    s, a = run(s, "clear_alarm", "missing", zone_id=zone.zone_id, expected_version=3,
               incident_id="I1", evidence_version=6, current_observation=False)
    assert a.outcome == "held"
    s, a = run(s, "clear_alarm", "one", zone_id=zone.zone_id, expected_version=3,
               incident_id="I1", evidence_version=6, current_observation=True)
    assert a.outcome == "accepted" and a.reason == "remaining_claims_hold_alarm"
    assert s.alarms[0].desired_active and s.alarms[0].audio == "failed"
    s, a = run(s, "alarm_feedback", "wrong", zone_id=zone.zone_id, expected_version=4,
               channel="visual", feedback="off")
    assert a.outcome == "held" and s.alarms[0].visual == "pending"
    s, a = run(s, "clear_alarm", "two", zone_id=zone.zone_id, expected_version=4,
               incident_id="I2", evidence_version=7, current_observation=True)
    assert a.outcome == "accepted" and not s.alarms[0].desired_active
    assert s.alarms[0].audio == "pending" and s.alarms[0].visual == "pending"


def test_alarm_stale_version_unknown_claim_and_duplicate_key_collision():
    s = start()
    c = DeviceCommand(action="claim_alarm", operation_id="claim", zone_id="announcement-a",
                      incident_id="I1", evidence_version=1, expected_version=0)
    first = operate_devices(s, c, now_utc=NOW, sim_time_ms=0)
    duplicate = operate_devices(first.state, c, now_utc=NOW, sim_time_ms=0)
    assert duplicate.outcome == first.outcome and duplicate.state == first.state
    collision = operate_devices(first.state, c.model_copy(update={"incident_id": "I2"}),
                                now_utc=NOW, sim_time_ms=0)
    assert collision.outcome == "rejected" and collision.state == first.state
    s, a = run(first.state, "clear_alarm", "clear-other", zone_id="announcement-a",
               incident_id="other", evidence_version=2, current_observation=True,
               expected_version=1)
    assert a.outcome == "held"
    s, a = run(s, "claim_alarm", "stale-version", zone_id="announcement-a",
               incident_id="I2", evidence_version=2, expected_version=0)
    assert a.outcome == "held" and len(s.alarms[0].claims) == 1


def _announce_and_deny(state):
    s, a = run(state, "broadcast", "b1", zone_id="announcement-a",
               message_id="closing_notice")
    assert a.outcome == "accepted" and s.broadcasts[0].simulated_playback == "pending"
    s, a = run(s, "broadcast_feedback", "b2", broadcast_operation_id="b1",
               channel="simulated_playback", feedback="played")
    assert a.outcome == "accepted"
    s, a = run(s, "set_entry_policy", "p1", gate_id="gate-in-01", expected_version=0,
               target="deny", broadcast_operation_id="b1", outbound_clear=True)
    assert a.outcome == "accepted"
    return s


def test_gate_closure_order_and_independent_exit():
    s = start()
    s, r = run(s, "set_entry_policy", "early", gate_id="gate-in-01",
               expected_version=0, target="deny", outbound_clear=True)
    assert r.outcome == "held" and s.gates[0].entry_policy == "allow"
    s = _announce_and_deny(s)
    assert s.gates[0].physical_state == "open" and s.gates[0].entry_policy == "deny"
    s, r = run(s, "command_gate", "bad-out", gate_id="gate-out-01",
               expected_version=0, target="closed", obstacle_detected=False)
    assert r.outcome == "rejected" and s.gates[1].physical_state == "open"
    s, r = run(s, "command_gate", "sensor-unknown", gate_id="gate-in-01",
               expected_version=1, target="closed", obstacle_detected=False)
    assert r.outcome == "held" and s.gates[0].physical_state == "open"
    s, r = run(s, "tick", "t1", gate_id="gate-in-01", expected_version=1,
               obstacle_detected=False)
    assert r.outcome == "accepted"
    s, r = run(s, "command_gate", "g1", gate_id="gate-in-01",
               expected_version=2, target="closed", obstacle_detected=False)
    assert r.outcome == "accepted" and s.gates[0].physical_state == "closing"
    s, r = run(s, "tick", "t2", gate_id="gate-in-01", expected_version=3,
               obstacle_detected=False, at=500)
    assert r.outcome == "unknown" and s.gates[0].physical_state == "closing"
    s, r = run(s, "gate_feedback", "feedback", gate_id="gate-in-01",
               expected_version=4, feedback="played", obstacle_detected=False, at=500)
    assert r.outcome == "accepted" and s.gates[0].physical_state == "closed"
    assert s.gates[1].physical_state == "open" and s.gates[1].entry_policy == "allow"


@pytest.mark.parametrize("sample", [True, None])
def test_obstacle_or_missing_sensor_stops_closing(sample):
    s = _announce_and_deny(start())
    s, _ = run(s, "tick", "sensor", gate_id="gate-in-01", expected_version=1,
               obstacle_detected=False)
    s, _ = run(s, "command_gate", "close", gate_id="gate-in-01", expected_version=2,
               target="closed", obstacle_detected=False)
    s, r = run(s, "tick", "obstacle", gate_id="gate-in-01", expected_version=3,
               obstacle_detected=sample, at=200)
    assert r.outcome == "held" and s.gates[0].physical_state == "stopped"
    assert s.gates[0].last_feedback == "safety_stop"
    s, r = run(s, "gate_feedback", "late-success", gate_id="gate-in-01",
               expected_version=4, feedback="played", obstacle_detected=False, at=200)
    assert r.outcome == "held" and s.gates[0].physical_state == "stopped"
    s, r = run(s, "command_gate", "again", gate_id="gate-in-01", expected_version=4,
               target="closed", obstacle_detected=False, at=200)
    assert r.outcome == "held" and s.gates[0].physical_state == "stopped"
    s, r = run(s, "cancel_gate", "cancel", gate_id="gate-in-01",
               expected_version=4, at=200)
    assert r.outcome == "accepted" and s.gates[0].active_operation_id is None


def test_unknown_gate_result_requires_reconciliation_and_cancel_does_not_undo_applied():
    s = _announce_and_deny(start())
    s, _ = run(s, "tick", "sensor", gate_id="gate-in-01", expected_version=1,
               obstacle_detected=False)
    s, _ = run(s, "command_gate", "close", gate_id="gate-in-01", expected_version=2,
               target="closed", obstacle_detected=False)
    s, r = run(s, "gate_feedback", "unknown", gate_id="gate-in-01",
               expected_version=3, feedback="unknown")
    assert r.outcome == "unknown" and s.gates[0].physical_state == "unknown"
    s, r = run(s, "command_gate", "retry", gate_id="gate-in-01", expected_version=4,
               target="closed", obstacle_detected=False)
    assert r.outcome == "held"
    s, r = run(s, "cancel_gate", "unsafe-cancel", gate_id="gate-in-01",
               expected_version=4)
    assert r.outcome == "held" and s.gates[0].physical_state == "unknown"
    assert s.gates[0].active_operation_id == "close"
    s, r = run(s, "gate_feedback", "resolved", gate_id="gate-in-01",
               expected_version=4, feedback="played", obstacle_detected=False)
    assert r.outcome == "accepted" and s.gates[0].physical_state == "closed"
    s, r = run(s, "cancel_gate", "too-late", gate_id="gate-in-01", expected_version=5)
    assert r.outcome == "held" and s.gates[0].physical_state == "closed"


def test_broadcast_frequency_and_receipt_playback_distinction():
    s = start()
    s, r = run(s, "broadcast", "b1", zone_id="announcement-a",
               message_id="safety_notice")
    assert r.outcome == "accepted"
    assert s.broadcasts[0].receipt == "accepted"
    assert s.broadcasts[0].simulated_playback == "pending"
    assert s.broadcasts[0].browser_playback == "not_requested"
    s, r = run(s, "broadcast", "b2", zone_id="announcement-a",
               message_id="safety_notice", now="2026-10-01T00:00:20Z")
    assert r.outcome == "held" and len(s.broadcasts) == 1
    s, r = run(s, "broadcast_feedback", "f1", broadcast_operation_id="b1",
               channel="simulated_playback", feedback="failed", now="2026-10-01T00:00:20Z")
    assert r.outcome == "held" and s.broadcasts[0].simulated_playback == "failed"
    s, r = run(s, "broadcast_feedback", "f2", broadcast_operation_id="b1",
               channel="browser_playback", feedback="played", now="2026-10-01T00:00:20Z")
    assert r.outcome == "accepted" and s.broadcasts[0].simulated_playback == "failed"
    s, r = run(s, "broadcast", "b3", zone_id="announcement-a",
               message_id="safety_notice", now="2026-10-01T00:00:30Z")
    assert r.outcome == "accepted" and len(s.broadcasts) == 2
    s, r = run(s, "broadcast", "bad-zone", zone_id="missing", message_id="safety_notice",
               now="2026-10-01T00:00:30Z")
    assert r.outcome == "rejected"


def test_checkpoint_roundtrip_and_clocks():
    s = start()
    restored = DeviceState.model_validate_json(s.model_dump_json())
    assert restored == s and restored is not s
    before = deepcopy(s)
    with pytest.raises(ValueError, match="backward"):
        operate_devices(s, DeviceCommand(action="broadcast", operation_id="b1",
                                        zone_id="announcement-a", message_id="closing_notice"),
                        now_utc="2026-09-30T23:59:59Z", sim_time_ms=0)
    assert s == before
    with pytest.raises(ValidationError):
        initial_devices(MAP, NOW, broadcast_cooldown_s=-1,
                        gate_transition_ms=500, allowed_messages=["closing_notice"])
