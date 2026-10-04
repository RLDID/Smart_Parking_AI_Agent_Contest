"""Bounded, synthetic S2 replay input and output; never an Agent tool contract.

``execution_record`` describes what happened in this synthetic run, without
expected answers or success grading. Only ``public_observations`` from the
simulator module may be passed to product observation consumers.

The default V/P coordinates, body sizes, speeds, and 200 ms observation cycle
are proposal fixture values, not adopted alarm thresholds or site policy.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Contract, Observation, Position, UtcTimestamp


class OcclusionWindow(Contract):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Occlusion window must have positive duration")
        return self


class S2MotionInput(Contract):
    """Explicit fixture controls, restricted to the current straight S2 map paths."""

    facility_id: Literal["fac-demo-01"] = "fac-demo-01"
    map_version: Literal["map-01-draft"] = "map-01-draft"
    initial_utc: UtcTimestamp
    seed: int = Field(ge=0, le=2**31 - 1, strict=True)
    tick_ms: Literal[50, 100, 200] = 100
    observation_ms: int = Field(default=200, ge=50, le=1000, strict=True)
    duration_ms: int = Field(default=6000, ge=200, le=12000, strict=True)
    vehicle_start: Position = Field(default_factory=lambda: Position(x=16, y=20))
    pedestrian_start: Position = Field(default_factory=lambda: Position(x=22, y=16.4))
    vehicle_speed_mps: float = Field(default=2, gt=0, le=5)
    pedestrian_speed_mps: float = Field(default=1.2, gt=0, le=3)
    vehicle_stop_x: float = Field(default=29.7, ge=10.3, le=29.7)
    pedestrian_stop_y: float = Field(default=29.7, ge=10.3, le=29.7)
    pedestrian_start_delay_ms: int = Field(default=0, ge=0, le=12000, strict=True)
    brake_command_at_ms: int | None = Field(default=None, ge=0, le=12000, strict=True)
    brake_response_delay_ms: int = Field(default=0, ge=0, le=12000, strict=True)
    pedestrian_occlusion: OcclusionWindow | None = None
    observation_jitter_m: float = Field(default=0, ge=0, le=0.1)

    @model_validator(mode="after")
    def bounded_schedule(self):
        if self.observation_ms % self.tick_ms or self.duration_ms % self.observation_ms:
            raise ValueError("Observation/duration must align with ticks")
        if self.duration_ms // self.observation_ms > 60:
            raise ValueError("Replay exceeds the 61-observation bound")
        try:
            datetime.fromisoformat(self.initial_utc.replace("Z", "+00:00")) + timedelta(milliseconds=self.duration_ms)
        except OverflowError as exc:
            raise ValueError("Replay exceeds representable UTC time") from exc
        if self.pedestrian_start_delay_ms > self.duration_ms:
            raise ValueError("Pedestrian departure exceeds replay duration")
        if self.brake_command_at_ms is None and self.brake_response_delay_ms:
            raise ValueError("Response delay needs an explicit brake command")
        if self.brake_command_at_ms is not None and self.brake_command_at_ms > self.duration_ms:
            raise ValueError("Brake command exceeds replay duration")
        if self.pedestrian_occlusion and self.pedestrian_occlusion.end_ms > self.duration_ms:
            raise ValueError("Occlusion exceeds replay duration")
        if self.vehicle_stop_x < self.vehicle_start.x or self.pedestrian_stop_y < self.pedestrian_start.y:
            raise ValueError("Straight paths cannot reverse")
        return self


class S2ContactRecord(Contract):
    """Physical contact observed in the synthetic environment execution."""

    sim_time_ms: float = Field(ge=0)
    vehicle_position: Position
    pedestrian_position: Position
    contact_kind: Literal["vehicle_pedestrian"] = "vehicle_pedestrian"


class S2ExecutionRecord(Contract):
    """Private replay record, not a predicted outcome or evaluation answer."""

    contact: S2ContactRecord | None
    final_vehicle_position: Position
    final_pedestrian_position: Position
    stopped_on_contact: bool


class S2MotionResult(Contract):
    replay_kind: Literal["synthetic_replay"] = "synthetic_replay"
    observations: list[Observation] = Field(max_length=61)
    execution_record: S2ExecutionRecord
