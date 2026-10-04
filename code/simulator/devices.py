"""Deterministic synthetic alarm, gate and announcement controller.

The caller owns authorization, event sourcing and current-world observations.
This module only advances a bounded device checkpoint from explicit inputs.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json

from contracts.devices import (AlarmClaim, AlarmZoneState, BroadcastRecord,
                               DeviceCommand, DeviceResult, DeviceState,
                               GateState, OperationRecord)
from contracts.models import utc_timestamp
from simulator.world import digest


def _clock(value):
    utc_timestamp(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def initial_devices(map_data, now_utc, *, broadcast_cooldown_s,
                    gate_transition_ms, allowed_messages):
    """Build a checkpoint from an explicit map and caller-chosen policy values."""
    _clock(now_utc)
    gates = map_data.get("gates", [])
    zones = map_data.get("announcement_zones", [])
    zone_types = {z["zone_id"]: z["type"] for z in map_data.get("zones", [])}
    if len(gates) != 2 or {zone_types.get(g.get("zone_id")) for g in gates} != {"entrance", "exit"}:
        raise ValueError("Separate entry and exit gates are required")
    if len(zones) != len(set(zones)) or not zones or any(zone_types.get(z) != "announcement" for z in zones):
        raise ValueError("Announcement zones must exist on the map")
    return DeviceState(
        facility_id=map_data["facility_id"], map_version=map_data["map_version"],
        map_digest=digest(map_data), version=0, now_utc=now_utc, sim_time_ms=0,
        broadcast_cooldown_s=broadcast_cooldown_s,
        gate_transition_ms=gate_transition_ms, allowed_messages=allowed_messages,
        alarms=[AlarmZoneState(zone_id=z, resource_version=0, claims=[],
                               desired_active=False, visual="off", audio="off") for z in zones],
        gates=[GateState(gate_id=g["gate_id"], direction=("entry" if zone_types[g["zone_id"]] == "entrance" else "exit"),
                         resource_version=0, obstacle_detected=None) for g in gates],
        broadcasts=[], operations=[])


def _find(items, field, value):
    return next((item for item in items if getattr(item, field) == value), None)


def _digest_command(command):
    serialized = json.dumps(command.model_dump(mode="json", exclude={"operation_id"}),
                            sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(serialized.encode()).hexdigest()


def operate_devices(state: DeviceState, command: DeviceCommand, *, now_utc: str,
                    sim_time_ms: int) -> DeviceResult:
    """Apply one synthetic command with idempotency and monotonic clocks.

    ``unknown`` results stay unknown until a *new* feedback operation is supplied.
    They must never be blindly retried under a new operation ID by a caller.
    """
    if not isinstance(state, DeviceState) or not isinstance(command, DeviceCommand):
        raise TypeError("Validated DeviceState and DeviceCommand required")
    now = _clock(now_utc)
    if isinstance(sim_time_ms, bool) or not isinstance(sim_time_ms, int) or sim_time_ms < state.sim_time_ms:
        raise ValueError("Simulation clock cannot move backward")
    if sim_time_ms > 2**53 - 1:
        raise ValueError("Simulation clock exceeds supported range")
    if now < _clock(state.now_utc):
        raise ValueError("UTC clock cannot move backward")
    payload_digest = _digest_command(command)
    old = _find(state.operations, "operation_id", command.operation_id)
    if old:
        if old.payload_digest != payload_digest:
            return DeviceResult(state=state, outcome="rejected", reason="operation_id_collision")
        return DeviceResult(state=state, outcome=old.outcome, reason=old.reason)
    if len(state.operations) >= 512:
        return DeviceResult(state=state, outcome="held", reason="operation_history_full")
    s = state.model_copy(deep=True)
    s.now_utc = now_utc
    s.sim_time_ms = sim_time_ms
    outcome, reason = _apply(s, command, now, sim_time_ms)
    s.operations.append(OperationRecord(operation_id=command.operation_id,
                                        payload_digest=payload_digest,
                                        outcome=outcome, reason=reason))
    s.version += 1
    return DeviceResult(state=DeviceState.model_validate(s.model_dump()),
                        outcome=outcome, reason=reason)


def _apply(s, c, now, sim_time_ms):
    if c.action in {"claim_alarm", "clear_alarm", "alarm_feedback"}:
        return _alarm(s, c)
    if c.action in {"set_entry_policy", "command_gate", "gate_feedback", "tick", "cancel_gate"}:
        return _gate(s, c, sim_time_ms)
    return _broadcast(s, c, now)


def _alarm(s, c):
    zone = _find(s.alarms, "zone_id", c.zone_id)
    if zone is None:
        return "rejected", "unknown_alarm_zone"
    if c.expected_version != zone.resource_version:
        return "held", "stale_alarm_version"
    if c.action == "claim_alarm":
        if c.incident_id is None or c.evidence_version is None:
            return "rejected", "missing_claim_evidence"
        old = _find(zone.claims, "incident_id", c.incident_id)
        if old and c.evidence_version <= old.evidence_version:
            return "held", "stale_claim_evidence"
        if old:
            old.evidence_version = c.evidence_version
        elif len(zone.claims) < 128:
            zone.claims.append(AlarmClaim(incident_id=c.incident_id,
                                          evidence_version=c.evidence_version))
        else:
            return "held", "alarm_claim_limit"
        zone.desired_active = True
        if zone.visual == "off":
            zone.visual = "pending"
        if zone.audio == "off":
            zone.audio = "pending"
        zone.resource_version += 1
        return "accepted", "alarm_claim_recorded"
    if c.action == "clear_alarm":
        old = _find(zone.claims, "incident_id", c.incident_id)
        if old is None:
            return "held", "unknown_incident_claim"
        if (c.current_observation is not True or c.evidence_version is None
                or c.evidence_version <= old.evidence_version):
            return "held", "clear_needs_new_current_evidence"
        zone.claims.remove(old)
        zone.desired_active = bool(zone.claims)
        if not zone.desired_active:
            zone.visual = "pending"
            zone.audio = "pending"
        zone.resource_version += 1
        return "accepted", "remaining_claims_hold_alarm" if zone.claims else "alarm_clear_requested"
    if c.channel not in {"visual", "audio"} or c.feedback not in {"on", "off", "failed", "unknown"}:
        return "rejected", "invalid_alarm_feedback"
    if c.feedback in {"on", "off"} and (c.feedback == "on") != zone.desired_active:
        return "held", "feedback_disagrees_with_current_claims"
    setattr(zone, c.channel, c.feedback)
    zone.resource_version += 1
    return "accepted", "alarm_channel_feedback_recorded"


def _gate(s, c, sim_time_ms):
    gate = _find(s.gates, "gate_id", c.gate_id)
    if gate is None:
        return "rejected", "unknown_gate"
    if c.expected_version != gate.resource_version:
        return "held", "stale_gate_version"
    if c.action == "tick":
        gate.obstacle_detected = c.obstacle_detected
        if gate.physical_state == "closing" and c.obstacle_detected is not False:
            gate.physical_state = "stopped"
            gate.transition_due_ms = None
            gate.transition_target = None
            gate.last_feedback = "safety_stop"
            gate.resource_version += 1
            return "held", "closing_stopped_for_obstacle_or_unknown"
        if gate.transition_due_ms is not None and sim_time_ms >= gate.transition_due_ms:
            # Completion still requires separate device feedback. Time alone is not proof.
            gate.last_feedback = "unknown"
            gate.resource_version += 1
            return "unknown", "transition_requires_feedback"
        gate.resource_version += 1
        return "accepted", "gate_sensor_sampled"
    if c.action == "set_entry_policy":
        if gate.direction != "entry" or c.target not in {"allow", "deny"}:
            return "rejected", "unsupported_entry_policy"
        if c.target == "deny":
            exit_gate = next((g for g in s.gates if g.direction == "exit"), None)
            announcement = _find(s.broadcasts, "operation_id", c.broadcast_operation_id)
            if c.outbound_clear is not True or exit_gate is None or exit_gate.physical_state != "open":
                return "held", "exit_route_not_confirmed"
            if announcement is None or announcement.receipt != "accepted" or announcement.simulated_playback != "played":
                return "held", "closure_announcement_unconfirmed"
        gate.entry_policy = c.target
        gate.resource_version += 1
        return "accepted", "entry_policy_recorded"
    if c.action == "command_gate":
        if c.target not in {"open", "closed"}:
            return "rejected", "unsupported_gate_target"
        if gate.active_operation_id or gate.physical_state in {"opening", "closing", "unknown"}:
            return "held", "gate_result_pending"
        if c.target == "closed":
            if gate.direction == "exit":
                return "rejected", "independent_exit_must_remain_open"
            if gate.entry_policy != "deny":
                return "held", "entry_policy_still_allows_entry"
            if c.obstacle_detected is not False or gate.obstacle_detected is not False:
                return "held", "close_needs_clear_current_sensor"
        if gate.physical_state == c.target:
            return "accepted", "gate_already_at_target"
        if sim_time_ms > 2**53 - 1 - s.gate_transition_ms:
            return "held", "gate_transition_exceeds_clock_range"
        gate.physical_state = "opening" if c.target == "open" else "closing"
        gate.transition_target = c.target
        gate.transition_due_ms = sim_time_ms + s.gate_transition_ms
        gate.active_operation_id = c.operation_id
        gate.last_feedback = "none"
        gate.resource_version += 1
        return "accepted", "gate_transition_started"
    if c.action == "cancel_gate":
        if gate.active_operation_id is None:
            return "held", "no_pending_gate_transition"
        if gate.physical_state == "unknown" or gate.last_feedback in {"unknown", "failed"}:
            return "held", "unknown_gate_result_needs_reconciliation"
        gate.physical_state = "stopped"
        gate.transition_target = None
        gate.transition_due_ms = None
        gate.active_operation_id = None
        gate.last_feedback = "safety_stop"
        gate.resource_version += 1
        return "accepted", "unapplied_transition_cancelled"
    if c.action == "gate_feedback":
        if gate.active_operation_id is None:
            return "held", "no_pending_gate_transition"
        if gate.transition_target is None:
            return "held", "safety_stopped_transition_needs_reconciliation"
        if c.feedback not in {"played", "failed", "unknown"}:
            return "rejected", "invalid_gate_feedback"
        if c.feedback == "played":
            if gate.transition_target == "closed" and c.obstacle_detected is not False:
                gate.physical_state = "stopped"
                gate.last_feedback = "safety_stop"
                gate.active_operation_id = None
                gate.transition_target = None
                gate.transition_due_ms = None
                gate.resource_version += 1
                return "held", "close_feedback_needs_clear_sensor"
            gate.physical_state = gate.transition_target
            gate.last_feedback = "applied"
            gate.active_operation_id = None
            gate.transition_target = None
            gate.transition_due_ms = None
            gate.obstacle_detected = c.obstacle_detected
            gate.resource_version += 1
            return "accepted", "gate_position_confirmed"
        gate.physical_state = "unknown" if c.feedback == "unknown" else "stopped"
        gate.last_feedback = c.feedback
        gate.transition_due_ms = None
        # Keep target and active ID for result reconciliation; a new command cannot overtake it.
        gate.resource_version += 1
        return "unknown" if c.feedback == "unknown" else "held", "gate_result_needs_reconciliation"
    return "rejected", "unsupported_gate_action"


def _broadcast(s, c, now):
    if c.action == "broadcast":
        if c.zone_id not in {zone.zone_id for zone in s.alarms} or c.message_id not in s.allowed_messages:
            return "rejected", "broadcast_not_allowed"
        if len(s.broadcasts) >= 256:
            return "held", "broadcast_history_full"
        for prior in reversed(s.broadcasts):
            if prior.zone_id == c.zone_id:
                elapsed = (now - _clock(prior.requested_at)).total_seconds()
                if elapsed < s.broadcast_cooldown_s:
                    return "held", "broadcast_frequency_limit"
                break
        s.broadcasts.append(BroadcastRecord(operation_id=c.operation_id,
                                            zone_id=c.zone_id, message_id=c.message_id,
                                            requested_at=s.now_utc, receipt="accepted",
                                            simulated_playback="pending",
                                            browser_playback="not_requested"))
        return "accepted", "broadcast_received_not_played"
    prior = _find(s.broadcasts, "operation_id", c.broadcast_operation_id)
    if prior is None:
        return "held", "unknown_broadcast_operation"
    if c.action == "cancel_broadcast":
        if (prior.receipt != "accepted" or prior.simulated_playback != "pending"
                or prior.browser_playback not in {"not_requested", "failed"}):
            return "held", "broadcast_result_needs_reconciliation"
        prior.simulated_playback = "cancelled"
        return "accepted", "pending_broadcast_cancelled"
    if c.action != "broadcast_feedback" or c.channel not in {"receipt", "simulated_playback", "browser_playback"}:
        return "rejected", "invalid_broadcast_feedback"
    mapping = {"accepted": "accepted", "played": "played", "failed": "failed", "unknown": "unknown"}
    if c.feedback not in mapping or (c.channel == "receipt" and c.feedback == "played") or (c.channel != "receipt" and c.feedback == "accepted"):
        return "rejected", "invalid_broadcast_feedback"
    if c.channel == "simulated_playback" and c.feedback == "played" and prior.receipt != "accepted":
        return "held", "broadcast_receipt_unconfirmed"
    if c.channel == "simulated_playback" and prior.simulated_playback == "cancelled":
        return "held", "broadcast_cancelled"
    setattr(prior, c.channel, mapping[c.feedback])
    return ("unknown" if c.feedback == "unknown" else "accepted" if c.feedback in {"accepted", "played"} else "held",
            "broadcast_channel_feedback_recorded")
