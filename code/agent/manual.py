"""Explicit operator walkthrough; never described as model reasoning."""
import json
from typing import Literal

from backend.auth import ApiError
from backend.business import CLOSED, encoded, ident, instant, later
from backend.knowledge import transaction
from agent.tools import call_read_tool
from contracts.models import Contract, Identifier
from simulator.world import FACILITY, digest


class ManualS1(Contract):
    run_id: Identifier
    action: Literal["notify", "recheck", "review_timeout"]
    incident_id: Identifier | None = None


def manual_s1(runtime, session, body, key):
    """Called with Runtime.lock; all accepted steps commit atomically."""
    if session.role != "test_operator":
        raise ApiError(403, "FORBIDDEN", "수동 시험은 test_operator만 가능합니다.")
    runtime.ensure_run(body.run_id)
    b, world = runtime.business, runtime.world
    if world["run_status"] != "paused":
        raise ApiError(409, "PAUSE_REQUIRED", "수동 업무 시험 전에 일시정지하세요.")
    if world["recovery_required"] or runtime.failure:
        raise ApiError(409, "RECOVERY_REQUIRED", "복구 확인 후 새 관측을 확보하세요.")
    fingerprint, old = b.key(session.username, key, "manual_s1a", body.model_dump())
    if old:
        for field in ("execution", "report"):
            if old.get(field):
                old[field] = b.execution_view(b.scoped("executions", old[field]["execution_id"], "execution_id"))
        return old
    task = runtime.read_task(session, body.run_id)
    policy = call_read_tool(runtime, session, "get_operating_policy", {"facility_id": FACILITY}, task)
    analysis = b.analysis()
    common = {"facility_id": analysis.facility_id, "run_id": body.run_id,
              "based_on_state_version": world["state_version"], "policy_version": policy["policy_version"]}
    step_key = digest({"request_key": key, "arguments": fingerprint})[:48]
    def tool(name, args, suffix):
        task.consume(session, name)
        return b.execute(session, name, common | args, "manual-" + step_key + "-" + suffix)

    def report_once(iid, reason, trigger, summary):
        report_key = "manual-report-" + digest([iid, reason, trigger])[:48]
        existing = b.db.execute("SELECT response_json FROM business_requests WHERE facility_id=? AND requester_ref=? AND key=?", (analysis.facility_id, session.username, report_key)).fetchone()
        if existing:
            old_report = json.loads(existing[0])
            return b.execution_view(b.scoped("executions", old_report["execution_id"], "execution_id"))
        task.consume(session, "report_to_owner")
        return b.execute(session, "report_to_owner", common | {"incident_id": iid, "reason_code": reason, "summary": summary}, report_key)

    result = {"mode": "manual", "action": body.action, "run_id": body.run_id,
              "policy_version": policy["policy_version"], "knowledge": None, "execution": None, "report": None}
    with transaction(b.db):
        if body.action == "notify":
            candidates = [o.object_id for o in analysis.metrics.objects if o.stationary_candidate]
            if analysis.support_status != "supported" or len(candidates) != 1:
                raise ApiError(409, "OBSERVATION_NOT_READY", "현재 서측 통로의 유일한 정지 차단 대상을 확인하세요.")
            object_id = candidates[0]
            recorded = tool("create_or_update_incident", {"primary_object_id": object_id, "status": "active",
                "impacts": [{"type": "aisle_obstruction", "zone_id": "aisle-west", "object_id": object_id}],
                "evidence_ids": analysis.observation_ids, "reason_summary": "수동 시험: 공개 관측의 지속 통로 차단과 정지"}, "incident")
            iid = recorded["result"]["incident_id"]
            row = b.scoped("incidents", iid, "incident_id")
            result["incident_id"] = iid
            knowledge = call_read_tool(runtime, session, "search_operating_knowledge", {
                "facility_id": analysis.facility_id, "run_id": body.run_id,
                "query": "통로 차단 이동 요청과 미응답", "topic": "parking_order", "zone_id": "aisle-west"}, task)
            result["knowledge"] = knowledge
            pid = ident("plan")
            b.insert("plans", plan_id=pid, facility_id=analysis.facility_id, run_id=body.run_id, incident_id=iid,
                command_id=None, trigger_followup_id=None, steps_json=encoded([{"tool": "notify_vehicle_user", "status": "proposed"}]),
                model_ref="manual_s1a", policy_version=policy["policy_version"], budget_json=encoded({"tool_calls": 16, "wall_seconds": 30}), status="active")
            result["plan_id"] = pid
            try:
                if knowledge["status"] != "matched":
                    code = {"unavailable": "KNOWLEDGE_UNAVAILABLE", "conflict": "KNOWLEDGE_CONFLICT"}.get(knowledge["status"], "KNOWLEDGE_REQUIRED")
                    raise ApiError(503 if code == "KNOWLEDGE_UNAVAILABLE" else 409, code, "현재 운영 근거를 확인할 수 없습니다.")
                task.consume(session, "resolve_vehicle_recipient")
                recipient = b.resolve_recipient(object_id)
                previous = b.db.execute("SELECT contact_sequence FROM notifications WHERE incident_id=? AND purpose='move_request' ORDER BY contact_sequence DESC LIMIT 1", (iid,)).fetchone()
                sequence = previous[0]+1 if previous else 1
                result["execution"] = tool("notify_vehicle_user", {"incident_id": iid, "plan_id": pid,
                    "recipient_ref": recipient["recipient_ref"], "expected_resource_version": row["resource_version"],
                    "template_args": {"zone_label": "서측 통로"}, "contact_sequence": sequence,
                    "knowledge_evidence": {"retrieval_id": knowledge["retrieval_id"], "reference_ids": [r["reference_id"] for r in knowledge["references"]]}}, "notice")
                result["status"] = "accepted"
                due = later(b.clock(), policy["execution_rules"]["response_timeout_wall_ms"])
                result["followup"] = tool("request_followup", {"incident_id": iid, "clock": "wall", "due_at": due,
                    "condition": "response_timeout", "max_attempts": 1}, "response-followup")
                b.changed("plans", "plan_id", pid, body.run_id, steps_json=encoded([
                    {"tool": "notify_vehicle_user", "execution_id": result["execution"]["execution_id"]},
                    {"tool": "request_followup", "execution_id": result["followup"]["execution_id"]}]))
            except ApiError as exc:
                b.changed("plans", "plan_id", pid, body.run_id, status="held")
                result["status"], result["reason_code"] = "held", exc.code
                # Independent reporting remains available without RAG success.
                result["report"] = report_once(iid, exc.code, ["held", row["resource_version"]],
                    "수동 시험: 이동 요청 조건을 확인할 수 없어 보류했습니다.")
        else:
            if not body.incident_id:
                raise ApiError(422, "INCIDENT_REQUIRED", "확인할 사건을 선택하세요.")
            row = b.scoped("incidents", body.incident_id, "incident_id")
            if row["run_id"] != body.run_id:
                raise ApiError(409, "CONTEXT_CHANGED", "현재 회차의 사건을 선택하세요.")
            iid = row["incident_id"]
            result["incident_id"] = iid
            if row["status"] in CLOSED:
                result["status"] = row["status"]
            elif body.action == "review_timeout":
                notice = b.db.execute("SELECT * FROM notifications WHERE incident_id=? AND purpose='move_request' ORDER BY contact_sequence DESC LIMIT 1", (iid,)).fetchone()
                if not notice or notice["delivery_status"] not in ("channel_accepted", "client_received"):
                    raise ApiError(409, "DELIVERY_UNRESOLVED", "실제 채널 접수 결과부터 확인하세요.")
                responses = b.db.execute("SELECT response,response_id FROM notification_responses WHERE notification_id=? ORDER BY rowid DESC LIMIT 1", (notice["notification_id"],)).fetchone()
                overall_due = (instant(b.clock())-instant(row["created_at"])).total_seconds()*1000 >= policy["execution_rules"]["overall_timeout_wall_ms"]
                if responses and responses[0] in ("acknowledged", "will_move") and not overall_due:
                    result["status"] = "awaiting_observation"
                elif overall_due or responses or (notice["response_due_at"] and instant(b.clock()) >= instant(notice["response_due_at"])):
                    reason = "OVERALL_TIMEOUT" if overall_due else responses[0] if responses else "RESPONSE_TIMEOUT"
                    # One report per immutable response/notification deadline,
                    # even when a caller supplies a different outer request key.
                    result["report"] = report_once(iid, reason,
                        [notice["notification_id"], responses["response_id"] if responses else "deadline"],
                        "수동 시험: 차주 응답 또는 기한 도래를 확인했습니다. 물리 문제는 별도 관측이 필요합니다.")
                    result["status"] = "escalated_review"
                else:
                    raise ApiError(409, "RESPONSE_NOT_DUE", "응답 기한이 아직 도래하지 않았습니다.")
            else:
                if analysis.support_status != "supported":
                    status = "needs_review"
                elif analysis.metrics.clearance_sustained:
                    status = "resolved"
                elif analysis.metrics.passage == "blocked":
                    status = "monitoring"
                else:
                    result["status"] = "awaiting_sustained_clearance"
                    b.save_key(session.username, key, fingerprint, result)
                    return result
                result["execution"] = tool("create_or_update_incident", {"incident_id": iid,
                    "expected_resource_version": row["resource_version"], "primary_object_id": row["primary_object_id"],
                    "status": status, "impacts": [{"type": "aisle_obstruction", "zone_id": "aisle-west"}],
                    "evidence_ids": analysis.observation_ids, "reason_summary": "수동 시험: 최신 공개 관측 후속 확인"}, "recheck")
                result["status"] = status
                if status == "resolved":
                    for plan in b.db.execute("SELECT plan_id FROM plans WHERE incident_id=? AND status='active'", (iid,)).fetchall():
                        b.changed("plans", "plan_id", plan[0], body.run_id, status="completed")
        b.audit(session.username, "manual_s1a", result.get("incident_id", body.run_id), body.run_id, result["status"], body.action)
        b.save_key(session.username, key, fingerprint, result)
        return result
