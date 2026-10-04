"""One observed vehicle footprint measured against one public parking bay."""

from typing import Literal

from pydantic import Field

from contracts.models import Contract, Identifier, UtcTimestamp


# Geometry/clock implementation limits, not parking or contact policy.
MAX_GEOMETRY_DISTANCE_M = 100.0
MAX_FRESHNESS_MS = 999_999_999 * 86_400_000  # Whole days within timedelta.max.


class BayGeometrySettings(Contract):
    # Supplied by the caller for each experiment; no operating default exists.
    tolerance_m: float = Field(ge=0, le=MAX_GEOMETRY_DISTANCE_M)
    max_uncertainty_m: float = Field(ge=0, le=MAX_GEOMETRY_DISTANCE_M)
    freshness_ms: int = Field(gt=0, le=MAX_FRESHNESS_MS, strict=True)


class BayOverlapArea(Contract):
    nominal_m2: float = Field(ge=0)
    lower_bound_m2: float = Field(ge=0)
    upper_bound_m2: float = Field(ge=0)


class BayGeometryAnalysis(Contract):
    analysis_version: Literal["s1c-bay-footprint-v1"] = "s1c-bay-footprint-v1"
    facility_id: Identifier
    run_id: Identifier
    map_version: Identifier
    map_digest: str | None
    observation_id: Identifier
    state_version: int = Field(ge=0)
    evaluated_at_sim_time_ms: int = Field(ge=0)
    analyzed_at: UtcTimestamp
    object_id: Identifier
    bay_id: Identifier
    support_status: Literal["supported", "insufficient_data", "unsupported_geometry"]
    geometry_relation: Literal["within", "overlap", "unknown"]
    reasons: list[str]
    nominal_outside_area_m2: float | None = Field(default=None, ge=0)
    outside_area_lower_bound_m2: float | None = Field(default=None, ge=0)
    outside_area_upper_bound_m2: float | None = Field(default=None, ge=0)
    nominal_max_depth_m: float | None = Field(default=None, ge=0)
    max_depth_lower_bound_m: float | None = Field(default=None, ge=0)
    max_depth_upper_bound_m: float | None = Field(default=None, ge=0)
    adjacent_bay_overlap_m2: dict[str, BayOverlapArea] = Field(default_factory=dict)
    applied_tolerance_m: float | None = Field(default=None, ge=0)
    position_uncertainty_m: float | None = Field(default=None, ge=0)
    assumptions: list[str]
