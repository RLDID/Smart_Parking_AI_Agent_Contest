"""Observed approach projections use no S2 execution record or future controls."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from contracts.approach import ApproachSettings
from contracts.s2_motion import OcclusionWindow, S2MotionInput
from simulator.approach import analyze_approach
from simulator.s2_motion import public_observations, simulate_s2
from simulator.world import MAP


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
SETTINGS = ApproachSettings(horizon_ms=3000, margin_m=0.5,
                            freshness_ms=1000, max_sample_gap_ms=500,
                            max_uncertainty_m=0.2, max_speed_mps=5,
                            max_heading_change_deg=10)


def _frames(**changes):
    config = dict(initial_utc=START.isoformat().replace("+00:00", "Z"), seed=2,
                  duration_ms=1200)
    config.update(changes)
    return public_observations(simulate_s2(S2MotionInput(**config)))


def _analyze(frames, **changes):
    options = dict(current_sim_time_ms=frames[-1].sim_time_ms,
                   current_utc=START + timedelta(milliseconds=frames[-1].sim_time_ms),
                   recovery_required=False, settings=SETTINGS)
    options.update(changes)
    return analyze_approach(MAP, frames, **options)


def test_simultaneous_approach_is_candidate_from_public_history():
    frames = _frames()[:3]
    result = _analyze(frames)
    assert result.status == "risk_candidate"
    assert result.observation_ids == [f.observation_id for f in frames]
    assert result.candidates[0].vehicle_id == "obj-car-s2-v"
    assert result.candidates[0].pedestrian_id == "obj-person-s2-p"
    assert 0 < result.candidates[0].projected_first_proximity_ms < 3000


def test_time_separated_crossing_does_not_project_simultaneous_proximity():
    # P starts late enough that V crosses and departs the common coordinates.
    frames = _frames(pedestrian_start_delay_ms=1200)[:3]
    assert _analyze(frames).status == "clear_projection"


def test_departing_vehicle_and_parallel_motion_are_clear_projections():
    frames = _frames()[:3]
    for frame in frames:
        car = frame.objects[0]
        car.position.x = 16 - frame.sim_time_ms/1000*2
    assert _analyze(frames).status == "clear_projection"
    parallel = _frames()[:3]
    for frame in parallel:
        person = frame.objects[1]
        person.position.x = 22 + frame.sim_time_ms/1000*2
        person.position.y = 16.4
        person.heading_deg = 0
    assert _analyze(parallel).status == "clear_projection"


def test_uncertainty_can_turn_clear_projection_into_candidate():
    frames = _frames()[:3]
    for frame in frames:
        frame.objects[1].position.y -= 2.7
    assert _analyze(frames).status == "clear_projection"
    for frame in frames:
        for obj in frame.objects:
            obj.quality.uncertainty_m = 0.2
    assert _analyze(frames).status == "risk_candidate"


def test_occlusion_missing_actor_and_recovery_fail_closed():
    frames = _frames(pedestrian_occlusion=OcclusionWindow(start_ms=200, end_ms=800))[:3]
    assert _analyze(frames).status == "insufficient_data"
    assert "incomplete_coverage" in _analyze(frames).reasons
    clear_frames = _frames()[:3]
    assert "recovery_required" in _analyze(clear_frames, recovery_required=True).reasons
    clear_frames[-1].objects.pop()
    assert _analyze(clear_frames).status == "insufficient_data"


def test_order_run_map_and_clocks_fail_closed():
    frames = _frames()[:3]
    assert _analyze(frames[::-1]).status == "insufficient_data"
    other = deepcopy(frames)
    other[0].run_id = "different"
    assert "observation_context_mismatch" in _analyze(other).reasons
    assert "invalid_or_stale_current_clock" in _analyze(frames, current_sim_time_ms=0).reasons
    assert "stale_wall_clock" in _analyze(frames, current_utc=START + timedelta(seconds=4)).reasons
    for offset_ms in (1, 500):
        future_received = deepcopy(frames)
        future_received[-1].received_at = (START + timedelta(milliseconds=400+offset_ms)).isoformat().replace("+00:00", "Z")
        assert "invalid_or_stale_current_clock" in _analyze(future_received).reasons
        future_observed = deepcopy(frames)
        future_stamp = (START + timedelta(milliseconds=400+offset_ms)).isoformat().replace("+00:00", "Z")
        future_observed[-1].observed_at = future_stamp
        future_observed[-1].received_at = future_stamp
        assert "invalid_or_stale_current_clock" in _analyze(future_observed).reasons
    changed_map = deepcopy(MAP)
    changed_map["route_policy_version"] = "changed"
    assert analyze_approach(changed_map, frames,
                            current_sim_time_ms=400, current_utc=START+timedelta(milliseconds=400),
                            recovery_required=False, settings=SETTINGS).status == "unsupported_geometry"
    for invalid_map in (None, []):
        assert analyze_approach(invalid_map, frames,
                                current_sim_time_ms=400, current_utc=START+timedelta(milliseconds=400),
                                recovery_required=False, settings=SETTINGS).status == "unsupported_geometry"


def test_heading_change_speed_duplicate_and_numeric_bounds_fail_closed():
    frames = _frames()[:3]
    spun = deepcopy(frames)
    spun[-1].objects[0].heading_deg = 20
    assert "heading_change_exceeded" in _analyze(spun).reasons
    resized = deepcopy(frames)
    resized[-1].objects[1].size.width_m = 0.8
    assert "body_size_changed" in _analyze(resized).reasons
    fast = deepcopy(frames)
    fast[-1].objects[0].position.x += 20
    assert "speed_limit_exceeded" in _analyze(fast).reasons
    duplicated = deepcopy(frames)
    duplicated[-1].objects.append(deepcopy(duplicated[-1].objects[0]))
    assert "duplicate_or_excess_objects" in _analyze(duplicated).reasons
    out_of_range = deepcopy(frames)
    out_of_range[-1].objects[0].position.x = 1001
    assert "numeric_range_exceeded" in _analyze(out_of_range).reasons


def test_resource_bounds_and_invalid_inputs():
    frames = _frames()[:3]
    with pytest.raises(ValueError):
        _analyze(frames*22)
    with pytest.raises(ValueError):
        _analyze(frames, current_sim_time_ms=True)
    with pytest.raises(ValueError):
        _analyze(frames, current_utc=datetime(2026, 10, 1))
    with pytest.raises(Exception):
        _analyze(frames, settings={**SETTINGS.model_dump(), "margin_m": float("nan")})
