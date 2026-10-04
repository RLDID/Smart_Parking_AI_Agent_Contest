"""Independent synthetic proximity warning; no model, RAG or business watcher."""
from datetime import datetime

from contracts.devices import DeviceCommand
from simulator.approach import analyze_approach
from simulator.world import MAP, digest
from simulator.replay import recorded_clock, safety_clock_available


class SafetyController:
    def __init__(self, runtime):
        self.runtime = runtime

    def step(self, world):
        from simulator.environment import apply_device_command
        frames = world.get("observation_history") or []
        if world.get("recovery_required") or len(frames) < 2 or "device_state" not in world:
            for claim in world.get("safety_state", {}).get("claims", {}).values():
                claim["clear_since_ms"] = None
            return
        state = world.setdefault("safety_state", {"last_observation_id": None, "claims": {}})
        oid = frames[-1]["observation_id"]
        if state["last_observation_id"] == oid:
            return
        if not safety_clock_available(world):
            return
        previous_time = state.get("last_sim_time_ms")
        if previous_time is not None and not 0 < world["sim_time_ms"] - previous_time <= 400:
            for claim in state["claims"].values():
                claim["clear_since_ms"] = None
        state["last_sim_time_ms"] = world["sim_time_ms"]
        analysis = analyze_approach(MAP, frames[-2:], current_sim_time_ms=world["sim_time_ms"],
            current_utc=datetime.fromisoformat(recorded_clock(world, "safety").replace("Z", "+00:00")),
            recovery_required=world["recovery_required"],
            settings=self.runtime.operating_analysis.settings["approach"])
        state["last_observation_id"] = oid
        state["analysis_status"] = analysis.status
        claims = state["claims"]
        if analysis.status == "risk_candidate":
            for claim in claims.values():
                claim["clear_since_ms"] = None
            for pair in analysis.candidates:
                claim_id = "safety-" + digest([pair.vehicle_id, pair.pedestrian_id])[:32]
                claims.setdefault(claim_id, {"vehicle_id": pair.vehicle_id,
                    "pedestrian_id": pair.pedestrian_id, "clear_since_ms": None})
                claims[claim_id]["clear_since_ms"] = None
                zone = world["device_state"]["alarms"][0]
                if any(c["incident_id"] == claim_id for c in zone["claims"]):
                    continue
                result = apply_device_command(world, DeviceCommand(action="claim_alarm", operation_id=claim_id + ":" + oid,
                    zone_id=zone["zone_id"], incident_id=claim_id, expected_version=zone["resource_version"],
                    evidence_version=frames[-1]["state_version"], current_observation=True),
                    now_utc=recorded_clock(world, "safety_device"))
                state["feedback_reason"] = result.reason
        elif analysis.status == "clear_projection":
            for claim_id, claim in list(claims.items()):
                observed = set.intersection(*[{obj["object_id"] for obj in frame["objects"]} for frame in frames[-2:]])
                if claim["vehicle_id"] not in observed or claim["pedestrian_id"] not in observed:
                    claim["clear_since_ms"] = None
                    continue
                if claim["clear_since_ms"] is None:
                    claim["clear_since_ms"] = world["sim_time_ms"]
                if world["sim_time_ms"] - claim["clear_since_ms"] < self.runtime.operating_analysis.settings["clearance_ms"]:
                    continue
                zone = world["device_state"]["alarms"][0]
                result = apply_device_command(world, DeviceCommand(action="clear_alarm", operation_id="clear-" + claim_id + ":" + oid,
                    zone_id=zone["zone_id"], incident_id=claim_id, expected_version=zone["resource_version"],
                    evidence_version=frames[-1]["state_version"], current_observation=True),
                    now_utc=recorded_clock(world, "safety_device"))
                state["feedback_reason"] = result.reason
                if result.outcome == "accepted":
                    del claims[claim_id]
        else:
            for claim in claims.values():
                claim["clear_since_ms"] = None
