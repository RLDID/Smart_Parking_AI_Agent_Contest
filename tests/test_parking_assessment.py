"""Observation-only parking duration probes with independent time/shape cases."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
import pytest

from contracts.parking_assessment import ParkingAssessmentSettings
from simulator.parking_assessment import assess_parking
from simulator.world import MAP

EPOCH = datetime(2026, 10, 1, tzinfo=timezone.utc)
SETTINGS = {
    "geometry": {"tolerance_m": .1, "max_uncertainty_m": .2, "freshness_ms": 1000},
    "stationary_ms": 2000, "position_tolerance_m": .1, "heading_tolerance_deg": 2,
    "maneuver_grace_ms": 3000, "intrusion_hold_ms": 1500,
    "max_sample_gap_ms": 400, "history_limit": 64,
}


def frame(t, x=11, heading=90, uncertainty=0):
    stamp = (EPOCH+timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z")
    return {"facility_id": "fac-demo-01", "run_id": "run-parking",
            "observation_id": f"obs-{t}", "map_version": "map-01-draft",
            "state_version": t//100, "sim_time_ms": t, "observed_at": stamp,
            "received_at": stamp, "coverage": "complete", "devices": [],
            "objects": [{"object_id": "car", "object_type": "vehicle",
                         "position": {"x": x, "y": 26.5},
                         "size": {"length_m": 4.6, "width_m": 1.8},
                         "heading_deg": heading,
                         "quality": {"visibility": "visible", "uncertainty_m": uncertainty,
                                     "missing_fields": []}}]}


def evaluate(frames, *, settings=None, map_data=None, **overrides):
    last = frames[-1]
    kwargs = dict(object_id="car", bay_id="B01", settings=settings or SETTINGS,
                  expected_run_id="run-parking", expected_state_version=last["state_version"],
                  current_sim_time_ms=last["sim_time_ms"], run_status="paused",
                  recovery_required=False, observation_ready=True,
                  now=EPOCH+timedelta(milliseconds=last["sim_time_ms"]))
    kwargs.update(overrides)
    return assess_parking(MAP if map_data is None else map_data, frames, **kwargs)


def series(end=4000, **kwargs):
    return [frame(t, **kwargs) for t in range(0, end+1, 200)]


def test_contiguous_stationary_overlap_is_only_a_persistent_feature():
    result = evaluate(series())
    assert result.support_status == "supported"
    assert result.motion_state == "stationary_candidate"
    assert result.stationary_duration_ms == 4000
    assert result.intrusion_state == "persistent_candidate"
    assert result.intrusion_duration_ms == 4000
    assert result.geometry.nominal_outside_area_m2 == pytest.approx(.9*4.6)
    assert not any(key in result.model_dump() for key in ("violation", "parking_complete", "resolved"))
    assert result.applied_settings.geometry.tolerance_m == .1


def test_grace_and_hold_are_explicit_distinct_gates():
    assert evaluate(series(2000)).motion_state == "stationary_candidate"
    assert evaluate(series(2000)).intrusion_state == "transient_or_maneuver"
    assert evaluate(series(3000)).intrusion_state == "persistent_candidate"
    stricter = {**SETTINGS, "intrusion_hold_ms": 5000}
    assert evaluate(series(4000), settings=stricter).intrusion_state == "transient_or_maneuver"
    no_grace = {**SETTINGS, "maneuver_grace_ms": 0}
    assert evaluate(series(2000), settings=no_grace).intrusion_state == "persistent_candidate"


def test_diagonal_inside_is_not_an_intrusion_even_after_long_stop():
    result = evaluate(series(x=9.5, heading=94))
    assert result.motion_state == "stationary_candidate"
    assert result.intrusion_state == "within_tolerance"
    assert result.geometry.nominal_outside_area_m2 == pytest.approx(0)


def test_time_inside_the_bay_never_consumes_grace_after_new_overlap():
    settings = {**SETTINGS, "stationary_ms": 1000, "position_tolerance_m": .3,
                "maneuver_grace_ms": 3000, "intrusion_hold_ms": 200}
    frames = series(3000, x=10.05)
    frames.extend(frame(t, x=10.25) for t in (3200, 3400))
    result = evaluate(frames, settings=settings)
    assert result.motion_state == "stationary_candidate"
    assert result.stationary_duration_ms == 3400
    assert result.intrusion_duration_ms == 200
    assert result.intrusion_state == "transient_or_maneuver"
    frames.extend(frame(t, x=10.25) for t in range(3600, 6201, 200))
    result = evaluate(frames, settings=settings)
    assert result.intrusion_duration_ms == 3000
    assert result.intrusion_state == "persistent_candidate"


def test_slow_drift_does_not_accumulate_a_stop_from_small_adjacent_steps():
    frames = [frame(t, x=10.6+t*.0001) for t in range(0, 4001, 200)]
    result = evaluate(frames)
    assert result.motion_state == "moving"
    assert result.stationary_duration_ms <= 1000
    assert result.intrusion_state != "persistent_candidate"


def test_movement_then_stop_starts_a_new_hold():
    frames = [frame(t, x=10.5+t*.00025) for t in range(0, 2000, 200)]
    frames.extend(frame(t, x=11) for t in range(2000, 4001, 200))
    result = evaluate(frames)
    assert result.motion_state == "stationary_candidate"
    assert 2000 <= result.stationary_duration_ms < 3000
    assert result.intrusion_state == "transient_or_maneuver"


def test_heading_wraparound_uses_circular_distance():
    frames = [frame(t, heading=359 if t % 400 == 0 else 1) for t in range(0, 4001, 200)]
    result = evaluate(frames)
    assert result.motion_state == "stationary_candidate"
    frames[-1]["objects"][0]["heading_deg"] = 4
    assert evaluate(frames).motion_state == "moving"


def test_position_uncertainty_cannot_be_washed_into_stationarity():
    result = evaluate(series(uncertainty=.1))
    assert result.motion_state == "unknown"
    assert result.stationary_duration_ms is None
    assert result.intrusion_state != "persistent_candidate"


def test_uncertain_intrusion_boundary_stays_unknown():
    result = evaluate(series(x=10.2, uncertainty=.02))
    assert result.geometry.geometry_relation == "unknown"
    assert result.intrusion_state == "unknown"


@pytest.mark.parametrize("mode", ["missing", "occluded", "partial", "dimension"])
def test_history_quality_break_never_counts_the_old_hold(mode):
    frames = series()
    changed = frames[-3]
    if mode == "missing":
        changed["objects"] = []
    elif mode == "occluded":
        changed["objects"][0]["quality"]["visibility"] = "occluded"
    elif mode == "partial":
        changed["coverage"] = "partial"
    else:
        changed["objects"][0]["size"]["length_m"] = 4.5
    result = evaluate(frames)
    assert result.intrusion_state != "persistent_candidate"
    assert result.stationary_duration_ms is None or result.stationary_duration_ms < 2000


def test_gap_duplicate_and_bounded_history_do_not_invent_duration():
    frames = [frame(0), frame(200), frame(4000), frame(4200)]
    assert evaluate(frames).stationary_duration_ms == 200
    original = series()
    repeated = [item for item in original for _ in range(2)]
    result = evaluate(repeated)
    assert result.stationary_duration_ms == 4000
    assert len(result.observation_ids) == len(original)
    settings = {**SETTINGS, "history_limit": 3}
    assert evaluate(original, settings=settings).stationary_duration_ms == 400


@pytest.mark.parametrize("change", ["run", "map", "reverse", "same_time", "version", "duplicate", "conflict", "old_retry", "future_wall", "backward_wall"])
def test_malformed_or_mixed_history_fails_closed(change):
    frames = series()
    if change == "run":
        frames[2]["run_id"] = "other"
    elif change == "map":
        frames[2]["map_version"] = "other"
    elif change == "reverse":
        frames[2], frames[3] = frames[3], frames[2]
    elif change == "same_time":
        frames[2]["sim_time_ms"] = frames[1]["sim_time_ms"]
    elif change == "version":
        frames[2]["state_version"] = frames[1]["state_version"]
    elif change == "duplicate":
        frames[2]["objects"].append(deepcopy(frames[2]["objects"][0]))
    elif change == "conflict":
        frames[2]["observation_id"] = frames[1]["observation_id"]
    elif change == "old_retry":
        frames.append(deepcopy(frames[0]))
    elif change == "future_wall":
        frames[2]["received_at"] = (EPOCH+timedelta(days=1)).isoformat()
    else:
        frames[2]["observed_at"] = frames[0]["observed_at"]
    result = evaluate(frames)
    assert result.support_status == "insufficient_data"
    assert result.motion_state == "unknown" and result.intrusion_state == "unknown"


@pytest.mark.parametrize("overrides", [
    {"recovery_required": True}, {"observation_ready": False},
    {"expected_run_id": "other"}, {"expected_state_version": 999},
    {"current_sim_time_ms": 6000},
    {"run_status": "running", "now": EPOCH+timedelta(seconds=10)},
])
def test_latest_context_and_freshness_gate_analysis(overrides):
    result = evaluate(series(), **overrides)
    assert result.support_status == "insufficient_data"
    assert result.intrusion_state == "unknown"


def test_last_frame_missing_is_unknown_not_bay_recovered():
    frames = series()
    frames[-1]["objects"] = []
    result = evaluate(frames)
    assert result.support_status == "insufficient_data"
    assert result.intrusion_state == "unknown"


def test_maps_inputs_and_settings_are_bounded_and_unmodified():
    frames = series()
    backup = deepcopy(frames)
    evaluate(frames)
    assert frames == backup
    modified_map = deepcopy(MAP)
    modified_map["bounds"]["max_x"] = 41
    assert evaluate(frames, map_data=modified_map).support_status == "unsupported_geometry"
    with pytest.raises(ValueError):
        evaluate([frame(0)]*513)
    with pytest.raises(ValidationError):
        ParkingAssessmentSettings.model_validate({**SETTINGS, "stationary_ms": True})
    with pytest.raises(ValidationError):
        ParkingAssessmentSettings.model_validate({**SETTINGS, "position_tolerance_m": float("inf")})
    with pytest.raises(ValidationError):
        ParkingAssessmentSettings.model_validate({})
