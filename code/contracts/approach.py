"""Bounded S2 projection from public observations, not an alarm policy."""

from typing import Literal

from pydantic import Field

from contracts.models import Contract, Identifier


class ApproachSettings(Contract):
    """Caller supplied proposal values; none is an adopted safety threshold."""

    horizon_ms: int = Field(ge=100, le=10000, strict=True)
    margin_m: float = Field(ge=0, le=10)
    freshness_ms: int = Field(ge=100, le=10000, strict=True)
    max_sample_gap_ms: int = Field(ge=50, le=5000, strict=True)
    max_uncertainty_m: float = Field(ge=0, le=10)
    max_speed_mps: float = Field(gt=0, le=20)
    max_heading_change_deg: float = Field(ge=0, le=45)


class ApproachCandidate(Contract):
    vehicle_id: Identifier
    pedestrian_id: Identifier
    projected_first_proximity_ms: float = Field(ge=0)
    position_uncertainty_m: float = Field(ge=0)


class ApproachAnalysis(Contract):
    status: Literal["risk_candidate", "clear_projection", "insufficient_data", "unsupported_geometry"]
    facility_id: Identifier | None
    run_id: Identifier | None
    map_version: Identifier | None
    map_digest: str | None
    evaluated_at_sim_time_ms: int = Field(ge=0, strict=True)
    observation_ids: list[Identifier]
    candidates: list[ApproachCandidate]
    reasons: list[str]
    assumptions: list[str]
