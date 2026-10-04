"""Observation-history features, without parking/notification policy defaults."""
from typing import Literal

from pydantic import Field

from contracts.models import Contract, Identifier, UtcTimestamp
from contracts.s1c_bay_geometry import BayGeometryAnalysis, BayGeometrySettings


class ParkingAssessmentSettings(Contract):
    geometry: BayGeometrySettings
    stationary_ms: int = Field(gt=0, le=86_400_000, strict=True)
    position_tolerance_m: float = Field(ge=0, le=100)
    heading_tolerance_deg: float = Field(ge=0, le=180)
    maneuver_grace_ms: int = Field(ge=0, le=86_400_000, strict=True)
    intrusion_hold_ms: int = Field(gt=0, le=86_400_000, strict=True)
    max_sample_gap_ms: int = Field(gt=0, le=60_000, strict=True)
    history_limit: int = Field(ge=2, le=512, strict=True)


class ParkingAssessment(Contract):
    analysis_version: Literal["s1c-parking-history-v1"] = "s1c-parking-history-v1"
    facility_id: Identifier
    run_id: Identifier
    map_version: Identifier
    object_id: Identifier
    bay_id: Identifier
    state_version: int = Field(ge=0, strict=True)
    evaluated_at_sim_time_ms: int = Field(ge=0, strict=True)
    analyzed_at: UtcTimestamp
    support_status: Literal["supported", "insufficient_data", "unsupported_geometry"]
    observation_ids: list[Identifier]
    motion_state: Literal["stationary_candidate", "moving", "insufficient_history", "unknown"]
    stationary_duration_ms: int | None = Field(default=None, ge=0, strict=True)
    intrusion_state: Literal["within_tolerance", "transient_or_maneuver", "persistent_candidate", "unknown"]
    intrusion_duration_ms: int | None = Field(default=None, ge=0, strict=True)
    geometry: BayGeometryAnalysis
    reasons: list[str]
    applied_settings: ParkingAssessmentSettings
    assumptions: list[str]
