"""Pure S1-c footprint measurement from a public map and observation.

The relation is geometry under caller-supplied tolerance, not a parking
violation, driver request, manoeuvre-completion or incident decision.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import cos, isfinite, pi, radians, sin

from shapely.errors import GEOSException
from shapely.geometry import Point, Polygon
from pydantic import ValidationError

from contracts.models import Observation
from contracts.s1c_bay_geometry import (BayGeometryAnalysis, BayGeometrySettings,
                                        BayOverlapArea, MAX_GEOMETRY_DISTANCE_M)


FACILITY = "fac-demo-01"
MAP_VERSION = "map-01-draft"
# Entire public map-01-draft, including lanes, bays, walkway and extensions.
SUPPORTED_MAP_DIGEST = "48761815f917c4dd68a1a6e53918db1b66b1e7431c649a84bc811380a47dd049"
BUFFER_QUAD_SEGS = 16
# Floating-point geometry is evaluated only near this 40 x 32 m sample map.
# These broad resource/precision caps are not permitted parking dimensions.
MAX_ABS_COORD_M = 1000.0
MIN_BODY_DIMENSION_M = 1e-4
MAX_BODY_DIMENSION_M = MAX_GEOMETRY_DISTANCE_M
MAX_SIM_TIME_MS = 2**63 - 1
ASSUMPTIONS = [
    "Only the pinned public map-01-draft and its declared parking-bay polygons are supported.",
    "expected_state_version is the caller's current public snapshot version, not the simulator's applied-state version.",
    "Observed size and heading are treated as exact; uncertainty_m bounds an isotropic center-position error in metres.",
    "Area bounds use a conservative outer footprint and an eroded inner footprint for all allowed translations.",
    "A paused run freezes simulation time; wall-age expiry is checked while running, and delivery age is always checked.",
    "Within/overlap describes only whether the footprint's maximum distance outside this bay is at most/above the caller's tolerance.",
    "Adjacent-bay area overlap is geometry only; it does not prove another driver's use is impeded.",
    "Implementation numeric range: |coordinate| <= 1000 m, body dimension 0.0001..100 m, and uncertainty/tolerance <= 100 m; these are not operating rules.",
    "The simulation clock accepts nonnegative signed-64-bit milliseconds; this is a serialization range, not a timeout policy.",
    "One observation cannot establish parking completion, sustained occupation, adjacent-bay use impact, a violation or incident resolution.",
]


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _map_bays(map_data, frame):
    try:
        digest = sha256(json.dumps(map_data, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode()).hexdigest()
        if (digest != SUPPORTED_MAP_DIGEST or map_data["facility_id"] != FACILITY
                or frame.facility_id != FACILITY or map_data["map_version"] != MAP_VERSION
                or frame.map_version != MAP_VERSION):
            return digest, None
        declared = [entry["zone_id"] for entry in map_data["parking_bays"]]
        if len(declared) != len(set(declared)):
            return digest, None
        zones = {}
        for zone in map_data["zones"]:
            if zone["zone_id"] in declared:
                if zone["zone_id"] in zones or zone["type"] != "parking_bay":
                    return digest, None
                polygon = Polygon([(p["x"], p["y"]) for p in zone["polygon"]])
                if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
                    return digest, None
                zones[zone["zone_id"]] = polygon
        if set(zones) != set(declared):
            return digest, None
        return digest, zones
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, None


def _body(obj):
    angle = radians(obj.heading_deg)
    corners = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = u * obj.size.length_m / 2, v * obj.size.width_m / 2
        corners.append((obj.position.x + dx*cos(angle) - dy*sin(angle),
                        obj.position.y + dx*sin(angle) + dy*cos(angle)))
    return Polygon(corners)


def _supported_numeric_target(obj):
    values = (obj.position.x, obj.position.y, obj.size.length_m,
              obj.size.width_m, obj.quality.uncertainty_m)
    return (all(isfinite(value) for value in values)
            and abs(obj.position.x) <= MAX_ABS_COORD_M
            and abs(obj.position.y) <= MAX_ABS_COORD_M
            and MIN_BODY_DIMENSION_M <= obj.size.length_m <= MAX_BODY_DIMENSION_M
            and MIN_BODY_DIMENSION_M <= obj.size.width_m <= MAX_BODY_DIMENSION_M
            and obj.quality.uncertainty_m <= MAX_GEOMETRY_DISTANCE_M)


def _outer_footprint(body, uncertainty):
    if uncertainty == 0:
        return body
    # GEOS buffers approximate circles from inside. Inflate the requested
    # radius to the circumscribed polygon to preserve an upper area bound.
    radius = uncertainty / cos(pi / (4 * BUFFER_QUAD_SEGS))
    return body.buffer(radius, quad_segs=BUFFER_QUAD_SEGS)


def _inner_footprint(body, uncertainty):
    if uncertainty == 0:
        return body
    radius = uncertainty / cos(pi / (4 * BUFFER_QUAD_SEGS))
    return body.buffer(-radius, quad_segs=BUFFER_QUAD_SEGS)


def _max_depth(body, bay):
    # Distance to a convex bay is convex; its maximum on a rectangular
    # vehicle lies at a vertex. The pinned map's bays are rectangles.
    return max(Point(x, y).distance(bay) for x, y in list(body.exterior.coords)[:-1])


def _measure(target, bay, bays, bay_id, settings):
    uncertainty = target.quality.uncertainty_m
    body = _body(target)
    if not body.is_valid or body.is_empty or body.area <= 0:
        raise ValueError("degenerate body geometry")
    outer = _outer_footprint(body, uncertainty)
    inner = _inner_footprint(body, uncertainty)
    if (not outer.is_valid or outer.is_empty or outer.area <= 0
            or not inner.is_valid):
        raise ValueError("invalid uncertainty envelope")
    depth = _max_depth(body, bay)
    lower_depth = max(0.0, depth - uncertainty)
    # Unlike depth + uncertainty, the conservative outer envelope retains a
    # known-within result when every possible translation remains inside.
    upper_depth = _max_depth(outer, bay)
    epsilon = 1e-9  # Floating-point rounding at a mathematical edge only.
    relation = "unknown"
    if upper_depth <= settings.tolerance_m + epsilon:
        relation = "within"
    elif lower_depth > settings.tolerance_m + epsilon:
        relation = "overlap"
    values = dict(
        nominal_outside_area_m2=body.difference(bay).area,
        outside_area_lower_bound_m2=inner.difference(bay).area,
        outside_area_upper_bound_m2=outer.difference(bay).area,
        nominal_max_depth_m=depth,
        max_depth_lower_bound_m=lower_depth,
        max_depth_upper_bound_m=upper_depth,
        applied_tolerance_m=settings.tolerance_m,
        position_uncertainty_m=uncertainty,
    )
    overlaps = {
        adjacent_id: BayOverlapArea(
            nominal_m2=body.intersection(adjacent).area,
            lower_bound_m2=inner.intersection(adjacent).area,
            upper_bound_m2=outer.intersection(adjacent).area,
        )
        for adjacent_id, adjacent in bays.items()
        if adjacent_id != bay_id and bay.touches(adjacent)
    }
    if not all(isfinite(value) and value >= 0 for value in values.values()):
        raise ValueError("invalid numeric geometry")
    return relation, values, overlaps


def analyze_bay_footprint(map_data, observation, *, object_id, bay_id, settings,
                          expected_run_id, expected_state_version,
                          current_sim_time_ms, run_status, recovery_required,
                          observation_ready, now):
    """Measure a caller-selected object's current bay footprint only."""
    frame = Observation.model_validate(observation)
    settings = BayGeometrySettings.model_validate(settings)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC-aware")
    if (type(current_sim_time_ms) is not int or current_sim_time_ms < 0
            or current_sim_time_ms > MAX_SIM_TIME_MS):
        raise ValueError("current_sim_time_ms must be a nonnegative signed-64-bit integer")
    if type(expected_state_version) is not int or expected_state_version < 0:
        raise ValueError("expected_state_version must be a nonnegative integer")
    if type(recovery_required) is not bool or type(observation_ready) is not bool:
        raise ValueError("recovery_required and observation_ready must be booleans")

    map_digest, bays = _map_bays(map_data, frame)
    reasons = []
    support = "supported"
    if bays is None or bay_id not in bays:
        support = "unsupported_geometry"
        reasons.append("unsupported_map_or_bay")
    if frame.run_id != expected_run_id or frame.state_version != expected_state_version:
        reasons.append("context_version_mismatch")
    age_sim = current_sim_time_ms - frame.sim_time_ms
    received_at, observed_at = _stamp(frame.received_at), _stamp(frame.observed_at)
    wall_age = now - received_at
    delivery_age = received_at - observed_at
    freshness = timedelta(milliseconds=settings.freshness_ms)
    if age_sim < 0 or wall_age < -timedelta(milliseconds=1000) or delivery_age < timedelta(0):
        reasons.append("invalid_observation_clock")
    if (age_sim > settings.freshness_ms or delivery_age > freshness
            or (run_status == "running" and wall_age > freshness)):
        reasons.append("stale_observation")
    if run_status not in ("running", "paused") or recovery_required or not observation_ready:
        reasons.append("run_not_ready")
    if frame.coverage != "complete":
        reasons.append("coverage_" + frame.coverage)
    matches = [obj for obj in frame.objects if obj.object_id == object_id]
    if len(matches) != 1:
        reasons.append("target_missing_or_duplicate")
    else:
        target = matches[0]
        if (target.object_type != "vehicle" or target.quality.visibility != "visible"
                or target.quality.missing_fields or target.position is None
                or target.size is None or target.heading_deg is None
                or target.quality.uncertainty_m is None
                or target.quality.uncertainty_m > settings.max_uncertainty_m):
            reasons.append("target_geometry_uncertain")
        if (target.position is not None and target.size is not None
                and target.quality.uncertainty_m is not None
                and not _supported_numeric_target(target)):
            support = "unsupported_geometry"
            reasons.append("numeric_range_exceeded")
    if support == "supported" and reasons:
        support = "insufficient_data"

    values = {}
    overlaps = {}
    relation = "unknown"
    if support == "supported":
        try:
            relation, values, overlaps = _measure(target, bays[bay_id], bays,
                                                  bay_id, settings)
        except (GEOSException, ValidationError, ValueError, OverflowError):
            support = "unsupported_geometry"
            reasons.append("numeric_geometry_unavailable")
    return BayGeometryAnalysis(
        facility_id=frame.facility_id, run_id=frame.run_id,
        map_version=frame.map_version, map_digest=map_digest,
        observation_id=frame.observation_id, state_version=frame.state_version,
        evaluated_at_sim_time_ms=current_sim_time_ms,
        analyzed_at=now.isoformat().replace("+00:00", "Z"),
        object_id=object_id, bay_id=bay_id,
        support_status=support, geometry_relation=relation,
        reasons=sorted(set(reasons)), adjacent_bay_overlap_m2=overlaps,
        assumptions=ASSUMPTIONS, **values)
