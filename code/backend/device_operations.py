"""Server-checked synthetic device executions with durable checkpoint/results."""
from copy import deepcopy
import json
from types import SimpleNamespace

from agent.tools import validate_session
from backend.auth import ApiError
from backend.business import ident, encoded
from backend.knowledge import transaction
from contracts.devices import DeviceCommand
from simulator.world import FACILITY, utc_now


class DeviceOperations:
    def __init__(self, runtime):
        self.runtime = runtime

    def cancel_announcement(self, candidate, row):
        """Resolve an accepted announcement against its current device feedback.

        The caller owns the runtime lock and commits the candidate with the DB row.
        """
        from simulator.environment import apply_device_command

        broadcast = next((item for item in candidate["device_state"]["broadcasts"]
                          if item["operation_id"] == row["execution_id"]), None)
        result = json.loads(row["result_json"] or "{}")
        if broadcast is None:
            return "unknown", result | {"simulated_playback": "unknown", "cancel_reason": "broadcast_record_missing"}, False
        simulated, browser = broadcast["simulated_playback"], broadcast["browser_playback"]
        if simulated == "played" or browser == "played":
            return "succeeded", result | {"simulated_playback": simulated,
                                          "browser_playback": browser, "cancel_reason": "already_played"}, False
        if simulated == "unknown" or browser in {"pending", "unknown"} or broadcast["receipt"] != "accepted":
            return "unknown", result | {"simulated_playback": simulated,
                                        "browser_playback": browser, "cancel_reason": "result_needs_reconciliation"}, False
        if simulated == "failed":
            return "failed", result | {"simulated_playback": simulated,
                                       "browser_playback": browser, "cancel_reason": "already_failed"}, False
        if simulated == "cancelled":
            return "cancelled", result | {"simulated_playback": simulated,
                                          "browser_playback": browser}, False
        applied = apply_device_command(candidate, DeviceCommand(action="cancel_broadcast",
            operation_id=ident("device-cancel"), broadcast_operation_id=row["execution_id"]), now_utc=utc_now())
        if applied.outcome != "accepted":
            raise ApiError(409, "DEVICE_CHANGED", "방송 장치 상태가 바뀌었습니다.")
        return "cancelled", result | {"simulated_playback": "cancelled",
                                      "browser_playback": browser, "cancel_reason": applied.reason}, True

    async def execute(self, session, *, run_id, command_id, plan_id, action,
                      zone_id, message_id, knowledge_evidence, key, authenticate=None, step_id=None):
        from simulator.environment import apply_device_command
        r, b = self.runtime, self.runtime.business
        async with r.lock:
            if authenticate is not None:
                authenticate()
            validate_session(r, session)
            r.ensure_business_writable()
            if session.role not in ("owner", "test_operator"):
                raise ApiError(403, "FORBIDDEN", "장치 업무 권한이 필요합니다.")
            r.ensure_run(run_id)
            if action not in ("play_announcement", "set_entry_policy"):
                raise ApiError(422, "INVALID_TOOL_INPUT", "지원되는 가상 장치 업무를 확인하세요.")
            arguments = {"run_id": run_id, "command_id": command_id, "plan_id": plan_id,
                         "action": action, "zone_id": zone_id, "message_id": message_id,
                         "knowledge_evidence": knowledge_evidence.model_dump() if knowledge_evidence else None}
            if step_id is not None:
                arguments["step_id"] = step_id
            fingerprint, old = b.key(session.username, key, action, arguments)
            if old:
                return old
            policy = r.knowledge.current_policy(FACILITY)
            context = SimpleNamespace(facility_id=FACILITY, run_id=run_id, incident_id=None,
                command_id=command_id, plan_id=plan_id, policy_version=policy.policy_version)
            b.policy(context, action)
            plan = b.scoped("plans", plan_id, "plan_id")
            steps = json.loads(plan["steps_json"])
            linked_step = None
            if step_id is not None:
                matches = [step for step in steps if step.get("step_id") == step_id]
                if len(matches) != 1 or matches[0].get("tool") != action or matches[0].get("zone_id") != zone_id:
                    raise ApiError(409, "PLAN_STEP_CHANGED", "계획의 정확한 단계와 실행 범위를 확인하세요.")
                linked_step = matches[0]
                refs = linked_step.get("execution_ids", [])
                if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs) or len(refs) != len(set(refs)):
                    raise ApiError(409, "PLAN_STEP_CHANGED", "계획 단계의 실행 참조를 확인하세요.")
                latest = linked_step.get("execution_id")
                if latest is not None:
                    if not isinstance(latest, str):
                        raise ApiError(409, "PLAN_STEP_CHANGED", "계획 단계의 실행 참조를 확인하세요.")
                    if latest not in refs:
                        refs = refs + [latest]
                linked_step["execution_ids"] = refs
                for ref in refs:
                    prior = b.db.execute("SELECT status FROM executions WHERE execution_id=? AND plan_id=? AND command_id=? AND run_id=? AND facility_id=?",
                        (ref, plan_id, command_id, run_id, FACILITY)).fetchone()
                    if not prior or prior["status"] != "held":
                        raise ApiError(409, "STEP_ALREADY_EXECUTED", "수락되었거나 결과 확인이 필요한 단계는 다시 실행할 수 없습니다.")
            elif any(step.get("step_id") for step in steps):
                raise ApiError(409, "PLAN_STEP_REQUIRED", "명시적 계획 단계가 필요합니다.")
            user_command = b.scoped("commands", command_id, "command_id")
            goal = json.loads(user_command["normalized_goal_json"] or "{}")
            if not goal.get("confirmed") or goal.get("kind") not in ("closing", "zone_notice"):
                raise ApiError(409, "CONFIRMATION_REQUIRED", "구체화한 운영 목표의 확인이 필요합니다.")
            if action == "set_entry_policy" and goal["kind"] != "closing":
                raise ApiError(403, "TOOL_NOT_ALLOWED", "확인된 마감 목표에만 입차 제한이 허용됩니다.")
            if action == "play_announcement" and (message_id != ("closing_notice" if goal["kind"] == "closing" else "no_litter_notice")
                    or zone_id not in ({"announcement-a", "announcement-b"} if goal["kind"] == "closing" else {"announcement-a"})):
                raise ApiError(409, "PLAN_STEP_CHANGED", "확인된 목표의 방송 범위와 문구만 실행할 수 있습니다.")
            if plan["policy_version"] != policy.policy_version:
                raise ApiError(409, "POLICY_CHANGED", "계획의 현재 정책을 다시 확인하세요.")
            if r.world["recovery_required"] or r.failure:
                raise ApiError(409, "RECOVERY_REQUIRED", "장치 실행 전 회차 복구를 확인하세요.")
            purpose = ("zone_notice" if goal["kind"] == "zone_notice" else "closing_notice") if action == "play_announcement" else "closing_entry"
            r.knowledge.validate_evidence(session.username, FACILITY, run_id, knowledge_evidence,
                tool_name=action, purpose=purpose)
            candidate = deepcopy(r.world)
            eid = ident("execution")
            if action == "play_announcement":
                command = DeviceCommand(action="broadcast", operation_id=eid, zone_id=zone_id, message_id=message_id)
            else:
                broadcasts = candidate["device_state"]["broadcasts"]
                prior_rows = b.db.execute("SELECT * FROM executions WHERE plan_id=? AND policy_version=? AND tool_name='play_announcement' AND status='succeeded' ORDER BY rowid", (plan_id, policy.policy_version)).fetchall()
                played_zones = {json.loads(row["payload_json"])["zone_id"] for row in prior_rows
                    if json.loads(row["payload_json"]).get("message_id") == "closing_notice"}
                if not {"announcement-a", "announcement-b"} <= played_zones:
                    raise ApiError(409, "DEPENDENCY_NOT_READY", "전체 구역 마감 방송의 재생 확인이 필요합니다.")
                for prior_row in prior_rows:
                    record = next((v for v in broadcasts if v["operation_id"] == prior_row["execution_id"]), None)
                    if not record or record["receipt"] != "accepted" or record["simulated_playback"] != "played":
                        raise ApiError(409, "DEPENDENCY_NOT_READY", "현재 가상 장치의 구역별 재생 결과를 확인하세요.")
                prior = prior_rows[-1] if prior_rows else None
                broadcast_id = json.loads(prior["result_json"])["operation_id"] if prior else None
                broadcast = next((v for v in broadcasts if v["operation_id"] == broadcast_id), None)
                if not broadcast or broadcast["receipt"] != "accepted" or broadcast["simulated_playback"] != "played":
                    raise ApiError(409, "DEPENDENCY_NOT_READY", "같은 계획의 방송 재생 확인이 먼저 필요합니다.")
                entry = next(g for g in candidate["device_state"]["gates"] if g["direction"] == "entry")
                frame = candidate["observation"]
                exit_id = next(g["gate_id"] for g in candidate["device_state"]["gates"] if g["direction"] == "exit")
                exit_device = next((d for d in frame["devices"] if d["device_id"] == exit_id), None)
                outbound_clear = bool(frame["coverage"] == "complete"
                    and candidate["sim_time_ms"] - frame["sim_time_ms"] <= 400
                    and exit_device and exit_device["physical_state"] == "open"
                    and exit_device["quality"]["visibility"] == "visible"
                    and not exit_device["quality"]["missing_fields"]
                    and exit_device["obstacle_detected"] is False)
                command = DeviceCommand(action="set_entry_policy", operation_id=eid,
                    gate_id=entry["gate_id"], expected_version=entry["resource_version"], target="deny",
                    broadcast_operation_id=broadcast_id, outbound_clear=outbound_clear)
            applied = apply_device_command(candidate, command, now_utc=utc_now())
            status = "accepted" if applied.outcome == "accepted" and action == "play_announcement" else (
                "succeeded" if applied.outcome == "accepted" else "unknown" if applied.outcome == "unknown" else "held")
            result = {"operation_id": eid, "receipt": applied.outcome, "reason": applied.reason,
                      "device_version": applied.state.version, "mode": "synthetic_demo"}
            if linked_step is not None:
                result["step_id"] = step_id
            with transaction(b.db):
                b.insert("executions", execution_id=eid, facility_id=FACILITY, run_id=run_id,
                    plan_id=plan_id, command_id=command_id, incident_id=None, tool_name=action,
                    target_ref=zone_id or command.gate_id, requester_ref=session.username,
                    idempotency_key=key, payload_hash=fingerprint, payload_json=encoded(arguments), status=status,
                    based_on_state_version=r.world["state_version"], policy_version=policy.policy_version,
                    mode="synthetic_demo", result_json=encoded(result),
                    knowledge_evidence_json=knowledge_evidence.model_dump_json() if knowledge_evidence else None)
                if linked_step is not None:
                    linked_step.setdefault("execution_ids", []).append(eid)
                    linked_step["execution_id"] = eid
                    b.changed("plans", "plan_id", plan_id, run_id, steps_json=encoded(steps))
                r.store.commit(candidate, r.event(candidate, "run.updated"))
                b.emit(run_id, "execution.updated", execution_id=eid, resource_version=1)
                b.audit(session.username, action, eid, run_id, status, applied.reason)
                view = b.execution_view(b.scoped("executions", eid, "execution_id"))
                b.save_key(session.username, key, fingerprint, view)
            r.world = candidate
            return view

    def reconcile(self):
        r, b = self.runtime, self.runtime.business
        if not r.world or r.failure:
            return
        for row in b.db.execute("SELECT * FROM executions WHERE run_id=? AND tool_name='play_announcement' AND status='accepted'", (r.world["run_id"],)).fetchall():
            broadcast = next((v for v in r.world["device_state"]["broadcasts"] if v["operation_id"] == row["execution_id"]), None)
            if not broadcast or broadcast["simulated_playback"] == "pending":
                continue
            status = "succeeded" if broadcast["simulated_playback"] == "played" else "unknown" if broadcast["simulated_playback"] == "unknown" else "failed"
            with transaction(b.db):
                b.changed("executions", "execution_id", row["execution_id"], row["run_id"], status=status,
                    result_json=encoded(json.loads(row["result_json"]) | {"simulated_playback": broadcast["simulated_playback"],
                        "browser_playback": broadcast["browser_playback"]}))
