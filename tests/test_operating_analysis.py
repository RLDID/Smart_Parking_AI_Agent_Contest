from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.operating_analysis import OperatingAnalysis
from simulator.world import FACILITY, MAP_VERSION, initial_world, advance


def vehicle(object_id, x, y, heading=90):
    return {"object_id": object_id, "object_type": "vehicle", "position": {"x": x, "y": y},
        "heading_deg": heading, "size": {"length_m": 4.6, "width_m": 1.8},
        "quality": {"visibility": "visible", "uncertainty_m": 0, "missing_fields": []}}


def evaluator(objects, duration=4000):
    now = datetime.now(timezone.utc)
    frames = []
    for t in range(0, duration + 1, 200):
        stamp = (now - timedelta(milliseconds=duration - t)).isoformat().replace("+00:00", "Z")
        frames.append({"facility_id": FACILITY, "map_version": MAP_VERSION, "run_id": "analysis-run",
            "observation_id": f"obs-{t}", "state_version": t // 100, "sim_time_ms": t,
            "coverage": "complete", "observed_at": stamp, "received_at": stamp,
            "objects": deepcopy(objects), "devices": []})
    world = {"run_id": "analysis-run", "state_version": duration // 100, "sim_time_ms": duration,
             "run_status": "paused", "recovery_required": False, "observation_history": frames,
             "observation": frames[-1]}
    return OperatingAnalysis(SimpleNamespace(world=world)), world


def test_fixed_exit_candidate_needs_stationary_blocker_and_new_continuous_clearance():
    analysis, world = evaluator([vehicle("obj-car-01", 9.5, 26.5), vehicle("obj-car-02", 9.5, 21.7, 0)])
    result = analysis("exit_blocked", "obj-car-02", "B01")
    assert result["support_status"] == "supported" and result["violation_candidate"]
    assert not result["clearance_sustained"]
    # One new clear sample cannot establish sustained recovery.
    world["observation_history"][-1]["objects"][1]["position"]["x"] = 27
    assert not analysis("exit_blocked", "obj-car-02", "B01")["clearance_sustained"]
    clear, _ = evaluator([vehicle("obj-car-01", 9.5, 26.5), vehicle("obj-car-02", 27, 21.7, 0)])
    assert clear("exit_blocked", "obj-car-02", "B01")["clearance_sustained"]


def test_intrusion_needs_sustained_adjacent_use_impact_not_only_angle():
    overlap, _ = evaluator([vehicle("obj-car-02", 11, 26.5)])
    result = overlap("bay_intrusion", "obj-car-02", "B01")
    assert result["violation_candidate"]
    occupation = result["adjacent_space_occupation"]
    assert occupation["sustained"] and occupation["bay_ids"] == ["B02"]
    assert occupation["current_overlap_lower_bound_m2"]["B02"] > 0
    contained, _ = evaluator([vehicle("obj-car-02", 9.5, 26.5, 94)])
    result = contained("bay_intrusion", "obj-car-02", "B01")
    assert not result["violation_candidate"] and result["clearance_sustained"]
    short, _ = evaluator([vehicle("obj-car-02", 11, 26.5)], 1000)
    assert not short("bay_intrusion", "obj-car-02", "B01")["violation_candidate"]


def test_one_adjacent_overlap_after_sustained_boundary_overlap_is_not_contact_evidence():
    # The body crosses B01's boundary for the whole history, but only the last
    # sample reaches B02. Persistent boundary overlap alone is insufficient.
    analysis, world = evaluator([vehicle("obj-car-02", 10.1, 29.2)], 6000)
    world["observation"]["objects"][0]["position"]["x"] = 10.11
    result = analysis("bay_intrusion", "obj-car-02", "B01")
    assert result["metrics"]["intrusion_state"] == "persistent_candidate"
    assert result["adjacent_space_occupation"]["current_overlap_lower_bound_m2"]["B02"] > 0
    assert not result["adjacent_space_occupation"]["sustained"]
    assert not result["violation_candidate"]


def test_incomplete_or_recovery_observation_cannot_authorize_contact():
    analysis, world = evaluator([vehicle("obj-car-02", 11, 26.5)])
    world["observation"]["coverage"] = "partial"
    assert not analysis("bay_intrusion", "obj-car-02", "B01")["violation_candidate"]
    world["observation"]["coverage"] = "complete"
    world["recovery_required"] = True
    assert analysis("bay_intrusion", "obj-car-02", "B01")["support_status"] == "insufficient_data"


def test_paused_risk_preserves_frozen_time_but_running_and_delivery_age_still_expire(monkeypatch):
    world = initial_world(6, "s2-crossing-v1")
    for _ in range(3):
        advance(world)
    analysis = OperatingAnalysis(SimpleNamespace(world=world))
    assert analysis("approach_risk", "obj-car-s2-v", "aisle-west")["violation_candidate"]
    late = datetime.now(timezone.utc) + timedelta(seconds=10)

    class LateClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return late

    monkeypatch.setattr("backend.operating_analysis.datetime", LateClock)
    assert analysis("approach_risk", "obj-car-s2-v", "aisle-west")["violation_candidate"]
    world["run_status"] = "running"
    assert not analysis("approach_risk", "obj-car-s2-v", "aisle-west")["violation_candidate"]
    world["run_status"] = "paused"
    world["recovery_required"] = True
    assert not analysis("approach_risk", "obj-car-s2-v", "aisle-west")["violation_candidate"]
    world["recovery_required"] = False
    for frame in world["observation_history"]:
        frame["observed_at"] = (datetime.fromisoformat(frame["observed_at"].replace("Z", "+00:00"))
                                - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    assert not analysis("approach_risk", "obj-car-s2-v", "aisle-west")["violation_candidate"]
