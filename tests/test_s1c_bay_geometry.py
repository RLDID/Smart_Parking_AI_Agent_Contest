"""Independent synthetic S1-c geometry and quality checks; no scenario truth enters product code."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import cos, pi, sin

import pytest
from shapely.affinity import translate
from shapely.geometry import Point, Polygon, box

from contracts.s1c_bay_geometry import BayGeometrySettings, MAX_FRESHNESS_MS
from simulator.s1c_bay_geometry import analyze_bay_footprint
from simulator.world import MAP


EPOCH = datetime(2026, 10, 1, tzinfo=timezone.utc)
EXPERIMENT = {"tolerance_m": 0.1, "max_uncertainty_m": 0.2, "freshness_ms": 1000}


def vehicle(x, y=26.5, heading=90, *, object_id="obj-car-02", uncertainty=0):
    return {"object_id": object_id, "object_type": "vehicle",
            "position": {"x": x, "y": y},
            "size": {"length_m": 4.6, "width_m": 1.8}, "heading_deg": heading,
            "quality": {"visibility": "visible", "uncertainty_m": uncertainty,
                        "missing_fields": []}}


def frame(*objects, t=0):
    stamp = (EPOCH + timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z")
    return {"facility_id": "fac-demo-01", "run_id": "run-s1c-test",
            "observation_id": f"obs-s1c-{t}", "map_version": "map-01-draft",
            "state_version": t//100, "sim_time_ms": t,
            "observed_at": stamp, "received_at": stamp,
            "coverage": "complete", "devices": [], "objects": list(objects)}


def analyze(snapshot, *, map_data=MAP, settings=EXPERIMENT, **overrides):
    args = {"object_id": "obj-car-02", "bay_id": "B01", "settings": settings,
            "expected_run_id": snapshot["run_id"],
            "expected_state_version": snapshot["state_version"],
            "current_sim_time_ms": snapshot["sim_time_ms"],
            "run_status": "paused", "recovery_required": False,
            "observation_ready": True,
            "now": EPOCH + timedelta(milliseconds=snapshot["sim_time_ms"])}
    args.update(overrides)
    return analyze_bay_footprint(map_data, snapshot, **args)


def test_centered_and_diagonal_inside_bay_with_independent_corner_oracle():
    centered = analyze(frame(vehicle(9.5)))
    assert centered.support_status == "supported"
    assert centered.geometry_relation == "within"
    assert centered.nominal_outside_area_m2 == pytest.approx(0)
    assert centered.nominal_max_depth_m == pytest.approx(0)
    diagonal = analyze(frame(vehicle(9.5, heading=94)))
    assert diagonal.geometry_relation == "within"
    # Independently compute the farthest x/y corner of a 94-degree body.
    angle = 94*pi/180
    x_half = 2.3*abs(cos(angle)) + .9*abs(sin(angle))
    y_half = 2.3*abs(sin(angle)) + .9*abs(cos(angle))
    assert 8 < 9.5-x_half < 9.5+x_half < 11
    assert 24 < 26.5-y_half < 26.5+y_half < 29
    assert diagonal.nominal_outside_area_m2 == pytest.approx(0)
    assert diagonal.adjacent_bay_overlap_m2["B02"].nominal_m2 == pytest.approx(0)


def test_b01_b02_split_measurement_is_single_observation_not_duration():
    snapshot = frame(vehicle(11, heading=90), t=200)
    split = analyze(snapshot)
    assert split.support_status == "supported"
    assert split.geometry_relation == "overlap"
    # Rectangle x=10.1..11.9, y=24.2..28.8; exactly 0.9*4.6 m2 per bay.
    assert split.nominal_outside_area_m2 == pytest.approx(4.14)
    assert split.nominal_max_depth_m == pytest.approx(.9)
    assert split.adjacent_bay_overlap_m2["B02"].nominal_m2 == pytest.approx(4.14)
    assert split.observation_id == "obs-s1c-200"
    assert split.object_id == "obj-car-02"
    assert split.bay_id == "B01"
    assert split.state_version == 2
    assert not any("duration" in key for key in split.model_dump())


def test_exact_boundary_and_supplied_tolerance_are_distinct():
    exact = analyze(frame(vehicle(10.1)))  # rightmost x=11 exactly
    assert exact.geometry_relation == "within"
    assert exact.nominal_outside_area_m2 == pytest.approx(0)
    small = analyze(frame(vehicle(10.2)), settings={**EXPERIMENT, "tolerance_m": .1})
    assert small.nominal_max_depth_m == pytest.approx(.1)
    assert small.geometry_relation == "within"
    assert analyze(frame(vehicle(10.2001))).geometry_relation == "overlap"
    assert analyze(frame(vehicle(10.2)), settings={**EXPERIMENT, "tolerance_m": .2}).geometry_relation == "within"


def test_left_and_right_bay_overlap_and_rotation_use_full_body():
    b02_left = analyze(frame(vehicle(11, heading=90)), bay_id="B02")
    assert b02_left.adjacent_bay_overlap_m2["B01"].nominal_m2 == pytest.approx(4.14)
    assert b02_left.adjacent_bay_overlap_m2["B03"].nominal_m2 == pytest.approx(0)
    b02_right = analyze(frame(vehicle(14, heading=90)), bay_id="B02")
    assert b02_right.adjacent_bay_overlap_m2["B03"].nominal_m2 == pytest.approx(4.14)
    rotated = analyze(frame(vehicle(9.5, heading=45)))
    assert rotated.geometry_relation == "overlap"
    assert rotated.nominal_outside_area_m2 > 0


def test_uncertainty_straddles_tolerance_without_claiming_overlap_or_within():
    snapshot = frame(vehicle(10.2, uncertainty=.05))
    result = analyze(snapshot)
    assert result.support_status == "supported"
    assert result.geometry_relation == "unknown"
    assert result.max_depth_lower_bound_m == pytest.approx(.05)
    assert .15 <= result.max_depth_upper_bound_m <= .151
    assert result.outside_area_lower_bound_m2 <= result.nominal_outside_area_m2
    assert result.nominal_outside_area_m2 <= result.outside_area_upper_bound_m2
    adjacent = result.adjacent_bay_overlap_m2["B02"]
    assert adjacent.lower_bound_m2 <= adjacent.nominal_m2 <= adjacent.upper_bound_m2
    # With the same uncertainty, deep split remains certain and a central
    # footprint remains within the supplied tolerance.
    assert analyze(frame(vehicle(11, uncertainty=.05))).geometry_relation == "overlap"
    assert analyze(frame(vehicle(9.5, uncertainty=.05))).geometry_relation == "within"
    assert analyze(frame(vehicle(9.5, heading=94, uncertainty=.1))).geometry_relation == "within"
    assert analyze(frame(vehicle(9.5, heading=94, uncertainty=.2))).geometry_relation == "within"
    zero_tolerance = {**EXPERIMENT, "tolerance_m": 0}
    assert analyze(frame(vehicle(9.5, heading=94, uncertainty=.1)),
                   settings=zero_tolerance).geometry_relation == "within"
    assert analyze(frame(vehicle(9.5, heading=94, uncertainty=.2)),
                   settings=zero_tolerance).geometry_relation == "unknown"


def test_dense_independent_translation_oracle_fits_reported_bounds():
    result = analyze(frame(vehicle(10.2, uncertainty=.2)))
    own = box(8, 24, 11, 29)
    neighbor = box(11, 24, 14, 29)
    # An independently constructed axis-aligned 4.6 x 1.8 m rectangle.
    nominal = Polygon(((9.3, 24.2), (11.1, 24.2), (11.1, 28.8), (9.3, 28.8)))
    for radial in range(11):
        radius = .2 * radial / 10
        for step in range(72):
            a = 2*pi*step/72
            body = translate(nominal, xoff=radius*cos(a), yoff=radius*sin(a))
            outside = body.difference(own).area
            adjacent = body.intersection(neighbor).area
            depth = max(own.distance(Point(*point))
                        for point in list(body.exterior.coords)[:-1])
            assert result.outside_area_lower_bound_m2 <= outside + 1e-9
            assert outside <= result.outside_area_upper_bound_m2 + 1e-9
            bounds = result.adjacent_bay_overlap_m2["B02"]
            assert bounds.lower_bound_m2 <= adjacent + 1e-9
            assert adjacent <= bounds.upper_bound_m2 + 1e-9
            assert result.max_depth_lower_bound_m <= depth + 1e-9
            assert depth <= result.max_depth_upper_bound_m + 1e-9


@pytest.mark.parametrize("fault", ["missing", "occluded", "partial", "null_position",
                                   "null_size", "null_heading", "uncertain", "duplicate"])
def test_target_unknown_cases_carry_no_numeric_claim(fault):
    snapshot = frame(vehicle(11))
    obj = snapshot["objects"][0]
    if fault == "missing":
        snapshot["objects"] = []
    elif fault == "occluded":
        obj["quality"]["visibility"] = "occluded"
    elif fault == "partial":
        snapshot["coverage"] = "partial"
    elif fault.startswith("null_"):
        field = fault[5:]
        if field == "heading":
            field = "heading_deg"
        obj[field] = None
        obj["quality"]["missing_fields"] = [field]
    elif fault == "uncertain":
        obj["quality"]["uncertainty_m"] = .21
    else:
        snapshot["objects"].append(deepcopy(obj))
    result = analyze(snapshot)
    assert result.support_status == "insufficient_data"
    assert result.geometry_relation == "unknown"
    assert result.nominal_outside_area_m2 is None
    assert result.adjacent_bay_overlap_m2 == {}


def test_unrelated_object_quality_does_not_change_selected_footprint():
    another = vehicle(20, object_id="other")
    another["quality"]["visibility"] = "occluded"
    result = analyze(frame(vehicle(11), another))
    assert result.geometry_relation == "overlap"


@pytest.mark.parametrize("change", ["bay", "lane", "walkway", "extra_field", "map_version"])
def test_any_same_version_map_mutation_or_new_version_is_unsupported(change):
    modified = deepcopy(MAP)
    if change == "bay":
        next(zone for zone in modified["zones"] if zone["zone_id"] == "B01")["polygon"][0]["x"] = 7.9
    elif change == "lane":
        modified["lanes"][0]["direction"] = "east"
    elif change == "walkway":
        next(zone for zone in modified["zones"] if zone["zone_id"] == "walkway")["polygon"][0]["x"] = 19.5
    elif change == "extra_field":
        modified["parking_bays"][0]["restriction"] = "unknown"
    else:
        modified["map_version"] = "map-02"
    result = analyze(frame(vehicle(11)), map_data=modified)
    assert result.support_status == "unsupported_geometry"
    assert result.geometry_relation == "unknown"
    assert result.nominal_outside_area_m2 is None


def test_observation_map_version_mismatch_is_unsupported():
    snapshot = frame(vehicle(11))
    snapshot["map_version"] = "map-other"
    result = analyze(snapshot)
    assert result.support_status == "unsupported_geometry"
    assert result.geometry_relation == "unknown"


@pytest.mark.parametrize("fault", ["run", "state", "future_sim", "stale_sim", "stale_wall",
                                   "delivery", "clock_order", "recovery", "not_ready", "stopped"])
def test_context_or_freshness_uncertainty_never_claims_overlap(fault):
    snapshot = frame(vehicle(11))
    kwargs = {}
    if fault == "run":
        kwargs["expected_run_id"] = "other-run"
    elif fault == "state":
        kwargs["expected_state_version"] = 1
    elif fault == "future_sim":
        kwargs["current_sim_time_ms"] = -1
    elif fault == "stale_sim":
        kwargs["current_sim_time_ms"] = 1001
    elif fault == "stale_wall":
        kwargs.update(run_status="running", now=EPOCH+timedelta(milliseconds=1001))
    elif fault == "delivery":
        snapshot["observed_at"] = (EPOCH-timedelta(milliseconds=1001)).isoformat().replace("+00:00", "Z")
    elif fault == "clock_order":
        snapshot["received_at"] = (EPOCH-timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    elif fault == "recovery":
        kwargs["recovery_required"] = True
    elif fault == "not_ready":
        kwargs["observation_ready"] = False
    else:
        kwargs["run_status"] = "stopped"
    if fault == "future_sim":
        with pytest.raises(ValueError):
            analyze(snapshot, **kwargs)
    else:
        result = analyze(snapshot, **kwargs)
        assert result.support_status == "insufficient_data"
        assert result.geometry_relation == "unknown"


def test_freshness_and_utc_boundaries_are_exact():
    snapshot = frame(vehicle(11))
    assert analyze(snapshot, run_status="running",
                   now=EPOCH+timedelta(milliseconds=1000)).geometry_relation == "overlap"
    assert analyze(snapshot, run_status="running",
                   now=EPOCH+timedelta(milliseconds=1000, microseconds=1)).geometry_relation == "unknown"
    assert analyze(snapshot, current_sim_time_ms=1000).geometry_relation == "overlap"
    assert analyze(snapshot, current_sim_time_ms=1001).geometry_relation == "unknown"
    with pytest.raises(ValueError, match="UTC"):
        analyze(snapshot, now=EPOCH.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UTC"):
        analyze(snapshot, now=EPOCH.astimezone(timezone(timedelta(hours=9))))


def test_settings_have_no_operating_defaults():
    with pytest.raises(Exception):
        BayGeometrySettings()
    with pytest.raises(Exception):
        BayGeometrySettings(tolerance_m=-.01, max_uncertainty_m=.1, freshness_ms=1000)


@pytest.mark.parametrize("field,value", [
    ("freshness_ms", 10**30),
    ("freshness_ms", MAX_FRESHNESS_MS + 1),
    ("tolerance_m", 1e308),
    ("max_uncertainty_m", 1e308),
])
def test_unsupported_numeric_settings_fail_before_timedelta_or_geos(field, value):
    settings = {**EXPERIMENT, field: value}
    with pytest.raises(ValueError):
        analyze(frame(vehicle(11)), settings=settings)
    assert BayGeometrySettings(**{**EXPERIMENT,
                                  "freshness_ms": MAX_FRESHNESS_MS}).freshness_ms == MAX_FRESHNESS_MS
    assert analyze(frame(vehicle(11)),
                   settings={**EXPERIMENT,
                             "freshness_ms": MAX_FRESHNESS_MS}).geometry_relation == "overlap"


@pytest.mark.parametrize("fault", ["position", "length", "width", "tiny_body",
                                   "uncertainty"])
def test_extreme_observed_geometry_returns_unsupported_unknown(fault):
    snapshot = frame(vehicle(11))
    obj = snapshot["objects"][0]
    if fault == "position":
        obj["position"]["x"] = 1e308
    elif fault == "length":
        obj["size"]["length_m"] = 1e308
    elif fault == "width":
        obj["size"]["width_m"] = 1e308
    elif fault == "tiny_body":
        obj["size"]["width_m"] = 1e-308
    else:
        obj["quality"]["uncertainty_m"] = 1e308
    result = analyze(snapshot)
    assert result.support_status == "unsupported_geometry"
    assert result.geometry_relation == "unknown"
    assert "numeric_range_exceeded" in result.reasons
    assert result.nominal_outside_area_m2 is None
    assert result.adjacent_bay_overlap_m2 == {}


def test_huge_caller_sim_clock_fails_explicitly_before_result_serialization():
    with pytest.raises(ValueError, match="signed-64-bit"):
        analyze(frame(vehicle(11)), current_sim_time_ms=10**30)
