"""Pure S1-b fixed-candidate geometry over a public map and public observation.

This is a candidate clearance check, not a route planner, vehicle controller,
incident decision, driver request, or proof that every possible exit is blocked.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import ceil, cos, hypot, pi, radians, sin

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from contracts.exit_geometry import ExitCandidateAnalysis, ExitGeometrySettings
from contracts.models import Observation


FACILITY = "fac-demo-01"
MAP_VERSION = "map-01-draft"
ROUTE_POLICY = "route-foundation-v1"
# Canonical digest of the public map-01-draft from simulator.world.MAP.
# A map version is immutable. Any same-version geometry, lane, portal,
# parking-bay, or extension-field change is outside this fixed candidate's
# supported contract until reviewed under a new version.
SUPPORTED_MAP_DIGEST = "48761815f917c4dd68a1a6e53918db1b66b1e7431c649a84bc811380a47dd049"
EGRESS_OBJECT = "obj-car-01"
BODY_LENGTH_M = 4.6
BODY_WIDTH_M = 1.8
ARC_RADIUS_M = 4.0
HALF_DIAGONAL_M = hypot(BODY_LENGTH_M, BODY_WIDTH_M) / 2
# Implementation resource limit, not an operating or safety clearance policy.
MAX_ARC_SEGMENTS = 4096
# GEOS/Shapely circular buffers are inscribed polygons. These segments and
# secant inflation make every polygon edge tangent to (or outside) the stated
# radius. This is an implementation accuracy choice, not an S1-b policy value.
BUFFER_QUAD_SEGS = 16

ASSUMPTIONS = [
    "Only the fixed B01 A pose (9.5, 26.5, 90 degrees), 4.6 x 1.8 m body, and one west-portal exit candidate are supported.",
    "The route is reverse south to (9.5, 24), reverse along the radius-4 arc to (13.5, 20), then west through portal-west.",
    "All public visible objects other than A are static obstacles for this snapshot; future motion and internal actors are unknown.",
    "The union of B01-B03, central and west aisles, and a west-portal external corridor is the supported vehicle area.",
    "The complete public map-01-draft value is pinned; same-version map-policy changes are unsupported.",
    "Touching the conservative inflated sweep counts as blocked; conservative blocked may be a false positive.",
    "Clear means this one geometric candidate is clear at this observation, not that A moved or an incident resolved.",
]


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _outer_buffer(shape, distance):
    """Conservative Euclidean-radius buffer despite polygonal circle arcs."""
    if distance == 0:
        return shape
    outer_radius = distance / cos(pi / (4 * BUFFER_QUAD_SEGS))
    return shape.buffer(outer_radius, quad_segs=BUFFER_QUAD_SEGS)


def _body(x, y, heading):
    angle = radians(heading)
    points = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = u * BODY_LENGTH_M / 2, v * BODY_WIDTH_M / 2
        points.append((x + dx*cos(angle) - dy*sin(angle),
                       y + dx*sin(angle) + dy*cos(angle)))
    return Polygon(points)


def _observed_body(obj):
    angle = radians(obj.heading_deg)
    points = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = u * obj.size.length_m / 2, v * obj.size.width_m / 2
        points.append((obj.position.x + dx*cos(angle) - dy*sin(angle),
                       obj.position.y + dx*sin(angle) + dy*cos(angle)))
    return Polygon(points)


def _arc_pose(index, segments):
    angle = pi + (pi/2) * index/segments
    return _body(13.5 + ARC_RADIUS_M*cos(angle),
                 24 + ARC_RADIUS_M*sin(angle),
                 angle*180/pi - 90)


def _candidate_sweep(settings):
    # For one material point on the rigid body, speed with respect to arc angle
    # is at most R + half diagonal. Every unsampled pose is at angular distance
    # <= delta/2 from one sampled pose. Thus d <= (R+halfdiag)*delta/2.
    segments = ceil((ARC_RADIUS_M + HALF_DIAGONAL_M) * pi
                    / (4 * settings.max_discretization_error_m))
    if segments > MAX_ARC_SEGMENTS:
        return None, None
    step = pi / (2*segments)
    error_bound = (ARC_RADIUS_M + HALF_DIAGONAL_M) * step / 2
    reverse = unary_union([_body(9.5, 26.5, 90), _body(9.5, 24, 90)]).convex_hull
    arc_poses = [_arc_pose(i, segments) for i in range(segments + 1)]
    # Each hull includes both endpoint rectangles, and buffer(error_bound)
    # covers every intermediate rectangle regardless of rotation.
    arc = _outer_buffer(unary_union([unary_union((arc_poses[i], arc_poses[i+1])).convex_hull
                                     for i in range(segments)]), error_bound)
    west = unary_union((_body(13.5, 20, 180), _body(-2.3, 20, 180))).convex_hull
    return unary_union((reverse, arc, west)), error_bound


def _supported_map(map_data, frame):
    try:
        map_digest = sha256(json.dumps(map_data, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
        if map_digest != SUPPORTED_MAP_DIGEST:
            return None
        if (map_data["facility_id"] != FACILITY or frame.facility_id != FACILITY
                or map_data["map_version"] != MAP_VERSION or frame.map_version != MAP_VERSION
                or map_data["route_policy_version"] != ROUTE_POLICY
                or map_data["coordinate_system"] != {"unit": "m", "origin": "southwest",
                                                      "x_axis": "east", "y_axis": "north"}
                or map_data["bounds"] != {"min_x": 0, "min_y": 0, "max_x": 40, "max_y": 32}):
            return None
        required = {"B01": box(8, 24, 11, 29), "B02": box(11, 24, 14, 29),
                    "B03": box(14, 24, 17, 29), "aisle-central": box(8, 16, 32, 24),
                    "aisle-west": box(0, 18, 8, 22)}
        expected_types = {**{bay: "parking_bay" for bay in ("B01", "B02", "B03")},
                          "aisle-central": "aisle", "aisle-west": "aisle"}
        zones = map_data["zones"]
        for name, shape in required.items():
            matching = [zone for zone in zones if zone["zone_id"] == name]
            if len(matching) != 1 or matching[0]["type"] != expected_types[name]:
                return None
            if not Polygon([(p["x"], p["y"]) for p in matching[0]["polygon"]]).equals(shape):
                return None
        bays = {entry["zone_id"] for entry in map_data["parking_bays"]}
        if not {"B01", "B02", "B03"} <= bays:
            return None
        lanes = {entry["zone_id"]: entry for entry in map_data["lanes"]}
        if ("aisle-central" not in lanes["aisle-west"]["connected_to"]
                or "aisle-west" not in lanes["aisle-central"]["connected_to"]):
            return None
        portals = [p for p in map_data["portals"] if p["portal_id"] == "portal-west"]
        if (len(portals) != 1 or portals[0]["direction"] not in ("both", "exit")
                or "vehicle" not in portals[0]["allowed_object_types"]
                or {(p["x"], p["y"]) for p in portals[0]["boundary_segment"]}
                != {(0, 18), (0, 22)}):
            return None
        # Only this portal authorizes the external continuation. Other map
        # zones (pedestrian/announcement) do not enlarge vehicle area.
        return unary_union((*required.values(), box(-100, 18, 0, 22)))
    except (KeyError, TypeError, ValueError):
        return None


def analyze_exit_candidate(map_data, observation, *, settings,
                           current_sim_time_ms, run_status,
                           recovery_required=False, observation_ready=True, now=None):
    """Return supported-candidate clearance using only current public inputs.

    Caller must pass each unresolved S1-b policy/precision input explicitly.
    The result contains no predicted route, owner identity, or incident status.
    """
    frame = Observation.model_validate(observation)
    settings = ExitGeometrySettings.model_validate(settings)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC-aware")
    area = _supported_map(map_data, frame)
    support_status = "supported" if area is not None else "unsupported_geometry"
    reasons = [] if area is not None else ["unsupported_map_or_portal"]
    age = current_sim_time_ms - frame.sim_time_ms
    received_at = _stamp(frame.received_at)
    observed_at = _stamp(frame.observed_at)
    wall_age = now - received_at
    delivery_age = received_at - observed_at
    freshness = timedelta(milliseconds=settings.freshness_ms)
    if age < 0 or wall_age < -timedelta(milliseconds=1000) or delivery_age < timedelta(0):
        reasons.append("invalid_observation_clock")
    if (age > settings.freshness_ms or delivery_age > freshness
            or (run_status == "running" and wall_age > freshness)):
        reasons.append("stale_observation")
    if run_status not in ("running", "paused") or recovery_required or not observation_ready:
        reasons.append("run_not_ready")
    if frame.coverage != "complete":
        reasons.append("coverage_" + frame.coverage)
    ids = [obj.object_id for obj in frame.objects]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_object_id")
    for obj in frame.objects:
        if (obj.quality.visibility != "visible" or obj.quality.missing_fields
                or obj.position is None or obj.size is None or obj.heading_deg is None
                or obj.quality.uncertainty_m is None
                or obj.quality.uncertainty_m > settings.max_observation_uncertainty_m):
            reasons.append("object_geometry_uncertain")
    a_objects = [obj for obj in frame.objects if obj.object_id == EGRESS_OBJECT]
    if len(a_objects) != 1:
        reasons.append("egress_vehicle_missing_or_duplicate")
    elif "object_geometry_uncertain" not in reasons:
        a = a_objects[0]
        if (a.object_type != "vehicle" or abs(a.position.x - 9.5) > 1e-6
                or abs(a.position.y - 26.5) > 1e-6
                or abs(a.heading_deg - 90) > 1e-6
                or abs(a.size.length_m - BODY_LENGTH_M) > 1e-6
                or abs(a.size.width_m - BODY_WIDTH_M) > 1e-6):
            support_status = "unsupported_geometry"
            reasons.append("unsupported_egress_pose_or_body")
    if support_status == "supported" and reasons:
        support_status = "insufficient_data"
    passage = "unknown"
    collisions = []
    uncertainty = {}
    bound = None
    margin = None
    if support_status == "supported":
        sweep, bound = _candidate_sweep(settings)
        if sweep is None:
            support_status = "unsupported_geometry"
            reasons.append("resolution_exceeds_limit")
        else:
            margin = settings.side_clearance_m + a_objects[0].quality.uncertainty_m
            occupied = _outer_buffer(sweep, margin)
            if not area.covers(occupied):
                passage = "blocked"
                reasons.append("candidate_leaves_supported_vehicle_area")
            for obj in frame.objects:
                if obj.object_id == EGRESS_OBJECT:
                    continue
                uncertainty[obj.object_id] = obj.quality.uncertainty_m
                if occupied.intersects(_outer_buffer(_observed_body(obj), obj.quality.uncertainty_m)):
                    collisions.append(obj.object_id)
            if collisions:
                passage = "blocked"
            elif passage != "blocked":
                passage = "clear"
    return ExitCandidateAnalysis(
        facility_id=frame.facility_id, run_id=frame.run_id,
        map_version=frame.map_version, route_policy_version=map_data.get("route_policy_version", "unknown"),
        observation_ids=[frame.observation_id], state_version=frame.state_version,
        evaluated_at_sim_time_ms=current_sim_time_ms,
        analyzed_at=now.isoformat().replace("+00:00", "Z"),
        support_status=support_status, candidate_passage=passage,
        collision_object_ids=sorted(collisions), reasons=sorted(set(reasons)),
        arc_discretization_bound_m=bound, applied_clearance_m=margin,
        obstacle_uncertainty_m=uncertainty, assumptions=ASSUMPTIONS)
