from typing import Literal

from pydantic import Field

from contracts.models import Contract, Identifier, UtcTimestamp


class StopMetric(Contract):
    object_id: Identifier
    stop_duration_ms: int = Field(ge=0)
    stationary_candidate: bool


class SpatialMetrics(Contract):
    passage: Literal["clear", "blocked", "unknown"] = "unknown"
    available_clearance_m: float | None = Field(default=None, ge=0)
    required_clearance_m: float = Field(gt=0)
    occupied_object_ids: list[Identifier] = Field(default_factory=list)
    blocked_duration_ms: int | None = Field(default=None, ge=0)
    clear_duration_ms: int | None = Field(default=None, ge=0)
    clearance_sustained: bool | None = None
    objects: list[StopMetric] = Field(default_factory=list)


class AnalysisQuality(Contract):
    freshness: Literal["fresh", "stale", "unknown"]
    run_status: Literal["running", "paused", "stopped", "replaying"]
    observation_age_sim_ms: int
    received_age_wall_ms: int
    reasons: list[str]


class SpatialAnalysis(Contract):
    analysis_id: Identifier
    analysis_version: Literal["west-straight-v1"] = "west-straight-v1"
    facility_id: Identifier
    run_id: Identifier
    map_version: Identifier
    route_policy_version: Identifier
    observation_ids: list[Identifier]
    state_version: int = Field(ge=0)
    evaluated_at_sim_time_ms: int = Field(ge=0)
    analyzed_at: UtcTimestamp
    object_ids: list[Identifier]
    zone_ids: list[Identifier]
    metrics: SpatialMetrics
    assumptions: list[str]
    quality: AnalysisQuality
    support_status: Literal["supported", "insufficient_data", "unsupported_geometry"]
