"""Validated business inputs and canonical execution output (schema v0.1-draft)."""
from typing import Literal

from pydantic import Field, model_validator

from contracts.knowledge import KnowledgeEvidence
from contracts.models import Contract, Identifier, UtcTimestamp


IncidentStatus = Literal["candidate", "active", "monitoring", "needs_review", "escalated", "resolved", "closed_no_issue", "closed_false_positive"]
ExecutionStatus = Literal["requested", "accepted", "running", "succeeded", "failed", "held", "cancelled", "unknown"]


class BusinessContext(Contract):
    facility_id: Identifier
    run_id: Identifier
    based_on_state_version: int = Field(ge=0, strict=True)
    policy_version: int = Field(ge=1, strict=True)
    incident_id: Identifier | None = None
    command_id: Identifier | None = None
    plan_id: Identifier | None = None
    knowledge_evidence: KnowledgeEvidence | None = None


class IncidentImpact(Contract):
    type: Literal["aisle_obstruction", "exit_blocked", "bay_intrusion", "approach_risk"]
    object_id: Identifier | None = None
    zone_id: Identifier
    condition_json: dict = Field(default_factory=dict)


class IncidentInput(BusinessContext):
    primary_object_id: Identifier
    status: IncidentStatus
    impacts: list[IncidentImpact] = Field(min_length=1)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=64)
    reason_summary: str = Field(min_length=1, max_length=1000)
    expected_resource_version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def existing_incident_needs_version(self):
        if self.incident_id is not None and self.expected_resource_version is None:
            raise ValueError("Updating an incident requires expected_resource_version")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Duplicate observation evidence")
        return self


class MoveTemplateArgs(Contract):
    zone_label: str = Field(min_length=1, max_length=100)


class NotifyVehicle(BusinessContext):
    recipient_ref: Identifier
    purpose: Literal["move_request"] = "move_request"
    template_id: Literal["move_request_v1"] = "move_request_v1"
    template_args: MoveTemplateArgs
    contact_sequence: int = Field(ge=1, strict=True)
    expected_resource_version: int = Field(ge=1, strict=True)


class ReportOwner(BusinessContext):
    reason_code: Identifier
    summary: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[Identifier] = Field(default_factory=list)


class FollowupInput(BusinessContext):
    clock: Literal["sim", "wall"]
    due_sim_time_ms: int | None = Field(default=None, ge=0, strict=True)
    due_at: UtcTimestamp | None = None
    condition: Literal["spatial_recheck", "exit_recheck", "bay_recheck", "approach_recheck",
                       "device_recheck", "command_recheck", "response_timeout"]
    max_attempts: int = Field(ge=1, le=3, strict=True)

    @model_validator(mode="after")
    def deadline_matches_clock(self):
        if (self.clock == "sim") != (self.due_sim_time_ms is not None) or (self.clock == "wall") != (self.due_at is not None):
            raise ValueError("Exactly one deadline matching clock is required")
        if self.incident_id is None and self.command_id is None:
            raise ValueError("Followup requires incident or command")
        return self


class CommandInput(Contract):
    run_id: Identifier
    purpose: Literal["query", "operational_goal", "own_vehicle_query", "report_exit_blocked"]
    text: str = Field(min_length=1, max_length=2000)
    target_vehicle_id: Identifier | None = None
    based_on_state_version: int = Field(ge=0, strict=True)


class CancelInput(Contract):
    expected_resource_version: int = Field(ge=1, strict=True)
    reason: str | None = Field(default=None, max_length=500)


class ReceiptInput(Contract):
    client_request_id: Identifier
    received_at: UtcTimestamp


class ResponseInput(Contract):
    client_request_id: Identifier
    response: Literal["acknowledged", "will_move", "cannot_move", "question"]
    text: str | None = Field(default=None, max_length=1000)


class ExecutionView(Contract):
    execution_id: Identifier
    status: ExecutionStatus
    mode: Literal["synthetic_demo", "simulated", "live"]
    resource_version: int = Field(ge=1, strict=True)
    result: dict | None = None
    error: dict | None = None
