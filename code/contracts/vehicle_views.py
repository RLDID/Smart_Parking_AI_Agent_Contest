"""Additive, private vehicle observations and a static map projection."""
from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Contract, Identifier, Position, Quality, Size, UtcTimestamp


class VehicleLocation(Contract):
    registered_vehicle_id: Identifier
    display_alias: str
    object_id: Identifier | None
    position: Position | None
    size: Size | None
    heading_deg: float | None = Field(ge=0, lt=360)
    quality: Quality | None
    observed_bay_id: Identifier | None
    location_status: Literal["observed_bay", "observed_position", "unknown"]
    location_reason: Literal[
        "stationary_whole_footprint_in_unique_bay", "no_verified_relationship",
        "ambiguous_relationship", "target_not_observed", "observation_unavailable",
        "observation_insufficient", "stale_observation", "run_not_ready",
        "moving", "insufficient_history", "uncertain_stationarity",
        "outside_or_intruding_bay", "ambiguous_bay", "unsupported_geometry",
    ]
    evidence_observation_ids: list[Identifier] = Field(default_factory=list)
    evidence_reasons: list[str] = Field(default_factory=list)
    stationary_duration_ms: int | None = Field(default=None, ge=0, strict=True)
    bay_semantics: Literal["observed_not_assigned"] = "observed_not_assigned"

    @model_validator(mode="after")
    def bay_requires_evidence(self):
        if self.location_status == "observed_bay":
            if self.observed_bay_id is None or self.position is None or self.object_id is None:
                raise ValueError("An observed bay requires an authorised observed position")
        elif self.observed_bay_id is not None:
            raise ValueError("Unconfirmed locations cannot carry a bay")
        if self.location_status == "unknown" and self.position is not None:
            raise ValueError("An unknown location cannot carry a current position")
        return self


class VehicleLocations(Contract):
    view_version: Literal["own-vehicle-locations-v1"] = "own-vehicle-locations-v1"
    view_scope: Literal["own_vehicles"] = "own_vehicles"
    facility_id: Identifier
    run_id: Identifier
    map_version: Identifier
    observation_id: Identifier
    state_version: int = Field(ge=0, strict=True)
    sim_time_ms: int = Field(ge=0, strict=True)
    observed_at: UtcTimestamp
    received_at: UtcTimestamp
    coverage: Literal["complete", "partial", "unavailable"]
    run_status: Literal["running", "paused", "stopped", "replaying"]
    recovery_required: bool
    applied_state_version: int = Field(ge=0, strict=True)
    applied_sim_time_ms: int = Field(ge=0, strict=True)
    settings_version: str | None
    vehicles: list[VehicleLocation]


class ParkingCoordinates(Contract):
    unit: Literal["m"]
    origin: Literal["southwest"]
    x_axis: Literal["east"]
    y_axis: Literal["north"]


class ParkingBounds(Contract):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class ParkingZone(Contract):
    zone_id: Identifier
    type: Literal["parking_bay", "aisle", "entrance", "exit", "pedestrian", "announcement"]
    polygon: list[Position]


class ParkingBay(Contract):
    zone_id: Identifier


class OwnParkingMap(Contract):
    view_version: Literal["static-parking-map-v1"] = "static-parking-map-v1"
    view_scope: Literal["static_geometry"] = "static_geometry"
    facility_id: Identifier
    map_version: Identifier
    coordinate_system: ParkingCoordinates
    bounds: ParkingBounds
    zones: list[ParkingZone]
    parking_bays: list[ParkingBay]
