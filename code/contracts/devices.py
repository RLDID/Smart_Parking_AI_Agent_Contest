"""Bounded synthetic device state. No physical equipment or AI authority is implied."""

from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Contract, UtcTimestamp


Outcome = Literal["accepted", "held", "rejected", "unknown"]
Feedback = Literal["off", "pending", "on", "failed", "unknown"]


class AlarmClaim(Contract):
    incident_id: str = Field(min_length=1, max_length=128)
    evidence_version: int = Field(ge=0, strict=True)


class AlarmZoneState(Contract):
    zone_id: str = Field(min_length=1, max_length=128)
    resource_version: int = Field(ge=0, strict=True)
    claims: list[AlarmClaim] = Field(max_length=128)
    desired_active: bool
    visual: Feedback
    audio: Feedback

    @model_validator(mode="after")
    def aggregate_matches_claims(self):
        if self.desired_active != bool(self.claims):
            raise ValueError("Alarm desired state must match incident claims")
        if len({claim.incident_id for claim in self.claims}) != len(self.claims):
            raise ValueError("Duplicate alarm incident")
        return self


class GateState(Contract):
    gate_id: str = Field(min_length=1, max_length=128)
    direction: Literal["entry", "exit"]
    resource_version: int = Field(ge=0, strict=True)
    entry_policy: Literal["allow", "deny"] = "allow"
    physical_state: Literal["open", "closed", "opening", "closing", "stopped", "unknown"] = "open"
    obstacle_detected: bool | None = None
    active_operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    transition_target: Literal["open", "closed"] | None = None
    transition_due_ms: int | None = Field(default=None, ge=0, strict=True)
    last_feedback: Literal["none", "applied", "failed", "unknown", "safety_stop"] = "none"


class BroadcastRecord(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    zone_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    requested_at: UtcTimestamp
    receipt: Literal["accepted", "failed", "unknown"]
    simulated_playback: Literal["pending", "played", "failed", "unknown", "cancelled"]
    browser_playback: Literal["not_requested", "pending", "played", "failed", "unknown"]


class OperationRecord(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Outcome
    reason: str = Field(max_length=128)


class DeviceState(Contract):
    kind: Literal["synthetic_device_state"] = "synthetic_device_state"
    facility_id: str = Field(min_length=1, max_length=128)
    map_version: str = Field(min_length=1, max_length=128)
    map_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=0, strict=True)
    now_utc: UtcTimestamp
    sim_time_ms: int = Field(ge=0, le=2**53 - 1, strict=True)
    broadcast_cooldown_s: int = Field(ge=0, le=3600, strict=True)
    gate_transition_ms: int = Field(ge=1, le=30000, strict=True)
    allowed_messages: list[str] = Field(min_length=1, max_length=32)
    alarms: list[AlarmZoneState] = Field(max_length=32)
    gates: list[GateState] = Field(min_length=2, max_length=8)
    broadcasts: list[BroadcastRecord] = Field(max_length=256)
    operations: list[OperationRecord] = Field(max_length=512)

    @model_validator(mode="after")
    def unique_resources(self):
        for values in (self.allowed_messages, [z.zone_id for z in self.alarms],
                       [g.gate_id for g in self.gates],
                       [b.operation_id for b in self.broadcasts],
                       [o.operation_id for o in self.operations]):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate device resource or operation")
        return self


class DeviceCommand(Contract):
    action: Literal["claim_alarm", "clear_alarm", "alarm_feedback", "set_entry_policy",
                    "command_gate", "gate_feedback", "tick", "broadcast",
                    "broadcast_feedback", "cancel_gate", "cancel_broadcast"]
    operation_id: str = Field(min_length=1, max_length=128)
    zone_id: str | None = Field(default=None, min_length=1, max_length=128)
    incident_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_version: int | None = Field(default=None, ge=0, strict=True)
    current_observation: bool | None = None
    expected_version: int | None = Field(default=None, ge=0, strict=True)
    gate_id: str | None = Field(default=None, min_length=1, max_length=128)
    target: Literal["open", "closed", "allow", "deny"] | None = None
    obstacle_detected: bool | None = None
    outbound_clear: bool | None = None
    broadcast_operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    message_id: str | None = Field(default=None, min_length=1, max_length=128)
    channel: Literal["visual", "audio", "receipt", "simulated_playback", "browser_playback"] | None = None
    feedback: Literal["on", "off", "failed", "unknown", "accepted", "played"] | None = None


class DeviceResult(Contract):
    state: DeviceState
    outcome: Outcome
    reason: str = Field(max_length=128)
