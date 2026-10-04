"""Synthetic geometry experiments only; S1-b operating values remain undecided."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import cos, pi, sin

import pytest
from shapely.geometry import Point
from shapely.prepared import prep

from contracts.exit_geometry import ExitGeometrySettings
from simulator.exit_geometry import _candidate_sweep, _outer_buffer, analyze_exit_candidate
from simulator.world import MAP


EPOCH = datetime(2026, 10, 1, tzinfo=timezone.utc)
# Test-only values, explicitly supplied on every analysis; not operating policy.
EXPERIMENT = {"side_clearance_m": 0.05, "max_observation_uncertainty_m": 0.1,
              "freshness_ms": 1000, "max_discretization_error_m": 0.02}


def vehicle(object_id, x, y, heading=90, *, length=4.6, width=1.8, uncertainty=0):
    return {"object_id": object_id, "object_type": "vehicle", "position": {"x": x, "y": y},
            "size": {"length_m": length, "width_m": width}, "heading_deg": heading,
            "quality": {"visibility": "visible", "uncertainty_m": uncertainty,
                        "missing_fields": []}}


def frame(*obstacles, t=0):
    at = (EPOCH + timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z")
    return {"facility_id": "fac-demo-01", "run_id": "run-synthetic-s1b",
            "observation_id": f"obs-synthetic-{t}", "map_version": "map-01-draft",
            "state_version": t//100, "sim_time_ms": t, "observed_at": at, "received_at": at,
            "coverage": "complete", "devices": [],
            "objects": [vehicle("obj-car-01", 9.5, 26.5), *obstacles]}


def check(snapshot, *, map_data=MAP, settings=EXPERIMENT, **kwargs):
    args = {"current_sim_time_ms": snapshot["sim_time_ms"], "run_status": "paused",
            "now": EPOCH + timedelta(milliseconds=snapshot["sim_time_ms"])}
    args.update(kwargs)
    return analyze_exit_candidate(map_data, snapshot, settings=settings, **args)


def test_b_blocks_reverse_and_moving_b_restores_candidate_without_moving_a():
    blocked = check(frame(vehicle("obj-car-02", 9.5, 21.7, 0)))
    assert blocked.support_status == "supported"
    assert blocked.candidate_passage == "blocked"
    assert blocked.collision_object_ids == ["obj-car-02"]
    clear = check(frame(vehicle("obj-car-02", 4, 30, 90), t=200))
    assert clear.support_status == "supported"
    assert clear.candidate_passage == "clear"
    assert clear.collision_object_ids == []
    assert clear.observation_ids == ["obs-synthetic-200"]
    assert clear.arc_discretization_bound_m <= EXPERIMENT["max_discretization_error_m"]


def test_adjacent_arc_obstacle_and_body_corner_obstacle_are_seen():
    arc = check(frame(vehicle("neighbor", 12.5, 23, 0)))
    assert arc.candidate_passage == "blocked"
    assert arc.collision_object_ids == ["neighbor"]
    # B center is outside the nominal reverse centerline, but its corner
    # reaches the swept A body.
    corner = check(frame(vehicle("corner", 11.9, 21.7, 0)))
    assert corner.candidate_passage == "blocked"
    assert corner.collision_object_ids == ["corner"]


def test_independent_dense_material_point_oracle_for_arc_inflation():
    # Independent corner equations at 10,001 angles include values between
    # the sweep samples. This checks the claimed conservative envelope.
    sweep, bound = _candidate_sweep(ExitGeometrySettings(**EXPERIMENT))
    prepared = prep(sweep)
    assert bound <= EXPERIMENT["max_discretization_error_m"]
    for index in range(10_001):
        a = pi + pi*index/(2*10_000)
        x, y = 13.5+4*cos(a), 24+4*sin(a)
        heading = a-pi/2
        for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            dx, dy = u*2.3, v*.9
            assert prepared.covers(Point(x + dx*cos(heading)-dy*sin(heading),
                                         y + dx*sin(heading)+dy*cos(heading)))
    # Collision claims also include off-grid boundary points.
    for deg in (181.37, 207.83, 249.19, 268.63):
        a = deg*pi/180
        x, y = 13.5+4*cos(a), 24+4*sin(a)
        heading = a-pi/2
        dx = 2.3*cos(heading) - .9*sin(heading)
        dy = 2.3*sin(heading) + .9*cos(heading)
        marker = vehicle("marker", x+dx, y+dy, 0, length=.001, width=.001)
        assert check(frame(marker)).collision_object_ids == ["marker"]


def test_polygonal_buffer_encloses_circle_at_all_angular_boundaries():
    # An ordinary Shapely buffer is an inscribed polygon, so a point on the
    # requested circle halfway between its vertices would otherwise lie out.
    radius = .37
    conservative = prep(_outer_buffer(Point(0, 0), radius))
    for index in range(10_001):
        angle = 2*pi*index/10_000
        assert conservative.covers(Point(radius*cos(angle), radius*sin(angle)))


@pytest.mark.parametrize("fault", ["missing", "occluded", "unavailable", "delayed", "uncertain", "duplicate"])
def test_incomplete_or_stale_observation_never_claims_clear(fault):
    snapshot = frame(vehicle("obj-car-02", 4, 30, 90))
    kwargs = {}
    if fault == "missing":
        snapshot["objects"] = snapshot["objects"][:1]
        snapshot["coverage"] = "partial"
    elif fault == "occluded":
        obj = snapshot["objects"][1]
        obj.update(position=None, size=None, heading_deg=None)
        obj["quality"] = {"visibility": "occluded", "uncertainty_m": None,
                          "missing_fields": ["position", "size", "heading_deg"]}
        snapshot["coverage"] = "partial"
    elif fault == "unavailable":
        snapshot.update(coverage="unavailable", objects=[])
    elif fault == "delayed":
        kwargs["current_sim_time_ms"] = 1200
    elif fault == "uncertain":
        snapshot["objects"][1]["quality"]["uncertainty_m"] = .2
    else:
        snapshot["objects"].append(deepcopy(snapshot["objects"][1]))
    result = check(snapshot, **kwargs)
    assert result.support_status == "insufficient_data"
    assert result.candidate_passage == "unknown"


def test_observation_delivery_delay_and_exact_freshness_boundary():
    snapshot = frame()
    received = EPOCH
    snapshot["observed_at"] = (received-timedelta(milliseconds=1000)).isoformat().replace("+00:00", "Z")
    assert check(snapshot).candidate_passage == "clear"
    snapshot["observed_at"] = (received-timedelta(milliseconds=1000, microseconds=1)).isoformat().replace("+00:00", "Z")
    result = check(snapshot)
    assert result.support_status == "insufficient_data"
    assert "stale_observation" in result.reasons
    assert result.candidate_passage == "unknown"


def test_running_wall_age_uses_exact_timedelta_not_millisecond_truncation():
    snapshot = frame()
    boundary = EPOCH + timedelta(milliseconds=1000)
    assert check(snapshot, run_status="running", now=boundary).candidate_passage == "clear"
    just_over = boundary + timedelta(microseconds=1)
    result = check(snapshot, run_status="running", now=just_over)
    assert result.support_status == "insufficient_data"
    assert "stale_observation" in result.reasons


def test_invalid_observation_clock_is_never_clear():
    snapshot = frame()
    snapshot["received_at"] = (EPOCH-timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    result = check(snapshot)
    assert result.support_status == "insufficient_data"
    assert "invalid_observation_clock" in result.reasons


def test_supported_map_and_pose_are_explicitly_bounded():
    changed = deepcopy(MAP)
    changed["portals"][0]["direction"] = "entry"
    assert check(frame(), map_data=changed).support_status == "unsupported_geometry"
    same_version_lane_change = deepcopy(MAP)
    for lane in same_version_lane_change["lanes"]:
        if lane["zone_id"] == "aisle-west":
            lane["direction"] = "east"
    result = check(frame(), map_data=same_version_lane_change)
    assert result.support_status == "unsupported_geometry"
    assert result.candidate_passage == "unknown"
    same_version_bay_change = deepcopy(MAP)
    same_version_bay_change["parking_bays"][0]["restriction"] = "no_exit"
    assert check(frame(), map_data=same_version_bay_change).support_status == "unsupported_geometry"
    same_version_walkway_change = deepcopy(MAP)
    for zone in same_version_walkway_change["zones"]:
        if zone["zone_id"] == "walkway":
            zone["polygon"] = [{"x": 8, "y": 22}, {"x": 12, "y": 22},
                               {"x": 12, "y": 28}, {"x": 8, "y": 28}]
    result = check(frame(), map_data=same_version_walkway_change)
    assert result.support_status == "unsupported_geometry"
    assert result.candidate_passage == "unknown"
    moved_a = frame()
    moved_a["objects"][0]["position"]["x"] = 9.6
    assert check(moved_a).support_status == "unsupported_geometry"
    unsupported_size = frame()
    unsupported_size["objects"][0]["size"]["length_m"] = 5
    assert check(unsupported_size).support_status == "unsupported_geometry"


def test_map_vehicle_area_shrink_or_side_clearance_blocks_candidate():
    map_changed = deepcopy(MAP)
    for zone in map_changed["zones"]:
        if zone["zone_id"] == "B02":
            zone["polygon"][1]["x"] = 13.8
    assert check(frame(), map_data=map_changed).support_status == "unsupported_geometry"
    # A locally supplied experimental clearance too large for the bay produces
    # a conservative blocked outcome, never an unsupported silent pass.
    huge_clearance = {**EXPERIMENT, "side_clearance_m": 1}
    result = check(frame(), settings=huge_clearance)
    assert result.candidate_passage == "blocked"
    assert "candidate_leaves_supported_vehicle_area" in result.reasons


def test_invalid_precision_refuses_instead_of_claiming_clear():
    tiny = {**EXPERIMENT, "max_discretization_error_m": .0001}
    result = check(frame(), settings=tiny)
    assert result.support_status == "unsupported_geometry"
    assert result.candidate_passage == "unknown"
    assert "resolution_exceeds_limit" in result.reasons
