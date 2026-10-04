"""S2 observed velocity projection; no fixture truth, Agent or device access.

It compares vehicle and pedestrian positions at the *same projected time*.
Constant velocity and heading are explicit assumptions, so a clear projection
must never be treated as proof that a future path is safe.
"""

from datetime import datetime, timedelta
from math import cos, hypot, isfinite, radians, sin, sqrt

from contracts.approach import ApproachAnalysis, ApproachCandidate, ApproachSettings
from contracts.models import Observation
from simulator.s2_motion import SUPPORTED_MAP_DIGEST
from simulator.world import digest


_ASSUMPTIONS = [
    "Only the pinned map-01-draft and public simulator observations are supported.",
    "The last two observed positions define constant velocity during the caller's horizon.",
    "The observed vehicle heading and body size remain fixed during projection.",
    "A pedestrian's public size is enclosed by a circle; position and inferred-velocity errors enlarge it.",
    "A clear projection is conditional on those assumptions, not a future safety guarantee or alarm-clear instruction.",
    "This component proposes candidates only; alarm issuance, release and incident resolution use separate policy.",
]
_MAX_HISTORY = 64
_MAX_OBJECTS = 64
_MAX_ABS_COORD = 1000.0
_MAX_BODY_DIM = 100.0
_MAX_SIM_MS = 2**63 - 1
_EPS = 1e-9


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _segment_box(x, y, dx, dy, xmin, xmax, ymin, ymax):
    lo, hi = 0.0, 1.0
    for point, delta, lower, upper in ((x, dx, xmin, xmax), (y, dy, ymin, ymax)):
        if abs(delta) <= _EPS:
            if point < lower - _EPS or point > upper + _EPS:
                return None
            continue
        a, b = (lower - point) / delta, (upper - point) / delta
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
        if lo > hi + _EPS:
            return None
    return max(0.0, lo)


def _segment_circle(x, y, dx, dy, cx, cy, radius):
    x -= cx
    y -= cy
    c = x*x + y*y - radius*radius
    if c <= _EPS:
        return 0.0
    a = dx*dx + dy*dy
    if a <= _EPS:
        return None
    b = 2*(x*dx + y*dy)
    determinant = b*b - 4*a*c
    if determinant < -_EPS:
        return None
    root = (-b - sqrt(max(0.0, determinant))) / (2*a)
    return max(0.0, root) if -_EPS <= root <= 1 + _EPS else None


def _proximity(vehicle, pedestrian, vehicle_delta, pedestrian_delta, radius):
    """First intersection of a moving point and rotated rounded rectangle."""
    angle = radians(vehicle.heading_deg)
    c, s = cos(angle), sin(angle)
    rx = pedestrian.position.x - vehicle.position.x
    ry = pedestrian.position.y - vehicle.position.y
    dx = pedestrian_delta[0] - vehicle_delta[0]
    dy = pedestrian_delta[1] - vehicle_delta[1]
    x, y = c*rx + s*ry, -s*rx + c*ry
    dx, dy = c*dx + s*dy, -s*dx + c*dy
    a, b = vehicle.size.length_m/2, vehicle.size.width_m/2
    entries = [
        _segment_box(x, y, dx, dy, -a, a, -b-radius, b+radius),
        _segment_box(x, y, dx, dy, -a-radius, a+radius, -b, b),
    ]
    entries.extend(_segment_circle(x, y, dx, dy, cx, cy, radius)
                   for cx in (-a, a) for cy in (-b, b))
    return min((entry for entry in entries if entry is not None), default=None)


def _quality(obj, settings):
    if (obj.quality.visibility != "visible" or obj.quality.missing_fields
            or obj.position is None or obj.size is None or obj.heading_deg is None
            or obj.quality.uncertainty_m is None):
        return "object_not_visible"
    vals = (obj.position.x, obj.position.y, obj.size.length_m,
            obj.size.width_m, obj.heading_deg, obj.quality.uncertainty_m)
    if (not all(isfinite(x) for x in vals)
            or max(abs(obj.position.x), abs(obj.position.y)) > _MAX_ABS_COORD
            or not 0 < obj.size.length_m <= _MAX_BODY_DIM
            or not 0 < obj.size.width_m <= _MAX_BODY_DIM):
        return "numeric_range_exceeded"
    if obj.quality.uncertainty_m > settings.max_uncertainty_m:
        return "position_uncertainty_exceeded"
    return None


def _result(status, frames, current_sim_time_ms, *, map_digest=None, reasons=(), candidates=()):
    latest = frames[-1] if frames else None
    return ApproachAnalysis(
        status=status, facility_id=latest.facility_id if latest else None,
        run_id=latest.run_id if latest else None,
        map_version=latest.map_version if latest else None,
        map_digest=map_digest, evaluated_at_sim_time_ms=current_sim_time_ms,
        observation_ids=[f.observation_id for f in frames],
        candidates=list(candidates), reasons=list(dict.fromkeys(reasons)),
        assumptions=_ASSUMPTIONS,
    )


def analyze_approach(map_data, history, *, current_sim_time_ms, current_utc,
                     recovery_required, settings) -> ApproachAnalysis:
    """Assess bounded vehicle/pedestrian approach using only public history.

    ``history`` must be chronological. Invalid or incomplete history fails
    closed with ``insufficient_data``; incompatible map returns
    ``unsupported_geometry``. Caller decides how to handle either state.
    """
    settings = ApproachSettings.model_validate(settings)
    if (type(current_sim_time_ms) is not int or not 0 <= current_sim_time_ms <= _MAX_SIM_MS):
        raise ValueError("current_sim_time_ms must be a nonnegative signed-64-bit integer")
    if (not isinstance(current_utc, datetime) or current_utc.tzinfo is None
            or current_utc.utcoffset() != timedelta(0)):
        raise ValueError("current_utc must be UTC-aware")
    if type(recovery_required) is not bool:
        raise ValueError("recovery_required must be boolean")
    if not isinstance(history, (list, tuple)) or len(history) > _MAX_HISTORY:
        raise ValueError("history must be a bounded list or tuple")
    for raw in history:
        objects = raw.get("objects") if isinstance(raw, dict) else getattr(raw, "objects", None)
        if isinstance(objects, list) and len(objects) > _MAX_OBJECTS:
            raise ValueError("observation exceeds object limit")
    try:
        map_digest = digest(map_data)
    except (TypeError, ValueError, OverflowError):
        map_digest = None
    frames = [Observation.model_validate(frame) for frame in history]
    if (not isinstance(map_data, dict) or map_digest != SUPPORTED_MAP_DIGEST
            or map_data.get("facility_id") != "fac-demo-01"
            or map_data.get("map_version") != "map-01-draft"):
        return _result("unsupported_geometry", frames, current_sim_time_ms,
                       map_digest=map_digest, reasons=["unsupported_map"])
    if len(frames) < 2:
        return _result("insufficient_data", frames, current_sim_time_ms,
                       map_digest=map_digest, reasons=["two_observations_required"])
    reasons = []
    first, last = frames[0], frames[-1]
    if recovery_required:
        reasons.append("recovery_required")
    if (first.facility_id != "fac-demo-01" or first.map_version != "map-01-draft"
            or any((f.facility_id, f.run_id, f.map_version, f.source)
                   != (first.facility_id, first.run_id, first.map_version, "simulator")
                   for f in frames)):
        reasons.append("observation_context_mismatch")
    if len({f.observation_id for f in frames}) != len(frames):
        reasons.append("duplicate_observation")
    if any(len(f.objects) > _MAX_OBJECTS for f in frames):
        reasons.append("object_limit_exceeded")
    if any(f.coverage != "complete" for f in frames):
        reasons.append("incomplete_coverage")
    for before, after in zip(frames, frames[1:]):
        gap = after.sim_time_ms - before.sim_time_ms
        if (not 0 < gap <= settings.max_sample_gap_ms
                or after.state_version <= before.state_version
                or _utc(after.observed_at) <= _utc(before.observed_at)
                or _utc(after.received_at) < _utc(before.received_at)):
            reasons.append("observation_order_or_gap")
        elif abs((_utc(after.observed_at) - _utc(before.observed_at)).total_seconds()*1000
                 - gap) > 1000:
            reasons.append("observation_clock_mismatch")
    for frame in frames:
        if (len({o.object_id for o in frame.objects}) != len(frame.objects)
                or len(frame.objects) > _MAX_OBJECTS):
            reasons.append("duplicate_or_excess_objects")
        if _utc(frame.received_at) < _utc(frame.observed_at):
            reasons.append("invalid_delivery_clock")
        elif (_utc(frame.received_at) - _utc(frame.observed_at)).total_seconds()*1000 > settings.freshness_ms:
            reasons.append("delayed_observation")
    if (last.sim_time_ms > current_sim_time_ms
            or current_sim_time_ms - last.sim_time_ms > settings.freshness_ms
            or _utc(last.received_at) > current_utc):
        reasons.append("invalid_or_stale_current_clock")
    elif (current_utc - _utc(last.received_at)).total_seconds()*1000 > settings.freshness_ms:
        reasons.append("stale_wall_clock")
    if reasons:
        return _result("insufficient_data", frames, current_sim_time_ms,
                       map_digest=map_digest, reasons=reasons)
    previous = {o.object_id: o for o in frames[-2].objects}
    current = {o.object_id: o for o in last.objects}
    relevant = [o for o in current.values() if o.object_type in ("vehicle", "pedestrian")]
    if not relevant or not any(o.object_type == "vehicle" for o in relevant) or not any(o.object_type == "pedestrian" for o in relevant):
        return _result("insufficient_data", frames, current_sim_time_ms,
                       map_digest=map_digest, reasons=["actor_pair_missing"])
    velocities = {}
    gap_seconds = (last.sim_time_ms - frames[-2].sim_time_ms) / 1000
    for obj in relevant:
        prior = previous.get(obj.object_id)
        if prior is None or prior.object_type != obj.object_type:
            reasons.append("actor_history_missing")
            continue
        for sample in (prior, obj):
            reason = _quality(sample, settings)
            if reason:
                reasons.append(reason)
        if reasons:
            continue
        if (obj.size.length_m != prior.size.length_m
                or obj.size.width_m != prior.size.width_m):
            reasons.append("body_size_changed")
        heading_delta = abs((obj.heading_deg - prior.heading_deg + 180) % 360 - 180)
        if heading_delta > settings.max_heading_change_deg:
            reasons.append("heading_change_exceeded")
        vx = (obj.position.x - prior.position.x) / gap_seconds
        vy = (obj.position.y - prior.position.y) / gap_seconds
        if not isfinite(vx) or not isfinite(vy) or hypot(vx, vy) > settings.max_speed_mps:
            reasons.append("speed_limit_exceeded")
        velocities[obj.object_id] = (vx, vy)
    if reasons:
        return _result("insufficient_data", frames, current_sim_time_ms,
                       map_digest=map_digest, reasons=reasons)
    candidates = []
    vehicles = [o for o in relevant if o.object_type == "vehicle"]
    people = [o for o in relevant if o.object_type == "pedestrian"]
    for vehicle in vehicles:
        for person in people:
            seconds = settings.horizon_ms / 1000
            v_delta = tuple(x*seconds for x in velocities[vehicle.object_id])
            p_delta = tuple(x*seconds for x in velocities[person.object_id])
            # A public pedestrian rectangle is conservatively enclosed by a
            # circle. Both observed center errors and the proposal margin are
            # added before the simultaneous relative-motion intersection.
            current_error = vehicle.quality.uncertainty_m + person.quality.uncertainty_m
            previous_error = (previous[vehicle.object_id].quality.uncertainty_m
                              + previous[person.object_id].quality.uncertainty_m)
            # A velocity inferred from two uncertain positions also carries
            # uncertainty. Its displacement error grows with horizon/gap.
            error = current_error + seconds/gap_seconds*(current_error + previous_error)
            radius = hypot(person.size.length_m/2, person.size.width_m/2) + settings.margin_m + error
            entry = _proximity(vehicle, person, v_delta, p_delta, radius)
            if entry is not None:
                candidates.append(ApproachCandidate(
                    vehicle_id=vehicle.object_id, pedestrian_id=person.object_id,
                    projected_first_proximity_ms=entry*settings.horizon_ms,
                    position_uncertainty_m=error))
    return _result("risk_candidate" if candidates else "clear_projection",
                   frames, current_sim_time_ms, map_digest=map_digest,
                   candidates=candidates)
