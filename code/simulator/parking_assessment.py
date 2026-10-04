"""Pure parking-history features. No server, contacts, intent or incident state."""
from datetime import datetime, timedelta
from math import hypot

from contracts.models import Observation
from contracts.parking_assessment import ParkingAssessment, ParkingAssessmentSettings
from simulator.s1c_bay_geometry import analyze_bay_footprint

MAX_INPUT_FRAMES = 512
MAX_OBJECTS_PER_FRAME = 128
EPSILON = 1e-9
ASSUMPTIONS = [
    "Only the pinned map and rectangular bay measurement supported by the S1-c geometry core are evaluated.",
    "Duration comes from consecutive public observations; retransmissions, gaps, missing/occluded targets and recovery do not prove time spent parked.",
    "Stationarity bounds every pair of observed positions in its contiguous suffix, including both position uncertainties; heading and size are exact observations.",
    "Stationary_candidate is an observed motion feature, not evidence of the driver's intent or parking completion.",
    "Maneuver grace must elapse both after observed movement stops and after contiguous overlap begins; time inside tolerance never consumes overlap grace.",
    "Persistent_candidate is sustained footprint overlap under explicit experiment settings; adjacent use impact, violations, contact and incident resolution are not evaluated.",
    "A paused simulation describes recorded/frozen time; this is not live CCTV or real parking safety validation.",
]


def _stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _angle_difference(a, b):
    difference = abs(a - b) % 360
    return min(difference, 360 - difference)


def _target(frame, object_id):
    matches = [obj for obj in frame.objects if obj.object_id == object_id]
    return matches[0] if len(matches) == 1 else None


def _same_size(a, b):
    return a.size == b.size


def _motion_suffix(records, settings):
    """Pairwise diameter prevents slow drift from being counted as a full stop."""
    latest = records[-1][0]
    suffix = [records[-1]]
    moved = uncertain = False
    for record in reversed(records[:-1]):
        candidate = record[2]
        pairs = [(candidate, entry[2]) for entry in suffix]
        if any(not _same_size(a, b) for a, b in pairs):
            uncertain = True
            break
        if any(_angle_difference(a.heading_deg, b.heading_deg)
               > settings.heading_tolerance_deg + EPSILON for a, b in pairs):
            moved = True
            break
        distances = [(hypot(a.position.x-b.position.x, a.position.y-b.position.y),
                      a.quality.uncertainty_m + b.quality.uncertainty_m)
                     for a, b in pairs]
        if any(distance + error > settings.position_tolerance_m + EPSILON
               for distance, error in distances):
            moved = any(max(0, distance-error) > settings.position_tolerance_m + EPSILON
                        for distance, error in distances)
            uncertain = not moved
            break
        suffix.append(record)
    duration = latest.sim_time_ms - suffix[-1][0].sim_time_ms
    if len(suffix) >= 2 and duration >= settings.stationary_ms:
        return "stationary_candidate", duration, []
    if uncertain:
        return "unknown", None, ["stationarity_uncertain"]
    if moved:
        return "moving", duration, ["observed_motion"]
    return "insufficient_history", duration, ["stationary_hold_not_observed"]


def assess_parking(map_data, history, *, object_id, bay_id, settings,
                   expected_run_id, expected_state_version, current_sim_time_ms,
                   run_status, recovery_required, observation_ready, now):
    """Assess a caller-selected object/bay using only bounded public history.

    All tolerances and hold/grace intervals are required caller settings.
    Authorization, bay assignment and actual use impact remain outside this core.
    """
    settings = ParkingAssessmentSettings.model_validate(settings)
    if not isinstance(history, (list, tuple)) or not 1 <= len(history) <= MAX_INPUT_FRAMES:
        raise ValueError("A bounded nonempty observation history is required")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC-aware")
    frames = [Observation.model_validate(item) for item in history]
    if any(len(frame.objects) > MAX_OBJECTS_PER_FRAME for frame in frames):
        raise ValueError("Observation object resource limit exceeded")
    latest = frames[-1]
    current_geometry = analyze_bay_footprint(
        map_data, latest, object_id=object_id, bay_id=bay_id, settings=settings.geometry,
        expected_run_id=expected_run_id, expected_state_version=expected_state_version,
        current_sim_time_ms=current_sim_time_ms, run_status=run_status,
        recovery_required=recovery_required, observation_ready=observation_ready, now=now)
    reasons = list(current_geometry.reasons)
    normalized, seen = [], {}
    history_invalid = False
    for frame in frames:
        scope = (frame.facility_id, frame.run_id, frame.map_version, frame.source)
        if scope != (latest.facility_id, latest.run_id, latest.map_version, latest.source):
            reasons.append("history_scope_mismatch")
            history_invalid = True
        observed_at, received_at = _stamp(frame.observed_at), _stamp(frame.received_at)
        if received_at < observed_at or observed_at > now or received_at > now:
            reasons.append("invalid_history_clock")
            history_invalid = True
        if len({obj.object_id for obj in frame.objects}) != len(frame.objects):
            reasons.append("duplicate_object_id")
            history_invalid = True
        if frame.observation_id in seen:
            if frame != seen[frame.observation_id]:
                reasons.append("conflicting_observation_id")
                history_invalid = True
            if normalized and frame != normalized[-1]:
                reasons.append("out_of_order_retransmission")
                history_invalid = True
            continue
        seen[frame.observation_id] = frame
        if normalized:
            previous = normalized[-1]
            if (frame.sim_time_ms <= previous.sim_time_ms
                    or frame.state_version <= previous.state_version
                    or observed_at < _stamp(previous.observed_at)
                    or received_at < _stamp(previous.received_at)):
                reasons.append("invalid_history_order")
                history_invalid = True
        normalized.append(frame)

    def result(support, *, records=(), motion="unknown", stationary=None,
               intrusion="unknown", intrusion_duration=None):
        return ParkingAssessment(
            facility_id=latest.facility_id, run_id=latest.run_id, map_version=latest.map_version,
            object_id=object_id, bay_id=bay_id, state_version=latest.state_version,
            evaluated_at_sim_time_ms=current_sim_time_ms,
            analyzed_at=now.isoformat().replace("+00:00", "Z"), support_status=support,
            observation_ids=[entry[0].observation_id for entry in records] or [latest.observation_id],
            motion_state=motion, stationary_duration_ms=stationary,
            intrusion_state=intrusion, intrusion_duration_ms=intrusion_duration,
            geometry=current_geometry, reasons=sorted(set(reasons)), applied_settings=settings,
            assumptions=ASSUMPTIONS)

    if current_geometry.support_status != "supported":
        return result(current_geometry.support_status)
    if history_invalid:
        return result("insufficient_data")

    records = []
    for frame in normalized[-settings.history_limit:]:
        geometry = analyze_bay_footprint(
            map_data, frame, object_id=object_id, bay_id=bay_id, settings=settings.geometry,
            expected_run_id=expected_run_id, expected_state_version=frame.state_version,
            current_sim_time_ms=frame.sim_time_ms, run_status="paused",
            recovery_required=False, observation_ready=True, now=_stamp(frame.received_at))
        target = _target(frame, object_id)
        if geometry.support_status != "supported":
            records.clear()
            reasons.append("history_quality_reset")
            continue
        if records and frame.sim_time_ms-records[-1][0].sim_time_ms > settings.max_sample_gap_ms:
            records.clear()
            reasons.append("history_gap_reset")
        records.append((frame, geometry, target))
    # Current_geometry was checked independently; never turn a missing suffix
    # into an empty bay or a finished manoeuvre.
    if not records or records[-1][0].observation_id != latest.observation_id:
        return result("insufficient_data")
    motion, stationary_duration, motion_reasons = _motion_suffix(records, settings)
    reasons.extend(motion_reasons)
    relation = current_geometry.geometry_relation
    if relation == "unknown":
        reasons.append("current_geometry_uncertain")
        return result("supported", records=records, motion=motion, stationary=stationary_duration)
    if relation == "within":
        return result("supported", records=records, motion=motion, stationary=stationary_duration,
                      intrusion="within_tolerance", intrusion_duration=0)

    overlap_suffix = []
    for record in reversed(records):
        if record[1].geometry_relation != "overlap":
            break
        overlap_suffix.append(record)
    intrusion_duration = latest.sim_time_ms-overlap_suffix[-1][0].sim_time_ms
    persistent = (motion == "stationary_candidate"
                  and stationary_duration >= max(settings.stationary_ms, settings.maneuver_grace_ms)
                  and intrusion_duration >= max(settings.intrusion_hold_ms, settings.maneuver_grace_ms))
    if not persistent:
        reasons.append("intrusion_hold_or_maneuver_grace_not_observed")
    return result("supported", records=records, motion=motion, stationary=stationary_duration,
                  intrusion="persistent_candidate" if persistent else "transient_or_maneuver",
                  intrusion_duration=intrusion_duration)
