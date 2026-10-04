"""Durable, server-authorized business operations for the synthetic facility.

SQLite is used only on the runtime thread. Channel I/O starts after commit and
outside the world lock. No notification/response mutates simulator actors.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from pydantic import ValidationError

from backend.auth import ApiError
from backend.knowledge import transaction
from contracts.business import IncidentImpact, IncidentInput, NotifyVehicle, ReportOwner, FollowupInput
from simulator.spatial import analyze_spatial_context
from simulator.world import FACILITY, MAP, digest, utc_now

INPUTS = {"create_or_update_incident": IncidentInput, "notify_vehicle_user": NotifyVehicle,
          "report_to_owner": ReportOwner, "request_followup": FollowupInput}
CLOSED = {"resolved", "closed_no_issue", "closed_false_positive"}
TERMINAL = {"succeeded", "failed", "held", "cancelled", "unknown"}
S1_SCOPES = (("aisle_obstruction", "aisle-west"), ("exit_blocked", "B01"), ("bay_intrusion", "B01"))
S1_TYPES = {kind for kind, _ in S1_SCOPES}


def ident(prefix):
    return prefix + "-" + uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def later(now, milliseconds):
    return (instant(now) + timedelta(milliseconds=milliseconds)).isoformat().replace("+00:00", "Z")


class WebInbox:
    """Local browser inbox, not email/SMS and not a browser receipt."""
    mode = "live"
    name = "local_web_inbox"

    async def send(self, notification_id, message):
        # Durable notification is already committed; publish eligibility is
        # committed by finish_delivery. No external side effect in this adapter.
        return "accepted"

    def reconcile(self, notification):
        return "accepted" if notification["delivery_status"] in ("channel_accepted", "client_received") else "not_sent"


class MockChannel:
    """Explicit simulated transport for isolated tests; selected by trusted code."""
    mode = "simulated"
    name = "simulated_inbox"

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or ["accepted"])
        self.calls = []

    async def send(self, notification_id, message):
        self.calls.append(notification_id)
        return self.outcomes.pop(0) if self.outcomes else "accepted"

    def reconcile(self, notification):
        return "unknown"


class Business:
    def __init__(self, runtime, channel=None, clock=utc_now):
        self.runtime = runtime
        self.db = runtime.store.db
        self.registry = runtime.store.registry
        self.channel = channel or WebInbox()
        self.clock = clock
        self.recover()

    def insert(self, table, **values):
        # All table/column names originate in this module, never in tool input.
        columns = ",".join(values)
        self.db.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    def scoped(self, table, resource_id, column):
        row = self.db.execute(f"SELECT * FROM {table} WHERE {column}=? AND facility_id=?", (resource_id, FACILITY)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "현재 권한에서 자료를 찾을 수 없습니다.")
        return row

    def audit(self, actor, action, target, run, outcome="succeeded", reason="applied"):
        self.insert("audit_events", audit_id=ident("audit"), facility_id=FACILITY, run_id=run,
                    actor_ref=actor, action=action, target_ref=target, outcome=outcome,
                    reason_code=reason, occurred_at=self.clock(), correlation_id=ident("correlation"))

    def emit(self, run, kind, **payload):
        self.insert("outbox_events", event_id=ident("outbox"), facility_id=FACILITY, run_id=run,
                    event_type=kind, payload_json=encoded(payload), dispatch_status="pending")

    def changed(self, table, column, resource_id, run, **fields):
        assignments = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE {table} SET {assignments},updated_at=?,resource_version=resource_version+1 WHERE {column}=?",
                        (*fields.values(), self.clock(), resource_id))
        version = self.scoped(table, resource_id, column)["resource_version"]
        self.emit(run, table.rstrip("s") + ".updated", **{column: resource_id, "resource_version": version})

    def analysis(self):
        w = self.runtime.world
        return analyze_spatial_context(MAP, w.get("observation_history") or [w["observation"]],
            current_sim_time_ms=w["sim_time_ms"], run_status=w["run_status"],
            recovery_required=w["recovery_required"], observation_ready=bool(w.get("observation_history")))

    def impact_assessment(self, impact_type, object_id, zone_id):
        """Obtain a current server analysis, never a model assertion or fixture truth."""
        if impact_type == "aisle_obstruction":
            analysis = self.analysis()
            trustworthy = analysis.support_status == "supported" and analysis.quality.freshness == "fresh"
            return {"support_status": "supported" if trustworthy else "insufficient_data",
                    "violation_candidate": trustworthy and any(o.object_id == object_id and o.stationary_candidate
                                                              for o in analysis.metrics.objects),
                    "clearance_sustained": trustworthy and analysis.metrics.clearance_sustained,
                    "occupied": trustworthy and object_id in analysis.metrics.occupied_object_ids,
                    "observation_ids": analysis.observation_ids,
                    "state_version": self.runtime.world["state_version"],
                    "metrics": analysis.metrics.model_dump()}
        provider = getattr(self.runtime, "operating_analysis", None)
        if provider is None:
            raise ApiError(409, "UNSUPPORTED_IMPACT", "이 영향의 관측 분석이 연결되지 않았습니다.")
        result = provider(impact_type, object_id, zone_id)
        if (not isinstance(result, dict) or result.get("state_version") != self.runtime.world["state_version"]
                or result.get("observation_ids") is None or not isinstance(result.get("violation_candidate"), bool)
                or not isinstance(result.get("clearance_sustained"), bool)):
            raise ApiError(409, "OBSERVATION_NOT_READY", "현재 영향 분석을 확인할 수 없습니다.")
        return result

    def policy(self, args, name):
        if args.facility_id != FACILITY:
            raise ApiError(404, "NOT_FOUND", "시설을 찾을 수 없습니다.")
        self.runtime.ensure_run(args.run_id)
        if self.runtime.failure:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "실행 상태를 확인할 수 없습니다.")
        try:
            p = self.runtime.knowledge.current_policy(FACILITY)
        except (ValueError, OSError):
            raise ApiError(503, "POLICY_UNAVAILABLE", "현재 정책을 확인할 수 없습니다.") from None
        if p.policy_version != args.policy_version:
            raise ApiError(409, "POLICY_CHANGED", "현재 정책 버전으로 다시 검토하세요.")
        if not p.execution_rules or name not in p.execution_rules.allowed_tools:
            raise ApiError(403, "TOOL_NOT_ALLOWED", "현재 정책이 실행을 허용하지 않습니다.")
        for table, column, value in [("incidents", "incident_id", args.incident_id),
                                     ("commands", "command_id", args.command_id), ("plans", "plan_id", args.plan_id)]:
            if value:
                row = self.scoped(table, value, column)
                if row["run_id"] != args.run_id:
                    raise ApiError(409, "CONTEXT_CHANGED", "현재 실행 회차의 업무를 사용하세요.")
                if table == "commands" and (row["aggregate_status"] == "cancelled" or row["cancellation_requested_at"]):
                    raise ApiError(409, "CANCELLED", "취소된 지시입니다.")
                if table == "plans" and row["status"] != "active":
                    raise ApiError(409, "PLAN_NOT_ACTIVE", "활성 실행 계획이 필요합니다.")
                if table == "plans" and (row["incident_id"], row["command_id"]) != (args.incident_id, args.command_id):
                    raise ApiError(409, "CONTEXT_CHANGED", "계획과 실행 대상의 관계가 다릅니다.")
                if table == "plans" and not any(step.get("tool") == name for step in json.loads(row["steps_json"])):
                    raise ApiError(409, "PLAN_STEP_CHANGED", "현재 계획에 없는 실행 단계입니다.")
        return p

    def resolve_recipient(self, object_id):
        w = self.runtime.world
        now = self.clock()
        rows = self.db.execute("""SELECT m.*,u.user_id,u.username,vu.valid_from AS owner_from,vu.valid_until AS owner_until FROM object_mappings m
            JOIN vehicles v USING(registered_vehicle_id) JOIN vehicle_users vu USING(registered_vehicle_id)
            JOIN users u USING(user_id) JOIN memberships ms ON ms.user_id=u.user_id AND ms.facility_id=m.facility_id
            WHERE m.facility_id=? AND m.run_id=? AND m.object_id=? AND m.mapping_status='verified'
            AND m.valid_from_sim_time_ms<=? AND (m.valid_to_sim_time_ms IS NULL OR m.valid_to_sim_time_ms>?)
            AND v.active=1 AND u.disabled_at IS NULL AND ms.role='driver' AND ms.revoked_at IS NULL
            AND julianday(vu.valid_from)<=julianday(?) AND (vu.valid_until IS NULL OR julianday(vu.valid_until)>julianday(?))""",
            (FACILITY, w["run_id"], object_id, w["sim_time_ms"], w["sim_time_ms"], now, now)).fetchall()
        if len(rows) != 1:
            raise ApiError(409, "RECIPIENT_UNVERIFIED", "유일하고 유효한 등록 차주를 확인할 수 없습니다.")
        row = rows[0]
        binding = {k: row[k] for k in ("mapping_id", "object_id", "registered_vehicle_id", "mapping_status", "mapping_source", "valid_from_sim_time_ms", "valid_to_sim_time_ms", "user_id", "owner_from", "owner_until")}
        return {"recipient_ref": "recipient-" + digest(binding)[:40],
                "object_id": object_id, "mapping_id": row["mapping_id"], "user_id": row["user_id"],
                "registered_vehicle_id": row["registered_vehicle_id"], "frame_sim": w["sim_time_ms"], "recorded_at": now}

    def open_s1_incident(self, object_id):
        rows = self.db.execute("""SELECT i.* FROM incidents i
            WHERE i.facility_id=? AND i.run_id=? AND i.primary_object_id=?
            AND i.status NOT IN ('resolved','closed_no_issue','closed_false_positive')
            AND EXISTS (SELECT 1 FROM incident_impacts p WHERE p.incident_id=i.incident_id
                AND p.type IN ('aisle_obstruction','exit_blocked','bay_intrusion'))""",
            (FACILITY, self.runtime.world["run_id"], object_id)).fetchall()
        if len(rows) > 1:
            raise ApiError(409, "INCIDENT_REVIEW_REQUIRED", "동일 대상의 복수 미종료 사건을 검토하세요.")
        return rows[0] if rows else None

    def current_s1_candidates(self, object_id):
        values = [(IncidentImpact(type=kind, zone_id=zone, object_id=object_id),
                   self.impact_assessment(kind, object_id, zone)) for kind, zone in S1_SCOPES]
        return [(impact, value) for impact, value in values
                if value["support_status"] == "supported" and value["violation_candidate"]]

    def incident_assessments(self, row, *, include_candidates=True):
        impacts = {(entry["type"], entry["zone_id"]): IncidentImpact(
            type=entry["type"], zone_id=entry["zone_id"], object_id=row["primary_object_id"])
            for entry in self.db.execute("SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (row["incident_id"],))}
        if include_candidates and impacts and all(kind in S1_TYPES for kind, _ in impacts):
            for impact, _ in self.current_s1_candidates(row["primary_object_id"]):
                impacts.setdefault((impact.type, impact.zone_id), impact)
        return [(impact, self.impact_assessment(impact.type, row["primary_object_id"], impact.zone_id))
                for impact in impacts.values()]

    def previous_vehicle_contact(self, incident_id, user_id):
        return self.db.execute("""SELECT * FROM notifications WHERE incident_id=?
            AND recipient_user_id=? AND purpose='move_request' ORDER BY contact_sequence DESC,rowid DESC LIMIT 1""",
            (incident_id, user_id)).fetchone()

    def notification_guard(self, args, username):
        if not args.incident_id:
            raise ApiError(422, "INCIDENT_REQUIRED", "차량 이동 요청에는 사건이 필요합니다.")
        row = self.scoped("incidents", args.incident_id, "incident_id")
        if row["resource_version"] != args.expected_resource_version:
            raise ApiError(409, "RESOURCE_CHANGED", "사건 버전이 바뀌었습니다.")
        if row["status"] not in ("active", "monitoring"):
            raise ApiError(409, "INCIDENT_NOT_ACTIVE", "연락 가능한 사건 상태를 확인하세요.")
        impacts = self.db.execute("SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (args.incident_id,)).fetchall()
        assessments = [self.impact_assessment(impact["type"], row["primary_object_id"], impact["zone_id"]) for impact in impacts]
        if (not assessments or not all(a["support_status"] == "supported" for a in assessments)
                or not any(a["violation_candidate"] for a in assessments)):
            raise ApiError(409, "OBSERVATION_NOT_READY", "현재 관측으로 이용 방해 조건을 재확인할 수 없습니다.")
        recipient = self.resolve_recipient(row["primary_object_id"])
        if recipient["recipient_ref"] != args.recipient_ref:
            raise ApiError(409, "RECIPIENT_CHANGED", "등록 차주 연결이 바뀌었습니다.")
        p = self.policy(args, "notify_vehicle_user")
        if (instant(self.clock())-instant(row["created_at"])).total_seconds()*1000 >= p.execution_rules.overall_timeout_wall_ms:
            raise ApiError(409, "INCIDENT_CONTACT_EXPIRED", "사건의 전체 연락 관찰 기한을 지났습니다.")
        if args.contact_sequence > p.execution_rules.contact_max_sequence:
            raise ApiError(429, "CONTACT_LIMIT", "연락 횟수 한도입니다.")
        self.runtime.knowledge.validate_evidence(username, FACILITY, args.run_id, args.knowledge_evidence,
                                                tool_name="notify_vehicle_user", purpose="move_request")
        return recipient, p

    def key(self, username, key, action, args):
        if not key or not 1 <= len(key) <= 128:
            raise ApiError(400, "IDEMPOTENCY_REQUIRED", "1~128자 요청 키가 필요합니다.")
        fingerprint = digest({"action": action, "arguments": args})
        old = self.db.execute("SELECT * FROM business_requests WHERE facility_id=? AND requester_ref=? AND key=?",
                              (FACILITY, username, key)).fetchone()
        if old and old["argument_hash"] != fingerprint:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 키에 다른 요청을 보낼 수 없습니다.")
        if old:
            result = json.loads(old["response_json"])
            if "execution_id" in result:
                result = self.execution_view(self.scoped("executions", result["execution_id"], "execution_id"))
            return fingerprint, result
        return fingerprint, None

    def save_key(self, username, key, fingerprint, result):
        self.insert("business_requests", facility_id=FACILITY, requester_ref=username, key=key,
                    argument_hash=fingerprint, response_json=encoded(result))

    def execution_view(self, row):
        return {"execution_id": row["execution_id"], "status": row["status"], "mode": row["mode"],
                "resource_version": row["resource_version"], "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": {"code": row["error_code"]} if row["error_code"] else None}

    def execute(self, session, name, arguments, key):
        if self.runtime.world and self.runtime.world.get("replay_state") is not None:
            raise ApiError(409, "REPLAY_INPUT_REJECTED", "기록 재생 중에는 새 업무 실행을 받지 않습니다.")
        if session.role not in ("owner", "test_operator"):
            raise ApiError(403, "FORBIDDEN", "업무 도구 실행 권한이 없습니다.")
        try:
            args = INPUTS[name].model_validate(arguments)
        except (KeyError, ValidationError):
            raise ApiError(422, "INVALID_TOOL_INPUT", "등록 도구와 인자를 확인하세요.") from None
        fingerprint, old = self.key(session.username, key, name, args.model_dump())
        if old:
            return old
        p = self.policy(args, name)
        purpose = {"create_or_update_incident": "incident_record", "report_to_owner": "incident_report",
                   "request_followup": getattr(args, "condition", ""), "notify_vehicle_user": "move_request"}[name]
        self.runtime.knowledge.validate_evidence(session.username, FACILITY, args.run_id, args.knowledge_evidence,
                                                tool_name=name, purpose=purpose)
        with transaction(self.db):
            execution_id = ident("execution")
            private = {"request_role": session.role}
            if name == "create_or_update_incident":
                result = self.incident(args)
                args.incident_id = result["incident_id"]
            elif name == "request_followup":
                result = self.followup(args, session.username, p)
            else:
                if name == "notify_vehicle_user":
                    recipient, p = self.notification_guard(args, session.username)
                    private.update(recipient)
                    previous = self.previous_vehicle_contact(args.incident_id, recipient["user_id"])
                    sequence = previous["contact_sequence"] + 1 if previous else 1
                    if args.contact_sequence != sequence:
                        raise ApiError(409, "DUPLICATE_CONTACT", "다음 연락 순서만 접수할 수 있습니다.")
                    if previous:
                        if (instant(self.clock()) - instant(previous["created_at"])).total_seconds()*1000 < p.execution_rules.contact_interval_wall_ms:
                            raise ApiError(429, "CONTACT_INTERVAL", "연락 간격을 기다리세요.")
                        if previous["delivery_status"] in ("queued", "unknown"):
                            raise ApiError(409, "DELIVERY_UNRESOLVED", "이전 발송의 결과를 먼저 확인하세요.")
                        if self.db.execute("SELECT 1 FROM notification_responses WHERE notification_id=?", (previous["notification_id"],)).fetchone():
                            raise ApiError(409, "RESPONSE_REVIEW_REQUIRED", "이미 받은 차주 응답을 검토하세요.")
                    recipient_id, vehicle, template = recipient["user_id"], recipient["registered_vehicle_id"], args.template_id
                    message = {"text": f"{args.template_args.zone_label} 이용을 위해 가상 차량을 이동해 주세요.",
                               "zone_label": args.template_args.zone_label, "synthetic_data": True}
                    sequence = args.contact_sequence
                else:
                    if not args.incident_id and not args.command_id:
                        raise ApiError(422, "CONTEXT_REQUIRED", "보고에는 사건 또는 지시가 필요합니다.")
                    owners = self.db.execute("SELECT u.user_id FROM users u JOIN memberships m USING(user_id) WHERE m.facility_id=? AND m.role='owner' AND m.revoked_at IS NULL AND u.disabled_at IS NULL", (FACILITY,)).fetchall()
                    if len(owners) != 1:
                        raise ApiError(409, "RECIPIENT_UNVERIFIED", "유일한 소유자를 확인할 수 없습니다.")
                    recipient_id, vehicle, template = owners[0][0], None, "owner_report_v1"
                    private["user_id"] = recipient_id
                    message = {"summary": args.summary, "reason_code": args.reason_code, "synthetic_data": True}
                    sequence = self.db.execute("SELECT count(*) FROM notifications WHERE context_key=? AND purpose='owner_report'", (args.incident_id or args.command_id,)).fetchone()[0]+1
                result = {"notification_id": ident("notification"), "delivery_status": "queued"}
            payload = args.model_dump()
            payload["_server"] = private
            self.insert("executions", execution_id=execution_id, facility_id=FACILITY, run_id=args.run_id,
                plan_id=args.plan_id, incident_id=args.incident_id, command_id=args.command_id, tool_name=name,
                target_ref=args.incident_id or args.command_id or result.get("followup_id", "facility"), requester_ref=session.username,
                idempotency_key=key, payload_hash=fingerprint, payload_json=encoded(payload),
                status="accepted" if name in ("notify_vehicle_user", "report_to_owner") else "succeeded",
                based_on_state_version=args.based_on_state_version, expected_resource_version=getattr(args, "expected_resource_version", None),
                policy_version=p.policy_version, mode=self.channel.mode if name in ("notify_vehicle_user", "report_to_owner") else "synthetic_demo",
                result_json=encoded(result), knowledge_evidence_json=args.knowledge_evidence.model_dump_json() if args.knowledge_evidence else None)
            if name in ("notify_vehicle_user", "report_to_owner"):
                self.insert("notifications", notification_id=result["notification_id"], facility_id=FACILITY, run_id=args.run_id,
                    execution_id=execution_id, incident_id=args.incident_id, command_id=args.command_id,
                    context_key=args.incident_id or args.command_id, registered_vehicle_id=vehicle, recipient_user_id=recipient_id,
                    purpose="move_request" if name == "notify_vehicle_user" else "owner_report", contact_sequence=sequence,
                    delivery_status="queued", message_template=template, message_json=encoded(message), mode=self.channel.mode)
            self.emit(args.run_id, "execution.updated", execution_id=execution_id, resource_version=1)
            self.audit(session.username, name, execution_id, args.run_id)
            view = self.execution_view(self.scoped("executions", execution_id, "execution_id"))
            self.save_key(session.username, key, fingerprint, view)
            return view

    def incident(self, args):
        if any(i.object_id not in (None, args.primary_object_id) for i in args.impacts):
            raise ApiError(422, "IMPACT_TARGET_CHANGED", "사건 대상과 영향 대상이 다릅니다.")
        if any(i.type == "aisle_obstruction" and i.zone_id != "aisle-west" for i in args.impacts):
            raise ApiError(422, "UNSUPPORTED_IMPACT", "서측 통로 영향 구역을 확인하세요.")
        operating = all(i.type in S1_TYPES for i in args.impacts)
        dedup = ("aisle-west:" + args.primary_object_id if len(args.impacts) == 1 and args.impacts[0].type == "aisle_obstruction"
                 else digest([args.primary_object_id, sorted((i.type, i.zone_id) for i in args.impacts)])[:48])
        old = None
        if args.incident_id:
            old = self.scoped("incidents", args.incident_id, "incident_id")
            if old["resource_version"] != args.expected_resource_version or old["primary_object_id"] != args.primary_object_id:
                raise ApiError(409, "RESOURCE_CHANGED", "사건 버전이나 대상이 바뀌었습니다.")
            if old["status"] in CLOSED:
                raise ApiError(409, "INCIDENT_CLOSED", "종료 사건은 다시 변경하지 않습니다.")
            if operating:
                current = self.open_s1_incident(args.primary_object_id)
                if current is not None and current["incident_id"] != old["incident_id"]:
                    raise ApiError(409, "INCIDENT_REVIEW_REQUIRED", "동일 대상의 미종료 사건을 검토하세요.")
        elif operating:
            old = self.open_s1_incident(args.primary_object_id)
        else:
            old = self.db.execute("SELECT * FROM incidents WHERE facility_id=? AND run_id=? AND dedup_key=? AND status NOT IN ('resolved','closed_no_issue','closed_false_positive')", (FACILITY, args.run_id, dedup)).fetchone()
        stored = {(entry["type"], entry["zone_id"]): IncidentImpact(
            type=entry["type"], zone_id=entry["zone_id"], object_id=args.primary_object_id)
            for entry in self.db.execute("SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (old["incident_id"],))} if old else {}
        if stored and operating != all(kind in S1_TYPES for kind, _ in stored):
            raise ApiError(409, "IMPACT_CHANGED", "주차 사건과 다른 영향은 별도 검토하세요.")
        merged = dict(stored)
        merged.update({(i.type, i.zone_id): i for i in args.impacts})
        if operating:
            for impact, _ in self.current_s1_candidates(args.primary_object_id):
                merged.setdefault((impact.type, impact.zone_id), impact)
        elif stored and set(stored) != set(merged):
            raise ApiError(409, "IMPACT_CHANGED", "영향 범위를 검토하세요.")
        impact_values = [(i, self.impact_assessment(i.type, args.primary_object_id, i.zone_id)) for i in merged.values()]
        additions = [(i, value) for i, value in impact_values if (i.type, i.zone_id) not in stored]
        if any(value["support_status"] != "supported" or not value["violation_candidate"] for _, value in additions):
            raise ApiError(409, "OBSERVATION_NOT_READY", "새 영향에는 현재 지원되는 위반 근거가 필요합니다.")
        evidence = {oid for _, value in impact_values for oid in value["observation_ids"]}
        if set(args.evidence_ids) != evidence:
            raise ApiError(409, "OBSERVATION_CHANGED", "현재 분석의 실제 관측 묶음을 사용하세요.")
        trustworthy = all(value["support_status"] == "supported" for _, value in impact_values)
        candidate = trustworthy and (any if operating else all)(value["violation_candidate"] for _, value in impact_values)
        if args.status in ("candidate", "active", "escalated") and not candidate:
            raise ApiError(409, "OBSERVATION_NOT_READY", "현재 차단과 정지 근거가 필요합니다.")
        if args.status == "monitoring" and not (trustworthy and (any if operating else all)(
                value.get("occupied", value["violation_candidate"]) for _, value in impact_values)):
            raise ApiError(409, "OBSERVATION_NOT_READY", "현재 대상의 통로 점유 근거가 필요합니다.")
        if args.status in CLOSED and not (trustworthy and all(value["clearance_sustained"] for _, value in impact_values)):
            raise ApiError(409, "RECOVERY_NOT_CONFIRMED", "새 관측의 지속 통로 회복이 필요합니다.")
        if args.status in ("closed_no_issue", "closed_false_positive") and args.incident_id and self.db.execute(
                "SELECT 1 FROM executions WHERE incident_id=? AND tool_name IN ('notify_vehicle_user','report_to_owner') AND status IN ('running','succeeded','unknown')", (args.incident_id,)).fetchone():
            raise ApiError(409, "ACTION_ALREADY_APPLIED", "이미 실행된 사건은 회복 확인으로 해결 처리하세요.")
        if not args.incident_id and args.status != "active":
            raise ApiError(422, "INITIAL_INCIDENT_STATUS", "새 사건은 검증된 active 상태로 기록합니다.")
        result_status = args.status
        if old:
            incident_id = old["incident_id"]
            if not args.incident_id and not additions:
                return {"incident_id": old["incident_id"], "resource_version": old["resource_version"], "deduplicated": True}
            result_status = args.status if args.incident_id else old["status"]
            for impact, value in additions:
                self.insert("incident_impacts", impact_id=ident("impact"), facility_id=FACILITY, run_id=args.run_id,
                    incident_id=incident_id, type=impact.type, object_id=args.primary_object_id,
                    zone_id=impact.zone_id, condition_json=encoded(value["metrics"]))
            self.changed("incidents", "incident_id", incident_id, args.run_id, status=result_status, reason_summary=args.reason_summary)
        else:
            if operating:
                previous = self.db.execute("""SELECT i.incident_id FROM incidents i
                    WHERE i.facility_id=? AND i.run_id=? AND i.primary_object_id=?
                    AND i.status IN ('resolved','closed_no_issue','closed_false_positive')
                    AND EXISTS (SELECT 1 FROM incident_impacts p WHERE p.incident_id=i.incident_id
                        AND p.type IN ('aisle_obstruction','exit_blocked','bay_intrusion'))
                    ORDER BY i.rowid DESC LIMIT 1""", (FACILITY, args.run_id, args.primary_object_id)).fetchone()
            else:
                previous = self.db.execute("SELECT incident_id FROM incidents WHERE facility_id=? AND run_id=? AND dedup_key=? ORDER BY rowid DESC LIMIT 1", (FACILITY, args.run_id, dedup)).fetchone()
            incident_id = ident("incident")
            self.insert("incidents", incident_id=incident_id, facility_id=FACILITY, run_id=args.run_id, status=args.status,
                        primary_object_id=args.primary_object_id, dedup_key=dedup, policy_version=args.policy_version,
                        reason_summary=args.reason_summary, previous_incident_id=previous[0] if previous else None)
            for impact, value in impact_values:
                self.insert("incident_impacts", impact_id=ident("impact"), facility_id=FACILITY, run_id=args.run_id,
                        incident_id=incident_id, type=impact.type, object_id=args.primary_object_id,
                            zone_id=impact.zone_id, condition_json=encoded(value["metrics"]))
            self.emit(args.run_id, "incident.updated", incident_id=incident_id, resource_version=1)
        frames = {f["observation_id"]: f for f in self.runtime.world.get("observation_history") or [self.runtime.world["observation"]]}
        for oid in args.evidence_ids:
            frame = frames[oid]
            prior = self.db.execute("SELECT digest FROM observation_evidence WHERE observation_id=?", (oid,)).fetchone()
            if prior and prior[0] != digest(frame):
                raise ApiError(409, "EVIDENCE_CONFLICT", "관측 ID의 내용이 바뀌었습니다.")
            if not prior:
                self.insert("observation_evidence", observation_id=oid, facility_id=FACILITY, run_id=args.run_id,
                            state_version=frame["state_version"], sim_time_ms=frame["sim_time_ms"], payload_json=encoded(frame), digest=digest(frame))
            self.db.execute("INSERT OR IGNORE INTO incident_evidence (facility_id,run_id,incident_id,observation_id,analysis_version,metrics_json,purpose) VALUES (?,?,?,?,?,?,?)",
                            (FACILITY, args.run_id, incident_id, oid, "observed-impacts-v1",
                             encoded({i.type: value["metrics"] for i, value in impact_values}), args.status))
        return {"incident_id": incident_id, "resource_version": self.scoped("incidents", incident_id, "incident_id")["resource_version"], "status": result_status}

    def followup(self, args, username, policy):
        if (args.clock, args.condition) not in {
            ("sim", "spatial_recheck"), ("sim", "exit_recheck"), ("sim", "bay_recheck"),
            ("sim", "approach_recheck"), ("sim", "device_recheck"), ("sim", "command_recheck"),
            ("wall", "response_timeout") }:
            raise ApiError(422, "CLOCK_MISMATCH", "조건에 맞는 시계를 사용하세요.")
        if args.max_attempts > policy.execution_rules.followup_max_attempts:
            raise ApiError(429, "FOLLOWUP_LIMIT", "후속 확인 한도입니다.")
        if args.clock == "sim":
            delta = args.due_sim_time_ms-self.runtime.world["sim_time_ms"]
            valid = 0 < delta <= policy.execution_rules.spatial_followup_sim_ms
        else:
            delta = (instant(args.due_at)-instant(self.clock())).total_seconds()*1000
            valid = 0 < delta <= policy.execution_rules.overall_timeout_wall_ms
        if not valid:
            raise ApiError(422, "INVALID_DEADLINE", "정책 한도 안의 미래 기한이 필요합니다.")
        existing = self.db.execute("SELECT followup_id FROM followups WHERE facility_id=? AND run_id=? AND incident_id IS ? AND command_id IS ? AND clock=? AND condition_json=? AND status IN ('scheduled','claimed')",
            (FACILITY, args.run_id, args.incident_id, args.command_id, args.clock, encoded({"condition": args.condition}))).fetchone()
        if existing:
            return {"followup_id": existing[0], "deduplicated": True}
        fid = ident("followup")
        self.insert("followups", followup_id=fid, facility_id=FACILITY, run_id=args.run_id, incident_id=args.incident_id,
                    command_id=args.command_id, requester_ref=username, clock=args.clock, due_sim_time_ms=args.due_sim_time_ms,
                    due_at=args.due_at, condition_json=encoded({"condition": args.condition}), status="scheduled",
                    max_attempts=args.max_attempts, policy_version=policy.policy_version)
        self.emit(args.run_id, "followup.updated", followup_id=fid, resource_version=1)
        return {"followup_id": fid}

    def recover(self):
        with transaction(self.db):
            rows = self.db.execute("SELECT e.*,n.notification_id,n.delivery_status FROM executions e JOIN notifications n USING(execution_id) WHERE e.status='running'").fetchall()
            for row in rows:
                state = self.channel.reconcile(row) if self.channel.mode == row["mode"] else "unknown"
                self.db.execute("UPDATE delivery_attempts SET status=?,completed_at=? WHERE notification_id=? AND status='requested'",
                    ("accepted" if state == "accepted" else "failed" if state == "not_sent" else "unknown", self.clock(), row["notification_id"]))
                status = "succeeded" if state == "accepted" else "accepted" if state == "not_sent" else "unknown"
                self.changed("executions", "execution_id", row["execution_id"], row["run_id"], status=status,
                             error_code=None if state == "accepted" else "RECOVERY_RECHECK" if state == "not_sent" else "DELIVERY_UNKNOWN")
                if state == "unknown":
                    self.changed("notifications", "notification_id", row["notification_id"], row["run_id"], delivery_status="unknown")

    def prepare_delivery(self):
        row = self.db.execute("""SELECT e.*,n.notification_id,n.delivery_status,n.recipient_user_id FROM executions e JOIN notifications n USING(execution_id)
            WHERE e.status IN ('accepted','running') AND NOT EXISTS (SELECT 1 FROM delivery_attempts a WHERE a.notification_id=n.notification_id AND a.status='requested')
            ORDER BY e.rowid LIMIT 1""").fetchone()
        if not row:
            return None
        with transaction(self.db):
            raw = json.loads(row["payload_json"])
            private = raw.pop("_server")
            try:
                if self.registry.role(row["requester_ref"]) != private["request_role"]:
                    raise ApiError(403, "ACCESS_CHANGED", "실행 주체 권한이 바뀌었습니다.")
                if row["cancellation_requested_at"]:
                    raise ApiError(409, "CANCELLED", "취소 요청을 확인했습니다.")
                args = INPUTS[row["tool_name"]].model_validate(raw)
                policy = self.policy(args, row["tool_name"])
                if args.plan_id:
                    plan = self.scoped("plans", args.plan_id, "plan_id")
                    if not any(step.get("tool") == row["tool_name"] and step.get("execution_id") == row["execution_id"]
                               for step in json.loads(plan["steps_json"])):
                        raise ApiError(409, "PLAN_STEP_CHANGED", "현재 계획에서 이 실행 단계가 교체됐습니다.")
                if (instant(self.clock())-instant(row["created_at"])).total_seconds()*1000 >= policy.execution_rules.overall_timeout_wall_ms:
                    raise ApiError(409, "EXECUTION_EXPIRED", "전체 실행 기한을 지났습니다.")
                if row["tool_name"] == "notify_vehicle_user":
                    recipient, policy = self.notification_guard(args, row["requester_ref"])
                    if any(recipient[k] != private[k] for k in ("mapping_id", "user_id", "registered_vehicle_id")):
                        raise ApiError(409, "RECIPIENT_CHANGED", "접수 후 차주 연결이 바뀌었습니다.")
                else:
                    owners = self.db.execute("SELECT u.user_id FROM users u JOIN memberships m USING(user_id) WHERE m.facility_id=? AND m.role='owner' AND m.revoked_at IS NULL AND u.disabled_at IS NULL", (FACILITY,)).fetchall()
                    if len(owners) != 1 or owners[0][0] != private["user_id"]:
                        raise ApiError(409, "RECIPIENT_CHANGED", "유일한 소유자 권한이 바뀌었습니다.")
                    self.runtime.knowledge.validate_evidence(row["requester_ref"], FACILITY, args.run_id, args.knowledge_evidence,
                                                            tool_name="report_to_owner", purpose="incident_report")
                if self.channel.mode != row["mode"]:
                    raise ApiError(409, "CHANNEL_CHANGED", "실행 채널이 바뀌었습니다.")
                attempts = self.db.execute("SELECT count(*) FROM delivery_attempts WHERE notification_id=?", (row["notification_id"],)).fetchone()[0]
                if attempts >= policy.execution_rules.delivery_max_attempts:
                    raise ApiError(409, "DELIVERY_LIMIT", "발송 재시도 한도입니다.")
            except ApiError as exc:
                self.changed("executions", "execution_id", row["execution_id"], row["run_id"], status="cancelled" if exc.code == "CANCELLED" else "held", error_code=exc.code)
                self.changed("notifications", "notification_id", row["notification_id"], row["run_id"], delivery_status="failed")
                self.audit(row["requester_ref"], "dispatch", row["execution_id"], row["run_id"], "held", exc.code)
                return None
            attempt_id = ident("attempt")
            self.insert("delivery_attempts", attempt_id=attempt_id, facility_id=FACILITY, run_id=row["run_id"],
                        notification_id=row["notification_id"], attempt_number=attempts+1, channel=self.channel.name,
                        status="requested", requested_at=self.clock())
            self.changed("executions", "execution_id", row["execution_id"], row["run_id"], status="running")
            return {"execution_id": row["execution_id"], "notification_id": row["notification_id"], "run_id": row["run_id"],
                    "attempt_id": attempt_id, "attempt": attempts+1, "limit": policy.execution_rules.delivery_max_attempts,
                    "timeout": policy.execution_rules.delivery_timeout_wall_ms/1000,
                    "response_ms": policy.execution_rules.response_timeout_wall_ms,
                    "message": json.loads(self.scoped("notifications", row["notification_id"], "notification_id")["message_json"])}

    def finish_delivery(self, pending, outcome):
        with transaction(self.db):
            self.db.execute("UPDATE delivery_attempts SET status=?,completed_at=? WHERE attempt_id=?", (outcome, self.clock(), pending["attempt_id"]))
            status = "succeeded" if outcome == "accepted" else "unknown" if outcome == "unknown" else "failed" if pending["attempt"] >= pending["limit"] else "running"
            delivery = "channel_accepted" if outcome == "accepted" else "unknown" if outcome == "unknown" else "failed" if status == "failed" else "queued"
            self.changed("notifications", "notification_id", pending["notification_id"], pending["run_id"],
                         delivery_status=delivery, response_due_at=later(self.clock(), pending["response_ms"]) if outcome == "accepted" else None)
            self.changed("executions", "execution_id", pending["execution_id"], pending["run_id"], status=status,
                result_json=encoded({"notification_id": pending["notification_id"], "delivery_status": delivery}),
                error_code=None if outcome == "accepted" else "DELIVERY_UNKNOWN" if outcome == "unknown" else "CHANNEL_FAILED")
            self.audit("dispatcher", "delivery", pending["execution_id"], pending["run_id"], status, delivery)

    async def deliver_one(self):
        async with self.runtime.lock:
            pending = self.prepare_delivery()
        if pending is None:
            return False
        try:
            outcome = await asyncio.wait_for(self.channel.send(pending["notification_id"], pending["message"]), pending["timeout"])
            if outcome not in ("accepted", "failed", "unknown"):
                outcome = "unknown"
        except asyncio.CancelledError:
            # Persisted requested attempt is reconciled on restart.
            raise
        except Exception:
            outcome = "unknown"
        async with self.runtime.lock:
            self.finish_delivery(pending, outcome)
        return True

    def publish_outbox(self):
        with transaction(self.db):
            for row in self.db.execute("SELECT * FROM outbox_events WHERE dispatch_status='pending' ORDER BY rowid LIMIT 64").fetchall():
                event = {"schema_version": "0.1-draft", "facility_id": FACILITY, "run_id": row["run_id"],
                         "state_version": self.runtime.world["state_version"] if self.runtime.world and self.runtime.world["run_id"] == row["run_id"] else 0,
                         "occurred_at": row["created_at"], "type": row["event_type"], "payload": json.loads(row["payload_json"])}
                seq = self.runtime.store.append_event(event)
                self.db.execute("UPDATE outbox_events SET dispatch_status='sent',stream_seq=?,dispatched_at=?,attempt_count=attempt_count+1 WHERE event_id=?",
                                (seq, self.clock(), row["event_id"]))

    def notification_allowed(self, username, row):
        if self.registry.role(username) is None or row["recipient_user_id"] != username:
            return False
        if row["mode"] != "live" or row["delivery_status"] not in ("channel_accepted", "client_received"):
            return False
        if row["purpose"] == "owner_report":
            return self.registry.role(username) == "owner"
        execution = self.scoped("executions", row["execution_id"], "execution_id")
        private = json.loads(execution["payload_json"])["_server"]
        current = self.runtime.world["sim_time_ms"] if self.runtime.world and self.runtime.world["run_id"] == row["run_id"] else private["frame_sim"]
        return private["object_id"] in self.registry.allowed_objects(username, row["run_id"], private["frame_sim"], current, private["recorded_at"])

    def notification_view(self, row):
        source = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='synthetic_inbox_events'").fetchone():
            source = self.db.execute("SELECT receipt_id,response_id,reason_code FROM synthetic_inbox_events WHERE notification_id=?",
                                     (row["notification_id"],)).fetchone()
        responses = []
        for response in self.db.execute("SELECT response_id,response,text,responded_at FROM notification_responses WHERE notification_id=? ORDER BY rowid",
                                        (row["notification_id"],)):
            responses.append({"response": response["response"], "text": response["text"],
                "responded_at": response["responded_at"],
                "response_source": "synthetic_consumer" if source and source["response_id"] == response["response_id"]
                    and source["reason_code"] != "EXISTING_RESPONSE" else "user"})
        return {k: row[k] for k in ("notification_id", "run_id", "incident_id", "command_id", "delivery_status", "purpose", "mode", "resource_version", "created_at", "response_due_at")} | {
            "message": json.loads(row["message_json"]),
            "recipient_activity_mode": "synthetic_consumer" if source and source["receipt_id"] else "human_or_unconfirmed",
            "responses": responses}

    def notifications(self, username, cursor=0, limit=50):
        rows = self.db.execute("SELECT rowid AS cursor,* FROM notifications WHERE recipient_user_id=? AND rowid>? ORDER BY rowid LIMIT ?", (username, cursor, limit)).fetchall()
        return {"items": [self.notification_view(r) for r in rows if self.notification_allowed(username, r)], "cursor": rows[-1]["cursor"] if rows else cursor}

    def reply(self, session, notification_id, action, args, key):
        row = self.scoped("notifications", notification_id, "notification_id")
        if not self.notification_allowed(session.username, row):
            raise ApiError(404, "NOT_FOUND", "현재 권한에서 알림을 찾을 수 없습니다.")
        fingerprint, old = self.key(session.username, key, action, {"notification_id": notification_id, **args.model_dump()})
        if old:
            return old
        table = "notification_receipts" if action == "receipt" else "notification_responses"
        prior = self.db.execute(f"SELECT * FROM {table} WHERE user_id=? AND client_request_id=?", (session.username, args.client_request_id)).fetchone()
        if prior:
            if prior["notification_id"] != notification_id or any(prior[k] != v for k, v in args.model_dump().items()):
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "클라이언트 요청 ID가 다른 응답에 사용됐습니다.")
            return {"receipt_id" if action == "receipt" else "response_id": prior["receipt_id" if action == "receipt" else "response_id"]}
        with transaction(self.db):
            identifier = "receipt_id" if action == "receipt" else "response_id"
            rid = ident(action)
            values = {identifier: rid, "facility_id": FACILITY, "run_id": row["run_id"], "notification_id": notification_id,
                      "user_id": session.username, **args.model_dump()}
            if action != "receipt":
                values["responded_at"] = self.clock()
            self.insert(table, **values)
            if action == "receipt" and row["delivery_status"] != "client_received":
                self.changed("notifications", "notification_id", notification_id, row["run_id"], delivery_status="client_received")
            elif action != "receipt":
                self.changed("notifications", "notification_id", notification_id, row["run_id"], delivery_status=row["delivery_status"])
            result = {identifier: rid}
            self.audit(session.username, action, notification_id, row["run_id"])
            self.save_key(session.username, key, fingerprint, result)
            return result

    def project_event(self, username, event):
        if self.registry.role(username) in ("owner", "test_operator"):
            return event
        payload = event["payload"]
        nid = payload.get("notification_id")
        if nid:
            row = self.scoped("notifications", nid, "notification_id")
            return event if self.notification_allowed(username, row) else None
        # Driver streams never disclose internal incident, execution or plan IDs.
        return None

    def cancel_old_run(self, current_run):
        for row in self.db.execute("SELECT * FROM executions WHERE run_id<>? AND status IN ('accepted','running')", (current_run,)).fetchall():
            if row["status"] == "accepted":
                self.changed("executions", "execution_id", row["execution_id"], row["run_id"], status="cancelled", error_code="RUN_CHANGED")
                notice = self.db.execute("SELECT notification_id FROM notifications WHERE execution_id=?", (row["execution_id"],)).fetchone()
                if notice:
                    self.changed("notifications", "notification_id", notice[0], row["run_id"], delivery_status="failed")
            else:
                self.changed("executions", "execution_id", row["execution_id"], row["run_id"], cancellation_requested_at=self.clock())
        self.db.execute("UPDATE followups SET status='cancelled',updated_at=?,resource_version=resource_version+1 WHERE run_id<>? AND status IN ('scheduled','claimed')", (self.clock(), current_run))
        self.db.execute("UPDATE plans SET status='cancelled',updated_at=?,resource_version=resource_version+1 WHERE run_id<>? AND status IN ('proposed','active')", (self.clock(), current_run))

    def process_followups(self):
        """Claim atomically with one bounded proposal. No empty proposal executes."""
        if not self.runtime.world:
            return
        with transaction(self.db):
            for row in self.db.execute("SELECT * FROM followups WHERE status IN ('scheduled','claimed') ORDER BY rowid LIMIT 32").fetchall():
                if row["run_id"] != self.runtime.world["run_id"] or self.registry.role(row["requester_ref"]) not in ("owner", "test_operator"):
                    self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="cancelled")
                    continue
                if row["command_id"]:
                    command = self.scoped("commands", row["command_id"], "command_id")
                    if command["cancellation_requested_at"] or command["aggregate_status"] == "cancelled":
                        self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="cancelled")
                        continue
                if row["incident_id"] and self.scoped("incidents", row["incident_id"], "incident_id")["status"] in CLOSED:
                    self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="cancelled")
                    continue
                due = self.runtime.world["sim_time_ms"] >= row["due_sim_time_ms"] if row["clock"] == "sim" else instant(self.clock()) >= instant(row["due_at"])
                if not due:
                    continue
                try:
                    current_policy = self.runtime.knowledge.current_policy(FACILITY)
                    valid = current_policy.policy_version == row["policy_version"] and current_policy.execution_rules is not None
                except (ValueError, OSError):
                    valid = False
                if not valid:
                    self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="cancelled")
                    prior_plan = self.db.execute("SELECT plan_id FROM plans WHERE trigger_followup_id=?", (row["followup_id"],)).fetchone()
                    if prior_plan:
                        self.changed("plans", "plan_id", prior_plan[0], row["run_id"], status="cancelled")
                    self.audit(row["requester_ref"], "followup_cancel", row["followup_id"], row["run_id"], "cancelled", "POLICY_CHANGED")
                    continue
                if row["status"] == "scheduled":
                    pid = ident("plan")
                    self.insert("plans", plan_id=pid, facility_id=FACILITY, run_id=row["run_id"], incident_id=row["incident_id"], command_id=row["command_id"],
                        trigger_followup_id=row["followup_id"], steps_json="[]", model_ref="manual_recheck", policy_version=row["policy_version"],
                        budget_json=encoded({"max_attempts": row["max_attempts"]}), status="proposed")
                    self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="claimed", attempt_count=row["attempt_count"]+1)
                else:
                    pid = self.db.execute("SELECT plan_id FROM plans WHERE trigger_followup_id=?", (row["followup_id"],)).fetchone()[0]
                # Follow-up creates a review proposal, not a privileged actuation.
                # P07 consumes current observations through the same incident tool.
                analysis = self.analysis() if json.loads(row["condition_json"])["condition"] == "spatial_recheck" else None
                steps = [{"operation": "manual_review", "policy_valid": valid,
                          "spatial_analysis": analysis.model_dump() if analysis else None,
                          "reason_code": "MANUAL_REVIEW_REQUIRED" if valid else "POLICY_CHANGED"}]
                self.changed("plans", "plan_id", pid, row["run_id"], status="held", steps_json=encoded(steps))
                self.changed("followups", "followup_id", row["followup_id"], row["run_id"], status="completed")
                self.audit(row["requester_ref"], "followup_review", pid, row["run_id"], "held", "MANUAL_REVIEW_REQUIRED")
