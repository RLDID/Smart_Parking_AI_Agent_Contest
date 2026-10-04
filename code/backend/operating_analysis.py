"""Public observation analysis under explicit, synthetic-only experiment settings."""
from datetime import datetime, timezone
import json
from math import hypot
from pathlib import Path

from simulator.approach import analyze_approach
from simulator.exit_geometry import analyze_exit_candidate
from simulator.parking_assessment import assess_parking
from simulator.s1c_bay_geometry import analyze_bay_footprint
from simulator.world import MAP

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data/samples/sim0-operation-settings.json"


class OperatingAnalysis:
    def __init__(self, runtime):
        self.runtime = runtime
        self.settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if self.settings["mode"] != "synthetic_demo":
            raise ValueError("Only synthetic operating settings are supported")

    @staticmethod
    def _suffix(frames, predicate, required_ms):
        suffix = []
        for frame in reversed(frames):
            if (frame["coverage"] != "complete" or not predicate(frame)
                    or suffix and not 0 < suffix[-1]["sim_time_ms"] - frame["sim_time_ms"] <= 400):
                break
            suffix.append(frame)
        return len(suffix) >= 2 and suffix[0]["sim_time_ms"] - suffix[-1]["sim_time_ms"] >= required_ms

    def __call__(self, impact_type, object_id, zone_id):
        w = self.runtime.world
        frames = w.get("observation_history") or [w["observation"]]
        now = datetime.now(timezone.utc)
        result = {"support_status": "insufficient_data", "violation_candidate": False,
                  "clearance_sustained": False, "occupied": False,
                  "observation_ids": [f["observation_id"] for f in frames],
                  "state_version": w["state_version"], "metrics": {"settings_version": self.settings["version"],
                  "mode": "synthetic_demo"}}
        common = {"current_sim_time_ms": w["sim_time_ms"], "run_status": w["run_status"],
                  "recovery_required": w["recovery_required"], "observation_ready": bool(w.get("observation_history")), "now": now}
        if impact_type == "exit_blocked":
            analysis = analyze_exit_candidate(MAP, frames[-1], settings=self.settings["exit"], **common)
            result["support_status"] = analysis.support_status
            def stationary(frame):
                target = next((o for o in frame["objects"] if o["object_id"] == object_id), None)
                latest = next((o for o in frames[-1]["objects"] if o["object_id"] == object_id), None)
                return bool(target and latest and target["quality"]["visibility"] == "visible"
                    and not target["quality"]["missing_fields"] and target.get("position") and latest.get("position")
                    and target["quality"].get("uncertainty_m") is not None
                    and hypot(target["position"]["x"] - latest["position"]["x"], target["position"]["y"] - latest["position"]["y"])
                        + target["quality"]["uncertainty_m"] + latest["quality"]["uncertainty_m"] <= .1)
            result["occupied"] = object_id in analysis.collision_object_ids
            result["violation_candidate"] = (analysis.support_status == "supported" and result["occupied"]
                and self._suffix(frames, stationary, self.settings["stationary_ms"]))
            # Old observations are evaluated at their own clock for continuous geometry;
            # the current sample still independently passes freshness/recovery checks.
            def historic_clear(frame):
                args = dict(common, current_sim_time_ms=frame["sim_time_ms"], now=datetime.fromisoformat(frame["received_at"].replace("Z", "+00:00")))
                value = analyze_exit_candidate(MAP, frame, settings=self.settings["exit"], **args)
                return value.support_status == "supported" and value.candidate_passage == "clear"
            result["clearance_sustained"] = analysis.support_status == "supported" and self._suffix(frames, historic_clear, self.settings["clearance_ms"])
            result["metrics"].update(analysis.model_dump())
        elif impact_type == "bay_intrusion":
            if zone_id not in {b["zone_id"] for b in MAP["parking_bays"]}:
                return result | {"support_status": "unsupported_geometry"}
            analysis = assess_parking(MAP, frames, object_id=object_id, bay_id=zone_id,
                settings=self.settings["parking"], expected_run_id=w["run_id"],
                expected_state_version=frames[-1]["state_version"], **common)
            result["support_status"] = analysis.support_status
            adjacent = {bay: area.lower_bound_m2 for bay, area in
                        analysis.geometry.adjacent_bay_overlap_m2.items() if area.lower_bound_m2 > 0}
            history_geometry = {}
            def occupies_adjacent(frame, bay):
                if frame["observation_id"] not in history_geometry:
                    history_geometry[frame["observation_id"]] = analyze_bay_footprint(
                        MAP, frame, object_id=object_id, bay_id=zone_id,
                        settings=self.settings["parking"]["geometry"], expected_run_id=w["run_id"],
                        expected_state_version=frame["state_version"], current_sim_time_ms=frame["sim_time_ms"],
                        run_status="paused", recovery_required=False, observation_ready=True,
                        now=datetime.fromisoformat(frame["received_at"].replace("Z", "+00:00")))
                geometry = history_geometry[frame["observation_id"]]
                area = geometry.adjacent_bay_overlap_m2.get(bay)
                return geometry.support_status == "supported" and area is not None and area.lower_bound_m2 > 0
            hold_ms = self.settings["parking"]["intrusion_hold_ms"]
            sustained_bays = [bay for bay in adjacent if analysis.support_status == "supported"
                and self._suffix(frames[-self.settings["parking"]["history_limit"]:],
                                 lambda frame, bay=bay: occupies_adjacent(frame, bay), hold_ms)]
            result["occupied"] = analysis.geometry.geometry_relation == "overlap"
            result["violation_candidate"] = (analysis.support_status == "supported"
                and analysis.intrusion_state == "persistent_candidate" and bool(sustained_bays))
            result["adjacent_space_occupation"] = {
                "basis": "synthetic_public_geometry_history", "sustained": bool(sustained_bays),
                "bay_ids": sustained_bays, "required_duration_ms": hold_ms,
                "current_overlap_lower_bound_m2": adjacent,
                "scope": "가상 인접 주차면 사용 공간의 지속 점유. 실제 운전자의 이용 방해나 의도는 판정하지 않음."}
            def within(frame):
                args = dict(common, current_sim_time_ms=frame["sim_time_ms"], now=datetime.fromisoformat(frame["received_at"].replace("Z", "+00:00")))
                value = assess_parking(MAP, [frame], object_id=object_id, bay_id=zone_id,
                    settings=self.settings["parking"], expected_run_id=w["run_id"], expected_state_version=frame["state_version"], **args)
                return value.geometry.support_status == "supported" and value.geometry.geometry_relation == "within"
            result["clearance_sustained"] = analysis.support_status == "supported" and self._suffix(frames, within, self.settings["clearance_ms"])
            result["metrics"].update(analysis.model_dump())
        elif impact_type == "approach_risk":
            # A paused synthetic run evaluates its frozen observation. Running
            # observations still expire in wall time; delivery lag remains checked.
            received = datetime.fromisoformat(frames[-1]["received_at"].replace("Z", "+00:00"))
            analysis_now = min(now, received) if w["run_status"] == "paused" else now
            analysis = analyze_approach(MAP, frames[-2:], current_sim_time_ms=w["sim_time_ms"], current_utc=analysis_now,
                recovery_required=w["recovery_required"], settings=self.settings["approach"])
            supported = analysis.status in ("risk_candidate", "clear_projection")
            result["support_status"] = "supported" if supported else analysis.status
            result["violation_candidate"] = any(c.vehicle_id == object_id for c in analysis.candidates)
            result["occupied"] = result["violation_candidate"]
            def clear_pair(frame):
                index = next(i for i, entry in enumerate(frames) if entry["observation_id"] == frame["observation_id"])
                if index == 0:
                    return False
                value = analyze_approach(MAP, frames[index-1:index+1], current_sim_time_ms=frame["sim_time_ms"],
                    current_utc=datetime.fromisoformat(frame["received_at"].replace("Z", "+00:00")),
                    recovery_required=w["recovery_required"], settings=self.settings["approach"])
                return value.status == "clear_projection"
            result["clearance_sustained"] = supported and analysis.status == "clear_projection" and self._suffix(frames, clear_pair, self.settings["clearance_ms"])
            result["metrics"].update(analysis.model_dump())
        else:
            result["support_status"] = "unsupported_geometry"
        return result
