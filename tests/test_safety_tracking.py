from types import ModuleType, SimpleNamespace
import sys

from backend.safety import SafetyController


def rig(monkeypatch):
    analysis = SimpleNamespace(status="risk_candidate", candidates=[SimpleNamespace(vehicle_id="car", pedestrian_id="person")])
    monkeypatch.setattr("backend.safety.analyze_approach", lambda *args, **kwargs: analysis)
    environment = ModuleType("simulator.environment")
    outcomes = {"clear_alarm": "held", "claim_alarm": "accepted"}
    def apply(world, command, **kwargs):
        zone = world["device_state"]["alarms"][0]
        if command.action == "claim_alarm":
            zone["claims"].append({"incident_id": command.incident_id})
        elif outcomes[command.action] == "accepted":
            zone["claims"] = [c for c in zone["claims"] if c["incident_id"] != command.incident_id]
        return SimpleNamespace(outcome=outcomes[command.action], reason="injected-device-feedback")
    environment.apply_device_command = apply
    monkeypatch.setitem(sys.modules, "simulator.environment", environment)
    world = {"sim_time_ms": 0, "recovery_required": False, "device_state": {"alarms": [
        {"zone_id": "announcement-a", "resource_version": 0, "claims": []}]},
        "observation_history": [{"observation_id": "old", "state_version": 0,
             "objects": [{"object_id": "car"}, {"object_id": "person"}]},
            {"observation_id": "initial", "state_version": 1,
             "objects": [{"object_id": "car"}, {"object_id": "person"}]}]}
    runtime = SimpleNamespace(operating_analysis=SimpleNamespace(settings={"approach": {}, "clearance_ms": 2000}))
    return SafetyController(runtime), world, analysis, outcomes


def tick(controller, world, t):
    world["sim_time_ms"] = t
    world["observation_history"][-1]["observation_id"] = f"obs-{t}"
    world["observation_history"][-1]["state_version"] += 1
    controller.step(world)


def test_held_clear_preserves_tracking_until_confirmed_and_missing_pair_never_clears(monkeypatch):
    controller, world, analysis, outcomes = rig(monkeypatch)
    tick(controller, world, 0)
    analysis.status = "clear_projection"
    tick(controller, world, 200)
    for t in range(400, 2401, 200):
        tick(controller, world, t)
    assert len(world["safety_state"]["claims"]) == 1
    assert len(world["device_state"]["alarms"][0]["claims"]) == 1
    world["observation_history"][-1]["objects"] = [{"object_id": "other-person"}, {"object_id": "car"}]
    outcomes["clear_alarm"] = "accepted"
    tick(controller, world, 2600)
    assert len(world["safety_state"]["claims"]) == 1
    world["observation_history"][-1]["objects"] = [{"object_id": "car"}, {"object_id": "person"}]
    tick(controller, world, 2800)
    for t in range(3000, 5001, 200):
        tick(controller, world, t)
    assert not world["safety_state"]["claims"]
    assert not world["device_state"]["alarms"][0]["claims"]


def test_new_risk_breaks_clearance_continuity_for_all_warning_claims(monkeypatch):
    controller, world, analysis, _ = rig(monkeypatch)
    tick(controller, world, 0)
    analysis.status = "clear_projection"
    tick(controller, world, 200)
    analysis.status = "risk_candidate"
    analysis.candidates = [SimpleNamespace(vehicle_id="other-car", pedestrian_id="other-person")]
    tick(controller, world, 2200)
    assert all(claim["clear_since_ms"] is None for claim in world["safety_state"]["claims"].values())
