"""Observation-only S1-b supported exit candidate, separate from incident policy."""

from typing import Literal

from pydantic import Field

from contracts.models import Contract, Identifier, UtcTimestamp


class ExitGeometrySettings(Contract):
    # Explicit experiment/operating inputs. No adopted S1-b policy values exist yet.
    side_clearance_m: float = Field(ge=0)
    max_observation_uncertainty_m: float = Field(ge=0)
    freshness_ms: int = Field(gt=0, strict=True)
    max_discretization_error_m: float = Field(gt=0, le=0.1)


class ExitCandidateAnalysis(Contract):
    analysis_version: Literal["s1b-fixed-exit-v1"] = "s1b-fixed-exit-v1"
    facility_id: Identifier
    run_id: Identifier
    map_version: Identifier
    route_policy_version: Identifier
    candidate_id: Literal["b01-reverse-arc-west-v1"] = "b01-reverse-arc-west-v1"
    observation_ids: list[Identifier]
    state_version: int = Field(ge=0)
    evaluated_at_sim_time_ms: int = Field(ge=0)
    analyzed_at: UtcTimestamp
    support_status: Literal["supported", "insufficient_data", "unsupported_geometry"]
    candidate_passage: Literal["clear", "blocked", "unknown"]
    collision_object_ids: list[Identifier]
    reasons: list[str]
    # Upper bound on each arc body's displacement between a sample and its
    # nearest endpoint. Included in the conservative sweep buffer.
    arc_discretization_bound_m: float | None = Field(default=None, ge=0)
    applied_clearance_m: float | None = Field(default=None, ge=0)
    obstacle_uncertainty_m: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str]
