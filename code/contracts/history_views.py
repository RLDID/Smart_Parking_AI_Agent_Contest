"""Additive, allowlisted history read contracts; no raw business payloads."""
from typing import Literal
from pydantic import Field
from contracts.models import Contract, Identifier, UtcTimestamp


class HistoryCommand(Contract):
    command_id: Identifier
    facility_id: Identifier
    run_id: Identifier
    purpose: str
    aggregate_status: str
    cancellation_requested_at: UtcTimestamp | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    resource_version: int


class HistoryExecution(Contract):
    execution_id: Identifier
    facility_id: Identifier
    run_id: Identifier
    command_id: Identifier | None = None
    incident_id: Identifier | None = None
    plan_id: Identifier | None = None
    tool_name: str
    status: str
    mode: Literal['synthetic_demo', 'simulated', 'live']
    applied_sim_time_ms: int | None = None
    cancellation_requested_at: UtcTimestamp | None = None
    error_code: str | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    resource_version: int


class HistoryPage(Contract):
    as_of_utc: UtcTimestamp
    next_cursor: str | None = None
    consistency: Literal['immutable_projection_current_authority'] = 'immutable_projection_current_authority'
    preservation_limits: list[str] = Field(default_factory=list)


class CommandsHistory(HistoryPage):
    items: list[HistoryCommand]


class ExecutionsHistory(HistoryPage):
    items: list[HistoryExecution]


class TimelineDetails(Contract):
    status: str | None = None
    mode: str | None = None
    tool_name: str | None = None
    purpose: str | None = None
    delivery_status: str | None = None
    response: str | None = None
    attempt_number: int | None = None
    error_code: str | None = None
    action: str | None = None
    outcome: str | None = None
    reason_code: str | None = None
    clock: str | None = None
    due_sim_time_ms: int | None = None
    due_at: UtcTimestamp | None = None
    received_at: UtcTimestamp | None = None
    created_at: UtcTimestamp | None = None
    updated_at: UtcTimestamp | None = None
    resource_version: int | None = None


class TimelineRecord(Contract):
    event_id: str
    kind: Literal['incident.latest_state', 'execution.latest_state', 'notification.latest_state',
                  'delivery_attempt.latest_state', 'notification.receipt', 'notification.response',
                  'followup.latest_state', 'audit']
    record_id: Identifier
    parent_record_id: Identifier | None = None
    recorded_at_utc: UtcTimestamp
    sim_time_ms: int | None = None
    details: TimelineDetails


class IncidentTimeline(HistoryPage):
    incident_id: Identifier
    facility_id: Identifier
    run_id: Identifier
    items: list[TimelineRecord]


class StepAttempt(Contract):
    execution_id: Identifier
    linkage_status: Literal['linked', 'unknown']
    reason_code: str | None = None
    execution: HistoryExecution | None = None


class PlanStepProgress(Contract):
    index: int
    step_id: Identifier | None = None
    tool_name: str | None = None
    zone_id: Identifier | None = None
    status: str
    reason_code: str | None = None
    attempts: list[StepAttempt]


class PlanProgress(Contract):
    plan_id: Identifier
    status: str
    resource_version: int
    steps: list[PlanStepProgress]


class CommandProgress(Contract):
    as_of_utc: UtcTimestamp
    command: HistoryCommand
    plans: list[PlanProgress]
    preservation_limits: list[str]
