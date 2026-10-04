from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1-draft"


def utc_timestamp(value: str):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if "T" not in value or not value.endswith(("Z", "+00:00")) or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC RFC3339 timestamp required")
    return value


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
UtcTimestamp = Annotated[str, AfterValidator(utc_timestamp)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Position(Contract):
    x: float
    y: float


class Size(Contract):
    length_m: float = Field(gt=0)
    width_m: float = Field(gt=0)


class Quality(Contract):
    visibility: Literal["visible", "occluded", "missing"] = "visible"
    uncertainty_m: float | None = Field(default=0, ge=0)
    missing_fields: list[str] = Field(default_factory=list)


class ObservedObject(Contract):
    object_id: str = Field(min_length=1)
    object_type: Literal["vehicle", "pedestrian", "unknown"]
    position: Position | None
    size: Size | None
    heading_deg: float | None = Field(ge=0, lt=360)
    quality: Quality


class ObjectEvent(Contract):
    object_id: Identifier
    event_type: Literal["entered", "exited"]
    portal_id: Identifier
    sim_time_ms: int = Field(ge=0, strict=True)
    observed_at: UtcTimestamp
    quality: Quality


class GateObservation(Contract):
    device_id: Identifier
    type: Literal["gate"]
    resource_version: int = Field(ge=0, strict=True)
    entry_policy: Literal["allow", "deny"]
    physical_state: Literal["open", "closed", "opening", "closing", "stopped", "unknown"]
    obstacle_detected: bool | None
    fault_code: str | None
    observed_at: UtcTimestamp
    quality: Quality


class Observation(Contract):
    schema_version: Literal["0.1-draft"] = SCHEMA_VERSION
    facility_id: Identifier
    run_id: Identifier
    observation_id: Identifier
    source: Literal["simulator"] = "simulator"
    map_version: Identifier
    state_version: int = Field(ge=0, strict=True)
    sim_time_ms: int = Field(ge=0, strict=True)
    observed_at: UtcTimestamp
    received_at: UtcTimestamp
    coverage: Literal["complete", "partial", "unavailable"]
    objects: list[ObservedObject]
    devices: list[GateObservation]
    object_events: list[ObjectEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def events_are_observed_history(self):
        if any(event.sim_time_ms > self.sim_time_ms for event in self.object_events):
            raise ValueError("Future object events cannot be observations")
        return self


class StateView(Contract):
    snapshot: Observation
    run_status: Literal["running", "paused", "stopped", "replaying"]
    recovery_required: bool
    applied_state_version: int = Field(ge=0, strict=True)
    applied_sim_time_ms: int = Field(ge=0, strict=True)


class RunView(StateView):
    run_id: Identifier


class DriverStateView(StateView):
    view_scope: Literal["own_vehicles"]
    registered_vehicle_ids: list[Identifier]


class RegisteredVehicle(Contract):
    registered_vehicle_id: Identifier
    facility_id: Identifier
    display_alias: str


class VehicleList(Contract):
    facility_id: Identifier
    vehicles: list[RegisteredVehicle]


class Login(Contract):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class CreateRun(Contract):
    facility_id: Literal["fac-demo-01"]
    fixture_ref: Literal["s1a-foundation-v1", "s1b-blocked-v1", "s1b-clear-v1",
                         "s1c-overlap-v1", "s1c-contained-v1", "s2-crossing-v1",
                         "s2-offset-v1", "s2-occluded-v1", "s3-closing-v1",
                         "s3-gate-obstacle-v1"]
    seed: int = Field(ge=0, le=2**31 - 1, strict=True)
    config_ref: Literal["foundation-v1", "sim0-v1"]

    @model_validator(mode="after")
    def fixture_matches_configuration(self):
        expected = "foundation-v1" if self.fixture_ref == "s1a-foundation-v1" else "sim0-v1"
        if self.config_ref != expected:
            raise ValueError("Fixture and configuration version disagree")
        return self


class ActionParams(Contract):
    # Test input is an explicit driver movement request, never a teleport.
    request_vehicle_move: Literal["obj-car-02"] | None = None
    request_vehicle_departure: Literal["obj-car-01"] | None = None
    request_portal_attempt: Literal["obj-car-s3-u", "obj-car-s3-w"] | None = None
    observation_mode: Literal["normal", "occluded_vehicle", "missing_vehicle",
                              "occluded_pedestrian", "missing_pedestrian",
                              "delayed", "unavailable"] | None = None


class Control(Contract):
    action: Literal["start", "pause", "step", "reset", "replay"]
    action_params: ActionParams | None = None
