"""Bounded autonomous work requests and model decisions.

The decision is advice. It never carries a tool name, device command, policy
version, recipient, or evidence reference supplied by the model.
"""
from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Contract, Identifier


Scenario = Literal["s1a", "s1b", "s1c", "s2", "s3"]
Action = Literal["notify", "recheck", "clarify", "announce", "restrict_entry", "report", "hold"]


class AutonomousDecision(Contract):
    scenario: Scenario
    action: Action
    target_ref: Identifier | None = None
    reason_code: Identifier = Field(max_length=80)
    rationale: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def action_matches_scenario(self):
        permitted = {
            "s1a": {"notify", "recheck", "report", "hold"},
            "s1b": {"notify", "recheck", "report", "hold"},
            "s1c": {"notify", "recheck", "report", "hold"},
            "s2": {"recheck", "report", "hold"},
            "s3": {"clarify", "announce", "restrict_entry", "report", "hold"},
        }
        if self.action not in permitted[self.scenario]:
            raise ValueError("Decision action is not allowed for this scenario")
        if self.action == "notify" and self.target_ref is None:
            raise ValueError("Vehicle notification needs a target")
        return self


class AutonomousControl(Contract):
    run_id: Identifier
    action: Literal["start", "stop", "process"]
    mode: Literal["mock", "live"] = "mock"
    scenario: Scenario | None = None
    command_id: Identifier | None = None

    @model_validator(mode="after")
    def process_needs_scope(self):
        if self.action == "process" and self.scenario is None:
            raise ValueError("Processing requires a scenario")
        if self.command_id is not None and self.scenario != "s3":
            raise ValueError("Commands are only used in S3")
        return self


class CommandClarification(Contract):
    expected_resource_version: int = Field(ge=1, strict=True)
    goal: Literal["closing", "zone_notice"]
    zone_id: Literal["announcement-a"] | None = None

    @model_validator(mode="after")
    def zone_matches_goal(self):
        if self.goal == "zone_notice" and self.zone_id != "announcement-a":
            raise ValueError("The supported notice needs announcement-a")
        if self.goal == "closing" and self.zone_id is not None:
            raise ValueError("Closing uses both announcement zones")
        return self
