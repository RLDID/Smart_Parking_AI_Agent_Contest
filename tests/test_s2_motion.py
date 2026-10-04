"""Synthetic S2 motion tests; expected outcomes stay in tests, never observations."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from contracts.models import Observation, Position
from contracts.s2_motion import S2MotionInput
from simulator.s2_motion import SUPPORTED_MAP_DIGEST, public_observations, simulate_s2
from simulator.world import MAP, digest


def fixture(**overrides):
    return S2MotionInput(**{"initial_utc": "2026-09-30T01:00:00Z", "seed": 73,
                            **overrides})


@pytest.mark.parametrize("tick", [50, 100, 200])
def test_nominal_contact_is_exactly_before_three_second_center_crossing(tick):
    result = simulate_s2(fixture(tick_ms=tick))
    event = result.execution_record.contact
    assert event is not None
    assert event.sim_time_ms == pytest.approx(2000, abs=1e-8)
    assert event.vehicle_position.x == pytest.approx(20)
    assert event.pedestrian_position.y == pytest.approx(18.8)
    assert result.execution_record.stopped_on_contact is True
    assert result.execution_record.final_vehicle_position == event.vehicle_position
    assert result.execution_record.final_pedestrian_position == event.pedestrian_position
    assert [o.sim_time_ms for o in result.observations] == list(range(0, 6001, 200))


@pytest.mark.parametrize("tick", [50, 100, 200])
def test_contact_inside_tick_is_not_rounded_to_next_tick(tick):
    result = simulate_s2(fixture(tick_ms=tick, pedestrian_start=Position(x=22, y=16.37)))
    assert result.execution_record.contact is not None
    assert result.execution_record.contact.sim_time_ms == pytest.approx(2025, abs=1e-7)


def test_spatially_crossing_paths_at_different_times_do_not_contact():
    result = simulate_s2(fixture(duration_ms=8000, pedestrian_start_delay_ms=2400))
    assert result.execution_record.contact is None
    assert result.execution_record.stopped_on_contact is False
    assert result.execution_record.final_vehicle_position.x == pytest.approx(29.7)
    assert result.execution_record.final_pedestrian_position.y > 20


def test_explicit_departure_and_separate_path_are_contact_free():
    # Vehicle has left the shared x before a delayed pedestrian crosses.
    delayed = simulate_s2(fixture(duration_ms=8000, pedestrian_start_delay_ms=2400,
                                  vehicle_stop_x=27))
    # Pedestrian's explicitly limited path remains south of the vehicle body.
    separated = simulate_s2(fixture(pedestrian_stop_y=18.4))
    assert delayed.execution_record.contact is None
    assert separated.execution_record.contact is None


def test_brake_command_delay_and_nonresponse_are_explicit_fixture_inputs():
    stopped = simulate_s2(fixture(brake_command_at_ms=500, brake_response_delay_ms=100))
    late = simulate_s2(fixture(brake_command_at_ms=500, brake_response_delay_ms=2500))
    no_response = simulate_s2(fixture())
    assert stopped.execution_record.contact is None
    assert stopped.execution_record.final_vehicle_position.x == pytest.approx(17.2)
    assert late.execution_record.contact.sim_time_ms == pytest.approx(2000)
    assert no_response.execution_record.contact.sim_time_ms == pytest.approx(2000)


def test_occlusion_changes_public_quality_but_not_physical_contact():
    config = fixture(pedestrian_occlusion={"start_ms": 1800, "end_ms": 2400})
    result = simulate_s2(config)
    assert result.execution_record.contact.sim_time_ms == pytest.approx(2000)
    frame = next(o for o in public_observations(result) if o.sim_time_ms == 2000)
    assert frame.coverage == "partial"
    pedestrian = next(o for o in frame.objects if o.object_type == "pedestrian")
    assert pedestrian.position is None
    assert pedestrian.size is None
    assert pedestrian.heading_deg is None
    assert pedestrian.quality.visibility == "occluded"
    assert pedestrian.quality.missing_fields == ["position", "size", "heading_deg"]
    assert Observation.model_validate(frame.model_dump()) == frame
    assert next(o for o in result.observations if o.sim_time_ms == 2400).coverage == "complete"


def test_replay_is_deterministic_with_explicit_seed_and_synthetic_utc():
    config = fixture(observation_jitter_m=0.1)
    first = simulate_s2(config)
    second = simulate_s2(config)
    changed = simulate_s2(fixture(seed=74, observation_jitter_m=0.1))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump() != changed.model_dump()
    assert first.replay_kind == "synthetic_replay"
    assert first.observations[0].observed_at == "2026-09-30T01:00:00Z"
    assert first.observations[1].observed_at == "2026-09-30T01:00:00.200000Z"
    assert first.observations[1].received_at == first.observations[1].observed_at
    assert all(o.quality.uncertainty_m == 0.1
               for frame in first.observations for o in frame.objects)


def test_public_projection_has_no_fixture_controls_record_or_mutable_reference():
    result = simulate_s2(fixture())
    public = public_observations(result)
    encoded = json.dumps([o.model_dump() for o in public], sort_keys=True)
    for private in ("execution_record", "contact_kind", "seed", "brake_command_at_ms",
                    "pedestrian_start_delay_ms", "vehicle_stop_x", "initial_utc"):
        assert private not in encoded
    assert public[0] is not result.observations[0]
    assert public[0].objects[0] is not result.observations[0].objects[0]
    public[0].objects[0].position.x = -1
    assert result.observations[0].objects[0].position.x == 16


@pytest.mark.parametrize("change", [
    {"tick_ms": 25}, {"observation_ms": 150}, {"duration_ms": 12500},
    {"duration_ms": 250}, {"pedestrian_start_delay_ms": 7000},
    {"observation_ms": 50, "duration_ms": 4000},
    {"initial_utc": "9999-12-31T23:59:59Z"},
    {"brake_response_delay_ms": 100}, {"brake_command_at_ms": 7000},
    {"observation_jitter_m": float("nan")}, {"vehicle_speed_mps": float("inf")},
    {"seed": -1}, {"pedestrian_occlusion": {"start_ms": 300, "end_ms": 200}},
    {"pedestrian_occlusion": {"start_ms": 5000, "end_ms": 7000}},
    {"unexpected_control": "agent-chosen"},
])
def test_contract_rejects_unbounded_or_unsupported_inputs(change):
    with pytest.raises(ValidationError):
        fixture(**change)


@pytest.mark.parametrize("change", [
    {"vehicle_start": Position(x=10, y=20)},
    {"vehicle_start": Position(x=16, y=23.2)},
    {"pedestrian_start": Position(x=20.2, y=16.4)},
    {"pedestrian_start": Position(x=22, y=10.2)},
    {"vehicle_start": Position(x=21, y=20), "pedestrian_start": Position(x=22, y=20)},
])
def test_support_boundary_and_initial_overlap_fail_loudly(change):
    with pytest.raises(ValueError):
        simulate_s2(fixture(**change))


def test_normal_observations_have_only_current_position_and_schema_fields():
    result = simulate_s2(fixture())
    public = public_observations(result)
    assert len(public) == 31
    for index, frame in enumerate(public):
        assert frame.state_version == frame.sim_time_ms // 100
        assert frame.sim_time_ms == index * 200
        assert frame.coverage == "complete"
        assert frame.devices == []
        assert frame.object_events == []
        assert all(obj.position is not None for obj in frame.objects)
        assert Observation.model_validate(deepcopy(frame.model_dump())) == frame


@pytest.mark.parametrize("section,index,key,replacement", [
    ("lanes", 0, "direction", "east"),
    ("zones", 0, "type", "pedestrian"),
    ("portals", 0, "direction", "exit"),
    ("gates", 0, "zone_id", "aisle-west"),
])
def test_same_version_map_semantic_changes_are_rejected(section, index, key, replacement):
    assert digest(MAP) == SUPPORTED_MAP_DIGEST
    entry = MAP[section][index]
    existed = key in entry
    original = entry.get(key)
    try:
        entry[key] = replacement
        assert MAP["map_version"] == "map-01-draft"
        with pytest.raises(ValueError, match="public map disagree"):
            simulate_s2(fixture())
    finally:
        if existed:
            entry[key] = original
        else:
            del entry[key]
    assert digest(MAP) == SUPPORTED_MAP_DIGEST
