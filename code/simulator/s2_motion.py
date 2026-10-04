"""Isolated S2 synthetic movement. No backend, Agent, alarm or device coupling.

The public observation uses the existing ``Size(0.6, 0.6)`` envelope for the
pedestrian. Physical contact below uses the planned circular radius 0.3 m.
Those different meanings must not be silently interchanged by analysis code.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import cos, pi, sin, sqrt
from random import Random

from shapely.geometry import Polygon, box

from contracts.models import Observation, Position
from contracts.s2_motion import S2ContactRecord, S2ExecutionRecord, S2MotionInput, S2MotionResult
from simulator.world import MAP, digest

_HALF_LENGTH = 2.3
_HALF_WIDTH = 0.9
_PERSON_RADIUS = 0.3
_EPS = 1e-12
# Exact public map-01-draft snapshot used by the supported straight S2 routes.
# A reused version string with altered lane, zone, portal, gate, or geometry
# semantics must not silently retain this candidate's support claim.
SUPPORTED_MAP_DIGEST = "48761815f917c4dd68a1a6e53918db1b66b1e7431c649a84bc811380a47dd049"


def _zone_bounds(name):
    if MAP["map_version"] != "map-01-draft":
        raise ValueError("Unsupported S2 map version")
    matches = [z for z in MAP["zones"] if z["zone_id"] == name]
    if len(matches) != 1:
        raise ValueError("Required S2 map zone is missing or ambiguous")
    polygon = Polygon([(p["x"], p["y"]) for p in matches[0]["polygon"]])
    if not polygon.is_valid or not polygon.equals(box(*polygon.bounds)):
        raise ValueError("S2 supports rectangular map zones only")
    return polygon.bounds


def _validate_routes(config):
    """Support only the public central aisle and northbound pedestrian link."""
    if (MAP["facility_id"] != config.facility_id
            or MAP["map_version"] != config.map_version
            or digest(MAP) != SUPPORTED_MAP_DIGEST):
        raise ValueError("S2 fixture and public map disagree")
    cx0, cy0, cx1, cy1 = _zone_bounds("aisle-central")
    px0, py0, px1, py1 = _zone_bounds("walkway")
    v = config.vehicle_start
    p = config.pedestrian_start
    if not (cx0 <= v.x - _HALF_LENGTH and config.vehicle_stop_x + _HALF_LENGTH <= cx1
            and cy0 <= v.y - _HALF_WIDTH and v.y + _HALF_WIDTH <= cy1):
        raise ValueError("Full vehicle path leaves the supported central aisle")
    if not (px0 <= p.x - _PERSON_RADIUS and p.x + _PERSON_RADIUS <= px1
            and py0 <= p.y - _PERSON_RADIUS
            and config.pedestrian_stop_y + _PERSON_RADIUS <= py1):
        raise ValueError("Full pedestrian circle leaves the supported walkway")


def _segment_box_entry(x, y, dx, dy, xmin, xmax, ymin, ymax):
    """First fraction at which a segment enters an axis-aligned closed box."""
    lo, hi = 0.0, 1.0
    for value, delta, lower, upper in ((x, dx, xmin, xmax), (y, dy, ymin, ymax)):
        if abs(delta) <= _EPS:
            if value < lower - _EPS or value > upper + _EPS:
                return None
            continue
        a, b = (lower - value) / delta, (upper - value) / delta
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
        if lo > hi + _EPS:
            return None
    return max(0.0, min(1.0, lo))


def _segment_circle_entry(x, y, dx, dy, cx, cy, radius):
    x, y = x - cx, y - cy
    c = x*x + y*y - radius*radius
    if c <= _EPS:
        return 0.0
    a = dx*dx + dy*dy
    if a <= _EPS:
        return None
    b = 2*(x*dx + y*dy)
    discriminant = b*b - 4*a*c
    if discriminant < -_EPS:
        return None
    root = (-b - sqrt(max(0.0, discriminant))) / (2*a)
    return max(0.0, min(1.0, root)) if -_EPS <= root <= 1 + _EPS else None


def _contact_fraction(vehicle_before, vehicle_after, pedestrian_before, pedestrian_after):
    """Exact first contact of a translating rectangle and simultaneous circle.

    Relative motion reduces the pair to a point segment against the rectangle
    expanded by a circle. Two strips and four quarter-corner circles form that
    rounded rectangle, so crossing the same coordinates at different times is
    correctly kept separate. Heading remains fixed at 0 degrees.
    """
    x = pedestrian_before.x - vehicle_before.x
    y = pedestrian_before.y - vehicle_before.y
    dx = (pedestrian_after.x - pedestrian_before.x) - (vehicle_after.x - vehicle_before.x)
    dy = (pedestrian_after.y - pedestrian_before.y) - (vehicle_after.y - vehicle_before.y)
    a, b, r = _HALF_LENGTH, _HALF_WIDTH, _PERSON_RADIUS
    entries = [
        _segment_box_entry(x, y, dx, dy, -a, a, -b-r, b+r),
        _segment_box_entry(x, y, dx, dy, -a-r, a+r, -b, b),
    ]
    entries.extend(_segment_circle_entry(x, y, dx, dy, cx, cy, r)
                   for cx in (-a, a) for cy in (-b, b))
    return min((value for value in entries if value is not None), default=None)


def _utc_at(initial, sim_ms):
    return (initial + timedelta(milliseconds=sim_ms)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _jitter(point, uncertainty, rng):
    if not uncertainty:
        return {"x": point.x, "y": point.y}
    radius, angle = uncertainty * sqrt(rng.random()), 2*pi*rng.random()
    return {"x": point.x + radius*cos(angle), "y": point.y + radius*sin(angle)}


def _observation(config, run_id, sim_ms, vehicle, pedestrian, rng):
    occlusion = config.pedestrian_occlusion
    hidden = occlusion is not None and occlusion.start_ms <= sim_ms < occlusion.end_ms
    timestamp = _utc_at(datetime.fromisoformat(config.initial_utc.replace("Z", "+00:00")), sim_ms)
    objects = [
        {"object_id": "obj-car-s2-v", "object_type": "vehicle",
         "position": _jitter(vehicle, config.observation_jitter_m, rng),
         "size": {"length_m": 4.6, "width_m": 1.8}, "heading_deg": 0.0,
         "quality": {"visibility": "visible", "uncertainty_m": config.observation_jitter_m,
                     "missing_fields": []}},
        {"object_id": "obj-person-s2-p", "object_type": "pedestrian",
         "position": None if hidden else _jitter(pedestrian, config.observation_jitter_m, rng),
         "size": None if hidden else {"length_m": 0.6, "width_m": 0.6},
         "heading_deg": None if hidden else 90.0,
         "quality": {"visibility": "occluded" if hidden else "visible",
                     "uncertainty_m": None if hidden else config.observation_jitter_m,
                     "missing_fields": ["position", "size", "heading_deg"] if hidden else []}},
    ]
    return Observation(
        facility_id=config.facility_id, run_id=run_id,
        observation_id=f"obs-{run_id}-{sim_ms}", map_version=config.map_version,
        state_version=sim_ms // config.tick_ms, sim_time_ms=sim_ms,
        observed_at=timestamp, received_at=timestamp,
        coverage="partial" if hidden else "complete", objects=objects,
        devices=[], object_events=[],
    )


def simulate_s2(config: S2MotionInput) -> S2MotionResult:
    """Run bounded deterministic fixture; contact freezes both actors at first touch.

    The UTC fields are a deterministic fixture mapping from sim time, not a live
    wall-clock measurement or real CCTV latency. ``execution_record`` is a
    private record of physical events; it is not an evaluation answer or
    evidence of incident resolution.
    """
    config = S2MotionInput.model_validate(config)
    _validate_routes(config)
    initial = datetime.fromisoformat(config.initial_utc.replace("Z", "+00:00"))
    if initial.utcoffset() != timedelta(0):
        raise ValueError("Initial clock must be UTC")
    fingerprint = json.dumps(config.model_dump(mode="json"), sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    run_id = "run-s2-" + sha256(fingerprint.encode()).hexdigest()[:16]
    rng = Random(config.seed)
    brake_at = (config.brake_command_at_ms + config.brake_response_delay_ms
                if config.brake_command_at_ms is not None else float("inf"))
    vehicle_target_at = (config.vehicle_stop_x - config.vehicle_start.x) * 1000 / config.vehicle_speed_mps
    pedestrian_target_at = (config.pedestrian_start_delay_ms +
                            (config.pedestrian_stop_y - config.pedestrian_start.y)
                            * 1000 / config.pedestrian_speed_mps)

    def planned(t):
        vx = min(config.vehicle_stop_x, config.vehicle_start.x +
                 config.vehicle_speed_mps * min(t, brake_at) / 1000)
        py = min(config.pedestrian_stop_y, config.pedestrian_start.y +
                 config.pedestrian_speed_mps * max(0, t - config.pedestrian_start_delay_ms) / 1000)
        return Position(x=vx, y=config.vehicle_start.y), Position(x=config.pedestrian_start.x, y=py)

    vehicle, pedestrian = planned(0)
    if _contact_fraction(vehicle, vehicle, pedestrian, pedestrian) is not None:
        raise ValueError("Initial S2 actors already touch")
    observations = [_observation(config, run_id, 0, vehicle, pedestrian, rng)]
    contact = None
    for start in range(0, config.duration_ms, config.tick_ms):
        end = start + config.tick_ms
        if contact is None:
            breaks = [start, end]
            breaks.extend(t for t in (brake_at, vehicle_target_at,
                                      config.pedestrian_start_delay_ms, pedestrian_target_at)
                          if start < t < end)
            breaks.sort()
            for a, b in zip(breaks, breaks[1:]):
                v0, p0 = planned(a)
                v1, p1 = planned(b)
                fraction = _contact_fraction(v0, v1, p0, p1)
                if fraction is not None:
                    when = a + (b - a) * fraction
                    vehicle, pedestrian = planned(when)
                    contact = S2ContactRecord(sim_time_ms=when,
                                             vehicle_position=vehicle,
                                             pedestrian_position=pedestrian)
                    break
            if contact is None:
                vehicle, pedestrian = planned(end)
        if end % config.observation_ms == 0:
            observations.append(_observation(config, run_id, end, vehicle, pedestrian, rng))
    return S2MotionResult(observations=observations,
                          execution_record=S2ExecutionRecord(contact=contact,
                              final_vehicle_position=vehicle,
                              final_pedestrian_position=pedestrian,
                              stopped_on_contact=contact is not None))


def public_observations(result: S2MotionResult) -> list[Observation]:
    """Return independent allowlisted snapshots, excluding fixture controls/truth."""
    validated = S2MotionResult.model_validate(result)
    return [Observation.model_validate(deepcopy(frame.model_dump()))
            for frame in validated.observations]
