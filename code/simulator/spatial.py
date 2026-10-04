"""Observation-only geometry: never import world actors, routes or scenario truth."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import cos, hypot, radians, sin

from shapely.geometry import Polygon, box

from contracts.models import Observation
from contracts.spatial import SpatialAnalysis

POLICY = {
    "version": "west-straight-v1", "vehicle_length_m": 4.6, "vehicle_width_m": 1.8,
    "side_clearance_m": 0.3, "stationary_ms": 5000, "recovery_ms": 3000,
    "freshness_ms": 1000, "sample_gap_ms": 200, "position_tolerance_m": 0.1,
    "heading_tolerance_deg": 2, "max_uncertainty_m": 0.1, "history_limit": 64,
}
ASSUMPTIONS = [
    "Virtual west aisle only; fixed east-west heading and constant lateral position.",
    "Vehicle 4.6 x 1.8 m; side clearance 0.3 m on both sides; touching is blocked.",
    "Centers move x=-2.3 to 10.3 m; body sweep x=-4.6 to 12.6 m includes west portal and central approach.",
    "Only a constant-y route through y=18..22 m is evaluated; no curved route or north bypass.",
    "Stationary 5000 ms and clear 3000 ms are experiment features, not violations or incident resolution.",
    "Paused data describes frozen simulation time; it is not live facility evidence.",
]


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def body_polygon(obj):
    angle = radians(obj.heading_deg)
    points = []
    for u, v in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        dx, dy = u * obj.size.length_m / 2, v * obj.size.width_m / 2
        points.append((obj.position.x + dx*cos(angle) - dy*sin(angle),
                       obj.position.y + dx*sin(angle) + dy*cos(angle)))
    # A rectangular expansion bounds the reported positional uncertainty.
    shape = Polygon(points)
    uncertainty = obj.quality.uncertainty_m
    if uncertainty:
        x1, y1, x2, y2 = shape.bounds
        shape = box(x1-uncertainty, y1-uncertainty, x2+uncertainty, y2+uncertainty)
    return shape


def quality_reasons(frame):
    reasons = []
    if frame.coverage != "complete":
        reasons.append("coverage_" + frame.coverage)
    if len({obj.object_id for obj in frame.objects}) != len(frame.objects):
        reasons.append("duplicate_object_id")
    for obj in frame.objects:
        if (obj.quality.visibility != "visible" or obj.quality.missing_fields
                or obj.position is None or obj.size is None or obj.heading_deg is None
                or obj.object_type == "unknown" or obj.quality.uncertainty_m is None
                or obj.quality.uncertainty_m > POLICY["max_uncertainty_m"]):
            reasons.append("object_geometry_uncertain")
    if timestamp(frame.received_at) < timestamp(frame.observed_at):
        reasons.append("invalid_observation_clock")
    return sorted(set(reasons))


def geometry(frame):
    """Largest open lateral band across the ENTIRE supported straight passage.

    The union of occupied y projections is exact for a constant-y east-west
    sweep. It is deliberately not a general path planner or a sampled grid.
    """
    length = POLICY["vehicle_length_m"]
    corridor = box(-length, 18, 8+length, 22)
    intervals, occupants = [], []
    for obj in frame.objects:
        overlap = body_polygon(obj).intersection(corridor)
        if not overlap.is_empty:
            occupants.append(obj.object_id)
            intervals.append((overlap.bounds[1], overlap.bounds[3]))
    edge, gaps = 18.0, []
    for low, high in sorted(intervals):
        gaps.append(max(0, low-edge))
        edge = max(edge, high)
    gaps.append(max(0, 22-edge))
    available = max(gaps)
    required = POLICY["vehicle_width_m"] + 2*POLICY["side_clearance_m"]
    # Positive clearance is required; exact contact isn't a traversable path.
    passage = "clear" if available > required + 1e-9 else "blocked"
    return passage, round(available, 6), sorted(occupants)


def supported_map(map_data, frame, zone_id):
    zone = next((z for z in map_data.get("zones", []) if z["zone_id"] == zone_id), None)
    lane = next((z for z in map_data.get("lanes", []) if z["zone_id"] == zone_id), None)
    central = next((z for z in map_data.get("zones", []) if z["zone_id"] == "aisle-central"), None)
    portal = next((p for p in map_data.get("portals", []) if p["portal_id"] == "portal-west"), None)
    return (zone_id == "aisle-west" and frame.map_version == "map-01-draft"
            and map_data.get("map_version") == frame.map_version
            and map_data.get("facility_id") == frame.facility_id
            and map_data.get("route_policy_version") == "route-foundation-v1"
            and zone and zone["type"] == "aisle" and lane
            and "aisle-central" in lane["connected_to"]
            and central and central["type"] == "aisle"
            and Polygon([(p["x"], p["y"]) for p in central["polygon"]]).covers(box(8, 18, 8+POLICY["vehicle_length_m"], 22))
            and portal and portal["direction"] == "both" and "vehicle" in portal["allowed_object_types"]
            and {(p["x"], p["y"]) for p in portal["boundary_segment"]} == {(0, 18), (0, 22)}
            and len(zone["polygon"]) == 4
            and {(p["x"], p["y"]) for p in zone["polygon"]} == {(0, 18), (8, 18), (8, 22), (0, 22)})


def analyze_spatial_context(map_data, history, *, current_sim_time_ms, run_status,
                            recovery_required=False, observation_ready=True, zone_id="aisle-west", now=None):
    """Analyze a bounded observation history supplied by the authorized server.

    No state mutation, scenario inputs, internal actor data or evaluation files.
    The caller supplies a simulation clock and availability status, not truth.
    """
    frames = [Observation.model_validate(item) for item in history[-POLICY["history_limit"]:]]
    if not frames:
        raise ValueError("At least one observation is required; no observation is not an empty lot")
    latest = frames[-1]
    now = now or datetime.now(timezone.utc)
    age = current_sim_time_ms - latest.sim_time_ms
    wall_age = (now - timestamp(latest.received_at)) // timedelta(milliseconds=1)
    reasons = quality_reasons(latest)
    normalized, seen = [], {}
    for frame in frames:
        key = frame.observation_id
        if key in seen:
            if frame != seen[key]:
                reasons.append("conflicting_observation_id")
            if normalized and frame.state_version < normalized[-1].state_version:
                reasons.append("invalid_history_order_or_scope")
            continue  # Exact retransmission never adds elapsed observation time.
        seen[key] = frame
        if ((frame.facility_id, frame.run_id, frame.map_version)
                != (latest.facility_id, latest.run_id, latest.map_version)
                or frame.sim_time_ms > current_sim_time_ms
                or (normalized and (frame.sim_time_ms <= normalized[-1].sim_time_ms
                                    or frame.state_version <= normalized[-1].state_version))):
            reasons.append("invalid_history_order_or_scope")
        normalized.append(frame)
    if age < 0 or wall_age < -1000:
        reasons.append("invalid_observation_clock")
    stale = age > POLICY["freshness_ms"] or (run_status == "running" and wall_age > POLICY["freshness_ms"])
    if stale:
        reasons.append("stale_observation")
    if recovery_required or run_status not in ("running", "paused"):
        reasons.append("run_not_ready")
    if not observation_ready:
        reasons.append("awaiting_post_boundary_observation")
    supported = supported_map(map_data, latest, zone_id)
    if not supported:
        reasons.append("unsupported_zone_or_map")
    status = "unsupported_geometry" if not supported else "insufficient_data" if reasons else "supported"
    metrics = {"required_clearance_m": POLICY["vehicle_width_m"] + 2*POLICY["side_clearance_m"]}
    if status == "supported":
        passage, clearance, occupants = geometry(latest)
        # Only uninterrupted, trustworthy samples can extend a temporal feature.
        suffix = [normalized[-1]]
        for frame in reversed(normalized[:-1]):
            if suffix[-1].sim_time_ms-frame.sim_time_ms > POLICY["sample_gap_ms"] or quality_reasons(frame):
                break
            suffix.append(frame)
        duration = 0
        for frame in suffix[1:]:
            if geometry(frame)[0] != passage:
                break
            duration = latest.sim_time_ms-frame.sim_time_ms
        stop_metrics = []
        for obj in latest.objects:
            stop_duration = 0
            for frame in suffix[1:]:
                previous = next((other for other in frame.objects if other.object_id == obj.object_id), None)
                if previous is None:
                    break
                angle_diff = abs(previous.heading_deg-obj.heading_deg)
                if (previous.object_type != obj.object_type or previous.size != obj.size
                        or hypot(previous.position.x-obj.position.x, previous.position.y-obj.position.y) > POLICY["position_tolerance_m"]
                        or min(angle_diff, 360-angle_diff) > POLICY["heading_tolerance_deg"]):
                    break
                stop_duration = latest.sim_time_ms-frame.sim_time_ms
            stop_metrics.append({"object_id": obj.object_id, "stop_duration_ms": stop_duration,
                                 "stationary_candidate": obj.object_type == "vehicle"
                                 and obj.object_id in occupants and stop_duration >= POLICY["stationary_ms"]})
        metrics.update(passage=passage, available_clearance_m=clearance,
                       occupied_object_ids=occupants, blocked_duration_ms=duration if passage == "blocked" else 0,
                       clear_duration_ms=duration if passage == "clear" else 0,
                       clearance_sustained=passage == "clear" and duration >= POLICY["recovery_ms"],
                       objects=stop_metrics)
    result = {
        "analysis_id": "pending", "facility_id": latest.facility_id, "run_id": latest.run_id,
        "map_version": latest.map_version, "route_policy_version": map_data.get("route_policy_version", "unknown"),
        "observation_ids": [frame.observation_id for frame in normalized], "state_version": latest.state_version,
        "evaluated_at_sim_time_ms": current_sim_time_ms, "analyzed_at": now.isoformat().replace("+00:00", "Z"),
        "object_ids": sorted({obj.object_id for obj in latest.objects}), "zone_ids": [zone_id],
        "metrics": metrics, "assumptions": ASSUMPTIONS,
        "quality": {"freshness": "stale" if stale else "unknown" if reasons else "fresh",
                    "run_status": run_status, "observation_age_sim_ms": age,
                    "received_age_wall_ms": wall_age, "reasons": sorted(set(reasons))},
        "support_status": status,
    }
    result["analysis_id"] = "analysis-" + sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:24]
    return SpatialAnalysis.model_validate(result)
