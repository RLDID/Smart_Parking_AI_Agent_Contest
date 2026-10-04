"""Server-owned autonomous work. Model output is only a bounded recommendation."""
import asyncio
from copy import deepcopy
import json
import time

from pydantic import ValidationError

from agent.autonomous import MockOperationsAdapter
from agent.tools import call_read_tool, validate_session
from backend.auth import ApiError
from backend.business import CLOSED, S1_TYPES, encoded, ident, instant, later
from backend.knowledge import transaction
from contracts.autonomous import AutonomousControl, AutonomousDecision
from contracts.knowledge import KnowledgeEvidence
from simulator.world import FACILITY, digest, public_state


_TOPICS = {"s1a": ("통로 차단 이동 요청과 미응답", "parking_order", "aisle-west"),
           "s1b": ("출차 방해 이중주차 이동 요청", "parking_order", "aisle-west"),
           "s1c": ("주차면 침범 재주차 요청", "parking_order", None),
           "s2": ("보행자 차량 접근 위험 안내", "user_guidance", None),
           "s3": ("영업 종료 입출차 안내 방송", "entry_exit", None)}
_IMPACTS = {"s1a": "aisle_obstruction", "s1b": "exit_blocked",
            "s1c": "bay_intrusion", "s2": "approach_risk"}
_SCENARIOS = {value: key for key, value in _IMPACTS.items()}


class AutonomousService:
    def __init__(self, runtime, live_adapter_factory=None):
        self.runtime = runtime
        self.live_adapter_factory = live_adapter_factory
        self.enabled = None  # Ephemeral opt-in; restart cannot silently resume paid work.
        self.active = {}
        self.seen = set()
        self.closed = False

    def _guard(self, session, run_id, authenticate):
        if self.closed or self.runtime.failure:
            raise ApiError(503, "AGENT_UNAVAILABLE", "현재 업무 Agent를 사용할 수 없습니다.")
        authenticate()
        validate_session(self.runtime, session)
        if session.role not in ("owner", "test_operator"):
            raise ApiError(403, "FORBIDDEN", "운영 업무 권한을 확인하세요.")
        self.runtime.ensure_run(run_id)
        if self.runtime.world.get("replay_state") is not None:
            raise ApiError(409, "REPLAY_MODE", "기록 재생 중에는 새 업무를 실행할 수 없습니다.")
        if self.runtime.world["recovery_required"]:
            raise ApiError(409, "RECOVERY_REQUIRED", "새 관측으로 복구 상태를 확인하세요.")

    def _stamp(self, session, run_id, authenticate):
        self._guard(session, run_id, authenticate)
        r = self.runtime
        try:
            policy = r.knowledge.current_policy(FACILITY)
            documents = [tuple(row) for row in r.store.db.execute(
                "SELECT document_id,document_version,content_digest,allowed_roles_json,approval_status,effective_at,retired_at,reviewed_conflict "
                "FROM knowledge_documents WHERE facility_id=? ORDER BY document_id,document_version", (FACILITY,))]
        except (ValueError, OSError):
            raise ApiError(503, "POLICY_UNAVAILABLE", "현재 운영 기준을 확인할 수 없습니다.") from None
        return digest({"run_id": run_id, "scope": r.store.registry.scope_stamp(session.username),
                       "policy": [policy.policy_version, policy.knowledge_release_id], "documents": documents})

    async def control(self, session, body: AutonomousControl, key, authenticate):
        r = self.runtime
        if not key or not 1 <= len(key) <= 128:
            raise ApiError(400, "IDEMPOTENCY_REQUIRED", "1~128자 요청 키가 필요합니다.")
        async with r.lock:
            self._guard(session, body.run_id, authenticate)
            if body.action != "stop" and hasattr(r, "ensure_autonomous_policy"):
                r.ensure_autonomous_policy()
            if body.action in ("start", "stop"):
                fingerprint, old = r.business.key(session.username, key, "autonomous_control", body.model_dump())
                if old:
                    return old
            if body.mode == "live" and (self.live_adapter_factory is None or r.queries.live_models is None):
                raise ApiError(503, "MODEL_NOT_CONFIGURED", "실제 업무 모델 설정이 필요합니다.")
            if body.action == "start":
                if self.enabled and self.enabled["run_id"] == body.run_id and self.enabled["mode"] == body.mode:
                    result = {"status": "enabled", "mode": body.mode, "run_id": body.run_id}
                    with transaction(r.store.db):
                        r.business.save_key(session.username, key, fingerprint, result)
                    return result
                self.enabled = {"session": session, "authenticate": authenticate,
                                "run_id": body.run_id, "mode": body.mode}
                self.seen.clear()
                result = {"status": "enabled", "mode": body.mode, "run_id": body.run_id}
                with transaction(r.store.db):
                    r.business.save_key(session.username, key, fingerprint, result)
                return result
            if body.action == "stop":
                self.enabled = None
                # Running jobs retain their durable status. They recheck this
                # switch before any new business or device operation.
                result = {"status": "disabled", "run_id": body.run_id}
                with transaction(r.store.db):
                    r.business.save_key(session.username, key, fingerprint, result)
                return result
        return await self.process(session, body, key, authenticate)

    def _current_trigger(self):
        """Only public observation/DB changes can cause a new background job."""
        r = self.runtime
        if not r.world:
            return None
        analysis = r.business.analysis()
        candidates = tuple(sorted(o.object_id for o in analysis.metrics.objects if o.stationary_candidate))
        incidents = tuple((row["incident_id"], row["status"], row["resource_version"], row["type"])
                          for row in r.store.db.execute("""SELECT i.incident_id,i.status,i.resource_version,p.type FROM incidents i
                              JOIN incident_impacts p USING(incident_id) WHERE i.run_id=?
                              AND i.status NOT IN ('resolved','closed_no_issue','closed_false_positive')
                              ORDER BY i.incident_id,p.type""", (r.world["run_id"],)))
        responses = tuple((row["notification_id"], row["response"])
                          for row in r.store.db.execute("SELECT n.notification_id,r.response FROM notification_responses r JOIN notifications n USING(notification_id) WHERE n.run_id=? ORDER BY r.rowid", (r.world["run_id"],)))
        notices = tuple((row["notification_id"], row["delivery_status"], row["contact_sequence"],
                         bool(row["response_due_at"] and instant(r.business.clock()) >= instant(row["response_due_at"])))
                        for row in r.store.db.execute("SELECT notification_id,delivery_status,contact_sequence,response_due_at FROM notifications WHERE run_id=? AND purpose='move_request' ORDER BY rowid", (r.world["run_id"],)))
        followups = tuple((row["followup_id"], row["status"], row["attempt_count"])
                          for row in r.store.db.execute("SELECT followup_id,status,attempt_count FROM followups WHERE run_id=? ORDER BY rowid", (r.world["run_id"],)))
        commands = r.store.db.execute("SELECT * FROM commands WHERE run_id=? AND aggregate_status IN ('pending','running','held') AND cancellation_requested_at IS NULL ORDER BY rowid", (r.world["run_id"],)).fetchall()
        latest_device_feedback = {}
        for row in r.store.db.execute("""SELECT command_id,tool_name,target_ref,status FROM executions
                WHERE run_id=? AND command_id IS NOT NULL AND tool_name IN ('play_announcement','set_entry_policy')
                ORDER BY rowid""", (r.world["run_id"],)):
            latest_device_feedback[(row["command_id"], row["tool_name"], row["target_ref"])] = row["status"]
        device_feedback = tuple(sorted((*identity, status) for identity, status in latest_device_feedback.items()))
        candidate_provider = getattr(r, "operating_candidates", None)
        other = {}
        if candidate_provider:
            for scenario in ("s1b", "s1c", "s2"):
                entries = candidate_provider(scenario)[:8]
                other[scenario] = tuple((item["object_id"], item["zone_id"],
                    item["assessment"].get("support_status"), item["assessment"].get("violation_candidate"),
                    item["assessment"].get("clearance_sustained")) for item in entries)
        for row in commands:
            goal = self._command_goal(row, json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else None)
            if goal["action"] == "hold":
                continue
            if not goal["confirmed"] and goal["kind"] != "ambiguous":
                proposal = r.store.db.execute("SELECT 1 FROM plans WHERE command_id=? AND status='proposed' LIMIT 1",
                                              (row["command_id"],)).fetchone()
                if proposal:
                    continue
            devices = r.public_devices(r.world) if goal["confirmed"] else None
            gates = tuple((gate["gate_id"], gate["entry_policy"], gate["physical_state"],
                           gate["obstacle_detected"]) for gate in devices["gates"]) if devices else ()
            broadcasts = tuple(sorted((item["zone_id"], item["message_id"], item["receipt"],
                                       item["simulated_playback"]) for item in devices["broadcasts"])) if devices else ()
            signature = digest(("s3", row["command_id"], row["resource_version"],
                                row["normalized_goal_json"], goal["action"],
                                device_feedback if goal["confirmed"] else (), gates, broadcasts))
            if signature not in self.seen:
                return signature, "s3", row["command_id"]
        if incidents:
            scenario, command_id = _SCENARIOS.get(incidents[0][3], "s1a"), None
        else:
            scenario = next((name for name in ("s2", "s1b", "s1c")
                             if any(item[3] and item[2] == "supported" for item in other.get(name, ()))), None)
            if scenario is None and candidates:
                scenario = "s1a"
            command_id = None
        if scenario is None:
            return None
        state = (scenario, command_id, analysis.support_status, analysis.metrics.passage,
                 analysis.metrics.clearance_sustained, candidates, incidents, responses, notices, followups,
                 tuple((name, other.get(name)) for name in ("s1b", "s1c"))
                 if scenario in ("s1a", "s1b", "s1c") else other.get(scenario))
        return digest(state), scenario, command_id

    async def tick(self):
        r = self.runtime
        async with r.lock:
            enabled = self.enabled
            if not enabled or self.closed or r.failure:
                return
            try:
                self._guard(enabled["session"], enabled["run_id"], enabled["authenticate"])
                self._hold_cancelled_device_plans(enabled["run_id"])
                trigger = self._current_trigger()
            except ApiError:
                self.enabled = None
                return
            if trigger is None:
                return
            signature, scenario, command_id = trigger
            if (signature in self.seen or len(self.active) + len(r.queries.active) >= 2
                    or any(user == enabled["session"].username for user, _ in self.active)
                    or any(user == enabled["session"].username for user, _ in r.queries.active)):
                return
            key = "watch-" + signature[:64]
            self.seen.add(signature)
            body = AutonomousControl(run_id=enabled["run_id"], action="process", mode=enabled["mode"],
                                     scenario=scenario, command_id=command_id)
            job = asyncio.create_task(self.process(enabled["session"], body, key, enabled["authenticate"],
                                                   watcher=True, watcher_state=enabled))
            job.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)

    def _hold_cancelled_device_plans(self, run_id):
        r = self.runtime
        rows = r.store.db.execute("""SELECT DISTINCT p.plan_id FROM plans p JOIN executions e USING(plan_id)
            WHERE p.run_id=? AND p.command_id IS NOT NULL AND p.status='active'
            AND e.tool_name IN ('play_announcement','set_entry_policy')
            AND e.status IN ('cancelled','failed')""", (run_id,)).fetchall()
        if rows:
            with transaction(r.store.db):
                for row in rows:
                    r.business.changed("plans", "plan_id", row["plan_id"], run_id, status="held")

    async def process(self, session, body, key, authenticate, *, watcher=False, watcher_state=None):
        r = self.runtime
        pair = (session.username, key)
        started = time.monotonic()
        def authenticate_work():
            authenticate()
            if watcher and self.enabled is not watcher_state:
                raise ApiError(409, "AGENT_STOPPED", "자동 감시가 중지됐습니다.")
        async with r.lock:
            if hasattr(r, "ensure_autonomous_policy"):
                r.ensure_autonomous_policy()
            stamp = self._stamp(session, body.run_id, authenticate_work)
            fingerprint, prior = r.business.key(session.username, key, "autonomous_process", body.model_dump())
            if watcher and self.enabled is not watcher_state:
                raise ApiError(409, "AGENT_STOPPED", "자동 감시가 중지됐습니다.")
            if pair in self.active:
                raise ApiError(409, "JOB_IN_PROGRESS", "같은 업무가 이미 진행 중입니다.")
            read_active = r.queries.active
            if (len(self.active) + len(read_active) >= 2
                    or any(user == session.username for user, _ in self.active)
                    or any(user == session.username for user, _ in read_active)):
                raise ApiError(429, "AGENT_QUEUE_LIMIT", "진행 중인 업무가 끝난 뒤 다시 요청하세요.")
            existing = r.store.db.execute("SELECT * FROM autonomous_jobs WHERE facility_id=? AND run_id=? AND requester_ref=? AND trigger_key=?",
                                          (FACILITY, body.run_id, session.username, key)).fetchone()
            if existing:
                if prior is None:
                    raise ApiError(409, "JOB_RECONCILIATION_REQUIRED", "접수 기록과 작업 기록이 일치하지 않습니다.")
                if existing["context_stamp"] != stamp:
                    raise ApiError(409, "JOB_CONTEXT_CHANGED", "이전 업무의 권한·정책·문서가 바뀌었습니다.")
                if existing["status"] == "pending":
                    raise ApiError(409, "JOB_RECONCILIATION_REQUIRED", "중단된 업무의 실제 실행 결과를 먼저 확인하세요.")
                return json.loads(existing["result_json"])
            if prior is not None:
                raise ApiError(409, "JOB_RECONCILIATION_REQUIRED", "기존 접수 기록의 실행 상태를 확인하세요.")
            task = r.read_task(session, body.run_id)
            snapshot = self._snapshot(session, body, task)
            job_id = ident("autojob")
            with transaction(r.store.db):
                r.business.insert("autonomous_jobs", job_id=job_id, facility_id=FACILITY, run_id=body.run_id,
                    requester_ref=session.username, mode=body.mode, scenario=body.scenario, trigger_key=key,
                    context_stamp=stamp, status="pending", result_json=None,
                    created_at=r.business.clock(), updated_at=r.business.clock())
                r.business.save_key(session.username, key, fingerprint,
                    {"pending": True, "job_id": job_id, "context_stamp": stamp})
            self.active[pair] = job_id
        async def check_context():
            async with r.lock:
                if self._stamp(session, body.run_id, authenticate_work) != stamp:
                    raise ApiError(409, "JOB_CONTEXT_CHANGED", "작업 중 권한·운영 기준이 바뀌었습니다.")
        adapter = None
        try:
            adapter = (self.live_adapter_factory(r.queries.live_models, check_context)
                       if body.mode == "live" else MockOperationsAdapter())
            if body.mode == "live":
                # Route and admission are durable before the provider can be
                # called. A crash leaves this key unreplayable until review.
                async with r.lock:
                    r.store.db.execute("UPDATE autonomous_jobs SET result_json=?,updated_at=? WHERE job_id=?",
                        (encoded({"status": "pending", "route": adapter.route}), r.business.clock(), job_id))
                    r.store.db.commit()
            remaining = 28 - (time.monotonic() - started)
            if remaining <= 0:
                raise ApiError(429, "TASK_LIMIT", "업무 판단 시간이 지났습니다.")
            raw = await asyncio.wait_for(adapter.decide(deepcopy(snapshot)), remaining)
            try:
                decision = AutonomousDecision.model_validate(raw)
            except ValidationError:
                raise ApiError(422, "INVALID_DECISION", "제한된 업무 판단 형식을 확인할 수 없습니다.") from None
            async with r.lock:
                if self._stamp(session, body.run_id, authenticate_work) != stamp:
                    raise ApiError(409, "JOB_CONTEXT_CHANGED", "실행 전 권한·운영 기준이 바뀌었습니다.")
                if time.monotonic() - started >= 30:
                    raise ApiError(429, "TASK_LIMIT", "업무 실행 시간이 지났습니다.")
                result = self._apply(session, body, key, decision, snapshot, task)
            if result.get("device_intent"):
                intent = result.pop("device_intent")
                # DeviceOperations owns its lock and repeats authority, policy,
                # document, command and safety checks at the actual side effect.
                # DeviceOperations invokes this inside its writer lock, after any
                # queued stop/start can run and before the device side effect.
                execution = await r.execute_device_action(session, authenticate=authenticate_work, **intent)
                async with r.lock:
                    result.update(status=execution.get("status", "unknown"), execution=execution)
                    if self._stamp(session, body.run_id, authenticate) != stamp:
                        raise ApiError(409, "JOB_CONTEXT_CHANGED", "장치 실행 후 운영 문맥이 바뀌었습니다.")
            async with r.lock:
                result.update(job_id=job_id, mode=body.mode, decision=decision.model_dump(),
                              model=adapter.result_metadata())
                status = "completed" if result["status"] in ("accepted", "succeeded", "resolved", "clarification_required") else "held"
                with transaction(r.store.db):
                    r.store.db.execute("UPDATE autonomous_jobs SET status=?,result_json=?,updated_at=? WHERE job_id=?",
                                       (status, encoded(result), r.business.clock(), job_id))
                    r.store.db.execute("UPDATE business_requests SET response_json=? WHERE facility_id=? AND requester_ref=? AND key=? AND argument_hash=?",
                        (encoded({"job_id": job_id, "response": result}), FACILITY, session.username, key, fingerprint))
                return result
        except BaseException as error:
            # A dispatched paid request has unknown outcome unless its adapter
            # reconciled cost. No retry of this job key or side effect occurs.
            async with r.lock:
                meta = adapter.result_metadata() if adapter is not None else {"usage_status": "not_sent"}
                unknown = body.mode == "live" and meta.get("usage_status") == "unknown"
                if unknown:
                    # Keep the job unreplayable, but expose its safe failure and
                    # reserved cost for reconciliation instead of an opaque pending row.
                    with transaction(r.store.db):
                        r.store.db.execute("UPDATE autonomous_jobs SET result_json=?,updated_at=? WHERE job_id=?",
                            (encoded({"status": "pending", "reason_code": getattr(error, "reason_code",
                                "MODEL_OUTCOME_UNKNOWN"), "model": meta,
                                "reconciliation_required": True}), r.business.clock(), job_id))
                if not unknown and not isinstance(error, asyncio.CancelledError):
                    held = {"status": "held", "reason_code": getattr(error, "code",
                            getattr(error, "reason_code", "DECISION_FAILED")),
                            "run_id": body.run_id, "mode": body.mode, "model": meta}
                    with transaction(r.store.db):
                        r.store.db.execute("UPDATE autonomous_jobs SET status='held',result_json=?,updated_at=? WHERE job_id=?",
                                           (encoded(held), r.business.clock(), job_id))
                        r.store.db.execute("UPDATE business_requests SET response_json=? WHERE facility_id=? AND requester_ref=? AND key=? AND argument_hash=?",
                            (encoded({"job_id": job_id, "response": held}), FACILITY,
                             session.username, key, fingerprint))
            raise
        finally:
            async with r.lock:
                self.active.pop(pair, None)

    def _snapshot(self, session, body, task):
        r = self.runtime
        policy = call_read_tool(r, session, "get_operating_policy", {"facility_id": FACILITY}, task)
        analysis = r.business.analysis().model_dump() if body.scenario == "s1a" else None
        target = None
        target_zone = None
        if body.scenario == "s1a" and analysis["support_status"] == "supported":
            candidates = [o["object_id"] for o in analysis["metrics"]["objects"] if o["stationary_candidate"]]
            if len(candidates) == 1:
                target = candidates[0]
                target_zone = "aisle-west"
        incident = None
        notice = None
        if body.scenario in _IMPACTS:
            rows = r.store.db.execute("""SELECT i.* FROM incidents i JOIN incident_impacts p USING(incident_id)
                WHERE i.facility_id=? AND i.run_id=? AND p.type=?
                AND i.status NOT IN ('resolved','closed_no_issue','closed_false_positive')
                ORDER BY i.rowid DESC""", (FACILITY, body.run_id, _IMPACTS[body.scenario])).fetchall()
            if len({row["incident_id"] for row in rows}) > 1:
                raise ApiError(409, "INCIDENT_REVIEW_REQUIRED", "현재 장면의 복수 사건을 검토하세요.")
            incident = ({name: rows[0][name] for name in
                ("incident_id", "status", "primary_object_id", "resource_version", "policy_version", "reason_summary")}
                if rows else None)
            if incident:
                target = incident["primary_object_id"]
            if incident and not target_zone:
                impact_row = r.store.db.execute("SELECT zone_id FROM incident_impacts WHERE incident_id=? AND type=? ORDER BY rowid LIMIT 1", (incident["incident_id"], _IMPACTS[body.scenario])).fetchone()
                target_zone = impact_row[0] if impact_row else None
        if body.scenario in ("s1b", "s1c", "s2") and not incident:
            candidate_provider = getattr(r, "operating_candidates", None)
            options = candidate_provider(body.scenario) if candidate_provider else []
            eligible = [item for item in options[:8] if item["assessment"].get("support_status") == "supported"
                        and item["assessment"].get("violation_candidate")]
            if len(eligible) == 1:
                item = eligible[0]
                target, target_zone = item["object_id"], item["zone_id"]
                analysis = item["assessment"]
        elif body.scenario in ("s1b", "s1c", "s2") and incident and target and target_zone:
            analysis = r.business.impact_assessment(_IMPACTS[body.scenario], target, target_zone)
        if body.scenario in ("s1a", "s1b", "s1c") and target:
            related = r.business.open_s1_incident(target)
            if related:
                incident = {name: related[name] for name in
                    ("incident_id", "status", "primary_object_id", "resource_version", "policy_version", "reason_summary")}
                target_zone = "aisle-west" if body.scenario == "s1a" else "B01"
        recipient_check = None
        current = None
        if body.scenario in ("s1a", "s1b", "s1c") and target:
            task.consume(session, "resolve_vehicle_recipient")
            try:
                current = r.business.resolve_recipient(target)
                recipient_check = {"object_id": target, "mapping_status": "verified"}
            except ApiError as error:
                if error.code != "RECIPIENT_UNVERIFIED":
                    raise
                recipient_check = {"object_id": target, "mapping_status": "unverified"}
        if incident:
            recipient_filter = " AND recipient_user_id=?" if current else ""
            parameters = (incident["incident_id"], current["user_id"]) if current else (incident["incident_id"],)
            latest = r.store.db.execute("SELECT notification_id,delivery_status,contact_sequence,response_due_at FROM notifications WHERE incident_id=? AND purpose='move_request'" + recipient_filter + " ORDER BY contact_sequence DESC,rowid DESC LIMIT 1", parameters).fetchone()
            if latest:
                response = r.store.db.execute("SELECT response_id,response FROM notification_responses WHERE notification_id=? ORDER BY rowid DESC LIMIT 1", (latest["notification_id"],)).fetchone()
                notice = dict(latest) | {"response": response["response"] if response else None,
                    "response_id": response["response_id"] if response else None,
                    "response_due": bool(latest["response_due_at"] and instant(r.business.clock()) >= instant(latest["response_due_at"]))}
        command = None
        if body.command_id:
            row = r.business.scoped("commands", body.command_id, "command_id")
            if row["run_id"] != body.run_id or row["cancellation_requested_at"]:
                raise ApiError(409, "COMMAND_CHANGED", "현재 유효한 지시를 확인하세요.")
            command = {"command_id": row["command_id"], "purpose": row["purpose"], "text": row["request_text"],
                       "resource_version": row["resource_version"], "normalized_goal": json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else None}
            command["normalized_goal"] = self._command_goal(row, command["normalized_goal"])
        query, topic, zone = _TOPICS[body.scenario]
        if body.scenario == "s3" and command and command["normalized_goal"]["kind"] == "zone_notice":
            query, topic, zone = "A구역 쓰레기 안내", "announcement", "announcement-a"
        search = call_read_tool(r, session, "search_operating_knowledge",
            {"facility_id": FACILITY, "run_id": body.run_id, "query": query,
             "topic": topic, "zone_id": zone}, task)
        return {"scenario": body.scenario, "trigger": "command" if body.command_id else "observation",
                "run_id": body.run_id, "state_version": r.world["state_version"],
                "observation": public_state(r.world), "analysis": analysis,
                "policy": policy, "knowledge": search, "target_ref": target, "target_zone": target_zone,
                "recipient_check": recipient_check,
                "incident": incident, "notification": notice, "command": command}

    def _command_goal(self, row, stored_goal):
        text = row["request_text"].strip().lower()
        if (stored_goal and stored_goal.get("clarified") is True
                and stored_goal.get("kind") in ("closing", "zone_notice")):
            kind = stored_goal["kind"]
        elif any(word in text for word in ("영업 끝", "영업 종료", "문 닫")):
            kind = "closing" if ("입" in text and ("나가" in text or "출차" in text)) else "ambiguous"
        elif "쓰레기" in text and ("a구역" in text or "a 구역" in text):
            kind = "zone_notice"
        else:
            kind = "ambiguous"
        if kind == "ambiguous":
            return {"kind": kind, "action": "clarify", "confirmed": False}
        confirmed = bool(stored_goal and stored_goal.get("confirmed") is True)
        if not confirmed:
            return {"kind": kind, "action": "clarify", "confirmed": False}
        plan = self.runtime.store.db.execute(
            "SELECT plan_id,steps_json FROM plans WHERE command_id=? AND status='active' ORDER BY rowid DESC LIMIT 1",
            (row["command_id"],)).fetchone()
        if not plan:
            return {"kind": kind, "action": "hold", "zone_id": None, "confirmed": confirmed}
        executions = self.runtime.store.db.execute(
            "SELECT tool_name,target_ref,status FROM executions WHERE plan_id=? AND tool_name IN ('play_announcement','set_entry_policy') ORDER BY rowid",
            (plan["plan_id"],)).fetchall() if plan else []
        announcements = [entry["status"] for entry in executions if entry["tool_name"] == "play_announcement"]
        restrictions = [entry["status"] for entry in executions if entry["tool_name"] == "set_entry_policy"]
        if any(item in ("cancelled", "failed") for item in announcements + restrictions):
            action, zone = "hold", None
        elif any(item in ("accepted", "running", "unknown") for item in announcements + restrictions):
            action, zone = "hold", None
        elif kind == "zone_notice":
            action = "complete" if any(entry["target_ref"] == "announcement-a" and entry["status"] == "succeeded"
                                   for entry in executions if entry["tool_name"] == "play_announcement") else "announce"
            zone = "announcement-a" if action == "announce" else None
        elif "succeeded" in restrictions:
            action, zone = "complete", None
        else:
            played = {entry["target_ref"] for entry in executions
                      if entry["tool_name"] == "play_announcement" and entry["status"] == "succeeded"}
            missing = [zone for zone in ("announcement-a", "announcement-b") if zone not in played]
            action, zone = ("announce", missing[0]) if missing else ("restrict_entry", None)
        return {"kind": kind, "action": action, "zone_id": zone, "confirmed": confirmed,
                "plan_steps": json.loads(plan["steps_json"]),
                "execution_evidence": {
                    "played_zones": sorted({entry["target_ref"] for entry in executions
                        if entry["tool_name"] == "play_announcement" and entry["status"] == "succeeded"}),
                    "entry_policy_statuses": restrictions[-4:],
                    "mode": "synthetic_demo"}}

    def _apply(self, session, body, key, decision, snapshot, task):
        if decision.scenario != body.scenario:
            raise ApiError(422, "DECISION_SCOPE", "요청 장면과 판단 장면이 다릅니다.")
        if body.scenario == "s3":
            return self._command(session, body, key, decision, snapshot, task)
        if decision.action == "hold":
            return {"status": "held", "reason_code": decision.reason_code, "run_id": body.run_id}
        if body.scenario == "s1a":
            return self._s1a(session, body, key, decision, snapshot, task)
        if body.scenario in ("s1b", "s1c", "s2"):
            return self._observed_scenario(session, body, key, decision, snapshot, task)
        return self._command(session, body, key, decision, snapshot, task)

    def _has_new_supported_s1_impact(self, incident):
        """Pending contact does not freeze fresh evidence for the same episode."""
        r = self.runtime
        row = r.business.scoped("incidents", incident["incident_id"], "incident_id")
        if row["run_id"] != r.world["run_id"] or row["status"] in CLOSED:
            return False
        stored = {(entry["type"], entry["zone_id"]) for entry in r.store.db.execute(
            "SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (row["incident_id"],))}
        if not stored or any(kind not in S1_TYPES for kind, _ in stored):
            return False
        return any((impact.type, impact.zone_id) not in stored
                   for impact, _ in r.business.current_s1_candidates(row["primary_object_id"]))

    def _s1a(self, session, body, key, decision, snapshot, task):
        r = self.runtime
        analysis = r.business.analysis()
        current = [o.object_id for o in analysis.metrics.objects if o.stationary_candidate]
        incident = snapshot["incident"]
        if decision.action == "notify":
            if analysis.support_status != "supported" or current != [decision.target_ref]:
                return {"status": "held", "reason_code": "OBSERVATION_CHANGED", "run_id": body.run_id}
            return self._notify(session, body, key, decision.target_ref, "aisle-west", "aisle_obstruction",
                                analysis.observation_ids, snapshot["knowledge"], task)
        if decision.action == "recheck" and incident:
            notice = snapshot["notification"]
            if (notice and not notice["response"] and not analysis.metrics.clearance_sustained
                    and not self._has_new_supported_s1_impact(incident)):
                return {"status": "held", "reason_code": "AWAITING_RESPONSE_OR_MOVEMENT",
                        "run_id": body.run_id, "incident_id": incident["incident_id"]}
            return self._recheck(session, body, key, incident, "aisle_obstruction", "aisle-west", task)
        if decision.action == "report" and incident:
            if analysis.metrics.clearance_sustained:
                return self._recheck(session, body, key, incident, "aisle_obstruction", "aisle-west", task)
            return self._report_issue(session, body, key, incident, snapshot["notification"], task)
        return {"status": "held", "reason_code": "ACTION_NOT_READY", "run_id": body.run_id}

    def _report_issue(self, session, body, key, incident, notice, task):
        r = self.runtime
        if not notice:
            try:
                r.business.resolve_recipient(incident["primary_object_id"])
            except ApiError as error:
                if error.code == "RECIPIENT_UNVERIFIED":
                    return self._review_unverified_recipient(session, body, incident["incident_id"], task)
            return {"status": "held", "reason_code": "NOTICE_NOT_FOUND", "run_id": body.run_id}
        reason = notice["response"] or ("RESPONSE_TIMEOUT" if notice["response_due"] else "AWAITING_RESPONSE")
        if reason == "AWAITING_RESPONSE":
            return {"status": "held", "reason_code": reason, "run_id": body.run_id}
        policy = r.knowledge.current_policy(FACILITY)
        trigger = notice["response_id"] or notice["notification_id"]
        report_key = "agent-report-" + digest([incident["incident_id"], reason, trigger])[:48]
        previous = r.store.db.execute("SELECT response_json FROM business_requests WHERE facility_id=? AND requester_ref=? AND key=?",
                                      (FACILITY, session.username, report_key)).fetchone()
        if previous:
            return {"status": "accepted", "run_id": body.run_id, "incident_id": incident["incident_id"],
                    "report": json.loads(previous["response_json"]), "reason_code": reason}
        task.consume(session, "report_to_owner")
        report = r.business.execute(session, "report_to_owner", {
            "facility_id": FACILITY, "run_id": body.run_id,
            "based_on_state_version": r.world["state_version"], "policy_version": policy.policy_version,
            "incident_id": incident["incident_id"], "reason_code": reason,
            "summary": "차주 응답 또는 기한을 확인했습니다. 이용 방해 해소는 현재 관측으로 별도 확인합니다."},
            report_key)
        return {"status": "accepted", "run_id": body.run_id, "incident_id": incident["incident_id"],
                "report": report, "reason_code": reason}

    def _observed_scenario(self, session, body, key, decision, snapshot, task):
        impact_type = _IMPACTS[body.scenario]
        incident = snapshot["incident"]
        target = decision.target_ref or snapshot["target_ref"]
        zone = snapshot["target_zone"]
        if not zone:
            return {"status": "held", "reason_code": "ZONE_UNVERIFIED", "run_id": body.run_id}
        if not target:
            return {"status": "held", "reason_code": "TARGET_UNVERIFIED", "run_id": body.run_id}
        try:
            assessment = self.runtime.business.impact_assessment(impact_type, target, zone)
        except ApiError as error:
            return {"status": "held", "reason_code": error.code, "run_id": body.run_id}
        if decision.action == "recheck" and incident:
            notice = snapshot.get("notification")
            if (notice and not notice["response"] and not assessment["clearance_sustained"]
                    and not (impact_type in S1_TYPES and self._has_new_supported_s1_impact(incident))):
                return {"status": "held", "reason_code": "AWAITING_RESPONSE_OR_MOVEMENT",
                        "run_id": body.run_id, "incident_id": incident["incident_id"]}
            return self._recheck(session, body, key, incident, impact_type, zone, task)
        if body.scenario in ("s1b", "s1c") and decision.action == "report" and incident:
            if assessment["clearance_sustained"]:
                return self._recheck(session, body, key, incident, impact_type, zone, task)
            return self._report_issue(session, body, key, incident, snapshot.get("notification"), task)
        if body.scenario == "s2" and decision.action == "report" and assessment["support_status"] == "supported" and assessment["violation_candidate"]:
            return self._report_risk(session, body, key, target, zone, assessment, task)
        if decision.action == "notify" and assessment["support_status"] == "supported" and assessment["violation_candidate"]:
            return self._notify(session, body, key, target, zone, impact_type, assessment["observation_ids"],
                                snapshot["knowledge"], task)
        return {"status": "held", "reason_code": "OBSERVATION_NOT_READY", "run_id": body.run_id}

    def _report_risk(self, session, body, key, target, zone, assessment, task):
        r = self.runtime
        policy = r.knowledge.current_policy(FACILITY)
        common = {"facility_id": FACILITY, "run_id": body.run_id,
                  "based_on_state_version": r.world["state_version"], "policy_version": policy.policy_version}
        task.consume(session, "create_or_update_incident")
        recorded = r.business.execute(session, "create_or_update_incident", common | {
            "primary_object_id": target, "status": "active",
            "impacts": [{"type": "approach_risk", "zone_id": zone, "object_id": target}],
            "evidence_ids": assessment["observation_ids"],
            "reason_summary": "공개 관측의 차량·보행자 접근 위험 후보"},
            "agent-" + digest([key, "risk_incident"])[:48])
        iid = recorded["result"]["incident_id"]
        plan_id = ident("plan")
        with transaction(r.store.db):
            r.business.insert("plans", plan_id=plan_id, facility_id=FACILITY, run_id=body.run_id,
                incident_id=iid, command_id=None, trigger_followup_id=None,
                steps_json=encoded([{"tool": "report_to_owner"}, {"tool": "request_followup"}]),
                model_ref="autonomous_" + body.mode, policy_version=policy.policy_version,
                budget_json=encoded({"tool_calls": 16, "wall_seconds": 30}), status="active")
        task.consume(session, "report_to_owner")
        report = r.business.execute(session, "report_to_owner", common | {
            "incident_id": iid, "plan_id": plan_id, "reason_code": "APPROACH_RISK_CANDIDATE",
            "summary": "합성 관측에서 차량·보행자 접근 위험 후보를 확인했습니다. 독립 경보 상태를 확인하세요.",
            "evidence_ids": assessment["observation_ids"]},
            "agent-" + digest([key, "risk_report", iid])[:48])
        task.consume(session, "request_followup")
        followup = r.business.execute(session, "request_followup", common | {
            "incident_id": iid, "plan_id": plan_id, "clock": "sim",
            "due_sim_time_ms": r.world["sim_time_ms"] + min(1000, policy.execution_rules.spatial_followup_sim_ms),
            "condition": "approach_recheck", "max_attempts": 1},
            "agent-" + digest([key, "risk_recheck", iid])[:48])
        with transaction(r.store.db):
            r.business.changed("plans", "plan_id", plan_id, body.run_id,
                steps_json=encoded([{"tool": "report_to_owner", "execution_id": report["execution_id"]},
                                    {"tool": "request_followup", "execution_id": followup["execution_id"]}]))
        return {"status": "accepted", "run_id": body.run_id, "incident_id": iid,
                "plan_id": plan_id, "report": report, "followup": followup}

    def _notify(self, session, body, key, target, zone, impact_type, evidence_ids, knowledge, task):
        r = self.runtime
        policy = r.knowledge.current_policy(FACILITY)
        common = {"facility_id": FACILITY, "run_id": body.run_id, "based_on_state_version": r.world["state_version"],
                  "policy_version": policy.policy_version}
        task.consume(session, "create_or_update_incident")
        incident = r.business.execute(session, "create_or_update_incident", common | {
            "primary_object_id": target, "status": "active", "impacts": [{"type": impact_type, "zone_id": zone,
                "object_id": target}], "evidence_ids": evidence_ids,
            "reason_summary": "공개 관측에서 확인된 이용 방해 후보"}, "agent-" + digest([key, "incident"])[:48])
        iid = incident["result"]["incident_id"]
        if knowledge["status"] != "matched":
            return {"status": "held", "reason_code": "KNOWLEDGE_" + knowledge["status"].upper(),
                    "run_id": body.run_id, "incident_id": iid}
        try:
            task.consume(session, "resolve_vehicle_recipient")
            recipient = r.business.resolve_recipient(target)
        except ApiError as error:
            if error.code == "RECIPIENT_UNVERIFIED":
                return self._review_unverified_recipient(session, body, iid, task)
            return {"status": "held", "reason_code": error.code, "run_id": body.run_id, "incident_id": iid}
        row = r.business.scoped("incidents", iid, "incident_id")
        if row["status"] == "needs_review":
            # Review holds survive deduplication. A now-verified driver can
            # receive a new plan only after all cumulative impacts are fresh.
            impact_values = r.business.incident_assessments(row)
            if (not impact_values or not all(value["support_status"] == "supported" for _, value in impact_values)
                    or not any(value["violation_candidate"] for _, value in impact_values)):
                return {"status": "held", "reason_code": "OBSERVATION_CHANGED", "run_id": body.run_id, "incident_id": iid}
            task.consume(session, "create_or_update_incident")
            r.business.execute(session, "create_or_update_incident", common | {
                "incident_id": iid, "expected_resource_version": row["resource_version"],
                "primary_object_id": target, "status": "active",
                "impacts": [impact.model_dump() for impact, _ in impact_values],
                "evidence_ids": sorted({oid for _, value in impact_values for oid in value["observation_ids"]}),
                "reason_summary": "현재 등록 차주와 새 관측을 재검증해 연락 계획을 재개합니다."},
                "agent-" + digest([key, "resume_incident", iid, row["resource_version"]])[:48])
            row = r.business.scoped("incidents", iid, "incident_id")
        previous = r.business.previous_vehicle_contact(iid, recipient["user_id"])
        sequence = previous["contact_sequence"] + 1 if previous else 1
        pid = ident("plan")
        with transaction(r.store.db):
            r.business.insert("plans", plan_id=pid, facility_id=FACILITY, run_id=body.run_id, incident_id=iid,
                command_id=None, trigger_followup_id=None, steps_json=encoded([{"tool": "notify_vehicle_user", "status": "proposed"}]),
                model_ref="autonomous_" + body.mode, policy_version=policy.policy_version,
                budget_json=encoded({"tool_calls": 16, "wall_seconds": 30}), status="active")
        args = common | {"incident_id": iid, "plan_id": pid, "recipient_ref": recipient["recipient_ref"],
            "expected_resource_version": row["resource_version"], "contact_sequence": sequence,
            "template_args": {"zone_label": zone},
            "knowledge_evidence": {"retrieval_id": knowledge["retrieval_id"],
                                   "reference_ids": [x["reference_id"] for x in knowledge["references"]]}}
        try:
            task.consume(session, "notify_vehicle_user")
            notice = r.business.execute(session, "notify_vehicle_user", args,
                "agent-" + digest([key, "notify", iid, sequence])[:48])
            due = later(r.business.clock(), policy.execution_rules.response_timeout_wall_ms)
            task.consume(session, "request_followup")
            followup = r.business.execute(session, "request_followup", common | {
                "incident_id": iid, "clock": "wall", "due_at": due, "condition": "response_timeout",
                "max_attempts": 1}, "agent-" + digest([key, "followup", iid])[:48])
            with transaction(r.store.db):
                r.business.changed("plans", "plan_id", pid, body.run_id, steps_json=encoded([
                    {"tool": "notify_vehicle_user", "execution_id": notice["execution_id"]},
                    {"tool": "request_followup", "execution_id": followup["execution_id"]}]))
            return {"status": "accepted", "run_id": body.run_id, "incident_id": iid, "plan_id": pid,
                    "execution": notice, "followup": followup}
        except ApiError as error:
            with transaction(r.store.db):
                r.business.changed("plans", "plan_id", pid, body.run_id, status="held")
            return {"status": "held", "reason_code": error.code, "run_id": body.run_id,
                    "incident_id": iid, "plan_id": pid}

    def _review_unverified_recipient(self, session, body, iid, task):
        """Hold private contact and report once through the normal owner guards."""
        r = self.runtime
        row = r.business.scoped("incidents", iid, "incident_id")
        held = {"status": "held", "reason_code": "RECIPIENT_UNVERIFIED",
                "run_id": body.run_id, "incident_id": iid}
        if row["run_id"] != body.run_id or row["status"] in CLOSED:
            return held | {"reason_code": "INCIDENT_CHANGED"}
        impact_values = r.business.incident_assessments(row)
        impacts = [impact.model_dump() for impact, _ in impact_values]
        assessments = [value for _, value in impact_values]
        if (not assessments or not all(a["support_status"] == "supported" for a in assessments)
                or not any(a["violation_candidate"] for a in assessments)):
            return held | {"reason_code": "OBSERVATION_CHANGED"}
        evidence = sorted({oid for assessment in assessments for oid in assessment["observation_ids"]})
        policy = r.knowledge.current_policy(FACILITY)
        common = {"facility_id": FACILITY, "run_id": body.run_id,
                  "based_on_state_version": r.world["state_version"], "policy_version": policy.policy_version,
                  "incident_id": iid}
        stored_keys = {(entry["type"], entry["zone_id"]) for entry in r.store.db.execute(
            "SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (iid,))}
        if row["status"] != "needs_review" or stored_keys != {(impact.type, impact.zone_id) for impact, _ in impact_values}:
            task.consume(session, "create_or_update_incident")
            r.business.execute(session, "create_or_update_incident", common | {
                "primary_object_id": row["primary_object_id"], "status": "needs_review",
                "expected_resource_version": row["resource_version"], "impacts": impacts,
                "evidence_ids": evidence, "reason_summary": "등록 차주 연결을 확인할 수 없어 검토가 필요합니다."},
                "agent-review-" + digest([iid, row["resource_version"], "RECIPIENT_UNVERIFIED"])[:48])
        # A new job key, another operator or an unknown delivery result must
        # not create a second report for the same unresolved incident/reason.
        previous = r.store.db.execute("""SELECT e.* FROM executions e JOIN notifications n USING(execution_id)
            WHERE e.incident_id=? AND n.purpose='owner_report'
            AND json_extract(n.message_json,'$.reason_code')='RECIPIENT_UNVERIFIED'
            ORDER BY e.rowid LIMIT 1""", (iid,)).fetchone()
        if previous:
            return held | {"report": r.business.execution_view(previous)}
        try:
            task.consume(session, "report_to_owner")
            report = r.business.execute(session, "report_to_owner", common | {
                "reason_code": "RECIPIENT_UNVERIFIED", "evidence_ids": evidence,
                "summary": "이용 방해 후보의 등록 차주를 확인할 수 없어 연락을 보류했습니다. 차량 연결과 현장 상황을 검토해 주세요."},
                "agent-recipient-report-" + digest([iid, "RECIPIENT_UNVERIFIED"])[:48])
        except ApiError as error:
            return held | {"report_reason_code": error.code}
        return held | {"report": report}

    def _recheck(self, session, body, key, incident, impact_type, zone, task):
        r = self.runtime
        row = r.business.scoped("incidents", incident["incident_id"], "incident_id")
        if row["status"] in CLOSED or row["run_id"] != body.run_id:
            return {"status": "held", "reason_code": "INCIDENT_CHANGED", "run_id": body.run_id}
        impact_values = r.business.incident_assessments(row)
        assessments = [value for _, value in impact_values]
        evidence_ids = sorted({oid for value in assessments for oid in value["observation_ids"]})
        if (impact_type in ("aisle_obstruction", "exit_blocked", "bay_intrusion")
                and any(a["support_status"] == "supported" and a["violation_candidate"] for a in assessments)):
            try:
                r.business.resolve_recipient(row["primary_object_id"])
            except ApiError as error:
                if error.code == "RECIPIENT_UNVERIFIED":
                    return self._review_unverified_recipient(session, body, row["incident_id"], task)
        if not assessments or not all(a["support_status"] == "supported" for a in assessments):
            status = "needs_review"
        elif all(a["clearance_sustained"] for a in assessments):
            status = "resolved"
        elif any(a.get("occupied", a["violation_candidate"]) for a in assessments):
            status = "monitoring"
        else:
            return {"status": "held", "reason_code": "AWAITING_SUSTAINED_CLEARANCE", "run_id": body.run_id,
                    "incident_id": row["incident_id"]}
        stored_keys = {(entry["type"], entry["zone_id"]) for entry in r.store.db.execute(
            "SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (row["incident_id"],))}
        if status == row["status"] and stored_keys == {(impact.type, impact.zone_id) for impact, _ in impact_values}:
            return {"status": "held", "reason_code": "AWAITING_NEW_EVIDENCE", "run_id": body.run_id,
                    "incident_id": row["incident_id"]}
        policy = r.knowledge.current_policy(FACILITY)
        task.consume(session, "create_or_update_incident")
        try:
            changed = r.business.execute(session, "create_or_update_incident", {
                "facility_id": FACILITY, "run_id": body.run_id, "based_on_state_version": r.world["state_version"],
                "policy_version": policy.policy_version, "incident_id": row["incident_id"],
                "expected_resource_version": row["resource_version"], "primary_object_id": row["primary_object_id"],
                "status": status, "impacts": [impact.model_dump() for impact, _ in impact_values],
                "evidence_ids": evidence_ids, "reason_summary": "최신 공개 관측의 후속 확인"},
                "agent-" + digest([key, "recheck", row["incident_id"], evidence_ids])[:48])
        except ApiError as error:
            if error.code != "RECOVERY_NOT_CONFIRMED":
                raise
            return {"status": "held", "reason_code": error.code, "run_id": body.run_id,
                    "incident_id": row["incident_id"]}
        if status == "resolved":
            with transaction(r.store.db):
                for plan in r.store.db.execute("SELECT plan_id FROM plans WHERE incident_id=? AND status='active'", (row["incident_id"],)):
                    r.business.changed("plans", "plan_id", plan[0], body.run_id, status="completed")
        return {"status": status, "run_id": body.run_id, "incident_id": row["incident_id"], "execution": changed}

    def _command(self, session, body, key, decision, snapshot, task):
        command = snapshot["command"]
        if not command:
            return {"status": "held", "reason_code": "COMMAND_REQUIRED", "run_id": body.run_id}
        row = self.runtime.business.scoped("commands", command["command_id"], "command_id")
        if row["resource_version"] != command["resource_version"] or row["cancellation_requested_at"]:
            return {"status": "held", "reason_code": "COMMAND_CHANGED", "run_id": body.run_id}
        current_goal = self._command_goal(row, json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else None)
        if current_goal != command["normalized_goal"]:
            return {"status": "held", "reason_code": "COMMAND_CHANGED", "run_id": body.run_id}
        if current_goal["action"] == "complete":
            plan = self._command_plan(body, row, current_goal)
            with transaction(self.runtime.store.db):
                self.runtime.business.changed("plans", "plan_id", plan["plan_id"], body.run_id, status="completed")
                self.runtime.business.changed("commands", "command_id", row["command_id"], body.run_id,
                                              aggregate_status="succeeded")
            return {"status": "succeeded", "run_id": body.run_id, "command_id": row["command_id"],
                    "plan_id": plan["plan_id"]}
        if decision.action == "hold":
            return {"status": "held", "reason_code": "COMMAND_AWAITING_REVIEW",
                    "run_id": body.run_id, "command_id": row["command_id"]}
        if decision.action == "clarify":
            if current_goal["kind"] == "ambiguous":
                return {"status": "clarification_required", "run_id": body.run_id,
                        "command_id": row["command_id"], "reason_code": "TARGET_OR_SCOPE_REQUIRED"}
            plan = self._command_plan(body, row, current_goal)
            return {"status": "confirmation_required", "run_id": body.run_id,
                    "command_id": row["command_id"], "plan_id": plan["plan_id"],
                    "reason_code": "OWNER_CONFIRMATION_REQUIRED"}
        if not current_goal["confirmed"] or decision.action != current_goal["action"]:
            return {"status": "held", "run_id": body.run_id, "command_id": row["command_id"],
                    "reason_code": "COMMAND_PLAN_NOT_CONFIRMED"}
        # Device operation is implemented by the server integration point. A
        # model's answer alone never changes a gate or plays a broadcast.
        executor = getattr(self.runtime, "execute_device_action", None)
        if executor is None or decision.action not in ("announce", "restrict_entry"):
            return {"status": "held", "run_id": body.run_id, "command_id": row["command_id"],
                    "reason_code": "DEVICE_ACTION_NOT_CONNECTED"}
        if snapshot["knowledge"]["status"] != "matched":
            return {"status": "held", "run_id": body.run_id, "command_id": row["command_id"],
                    "reason_code": "KNOWLEDGE_REQUIRED"}
        # The server chooses the approved template and exact device; no model
        # generated text, zone, target or instruction becomes an actuator arg.
        plan = self._command_plan(body, row, current_goal)
        if plan["status"] != "active":
            return {"status": "held", "run_id": body.run_id, "command_id": row["command_id"],
                    "reason_code": "PLAN_NOT_ACTIVE"}
        zone_id = current_goal.get("zone_id") if decision.action == "announce" else None
        message_id = ("no_litter_notice" if current_goal["kind"] == "zone_notice" else "closing_notice") if zone_id else None
        action = "play_announcement" if decision.action == "announce" else "set_entry_policy"
        steps = json.loads(plan["steps_json"])
        matching = [step for step in steps if step.get("tool") == action and step.get("zone_id") == zone_id]
        step_id = matching[0].get("step_id") if len(matching) == 1 else None
        if any(step.get("step_id") for step in steps) and not step_id:
            raise ApiError(409, "PLAN_STEP_CHANGED", "확인된 계획 단계의 명시적 연결을 확인하세요.")
        return {"status": "pending", "run_id": body.run_id, "command_id": row["command_id"],
                "device_intent": {"run_id": body.run_id, "command_id": row["command_id"],
                    "plan_id": plan["plan_id"], "step_id": step_id, "zone_id": zone_id, "message_id": message_id,
                    "action": "play_announcement" if decision.action == "announce" else "set_entry_policy",
                    "knowledge_evidence": KnowledgeEvidence(retrieval_id=snapshot["knowledge"]["retrieval_id"],
                        reference_ids=[x["reference_id"] for x in snapshot["knowledge"]["references"]]),
                    "key": "agent-" + digest([key, decision.action, zone_id, row["resource_version"]])[:48]}}

    def _command_plan(self, body, row, goal):
        r = self.runtime
        existing = r.store.db.execute("SELECT * FROM plans WHERE command_id=? AND trigger_followup_id IS NULL ORDER BY rowid DESC LIMIT 1", (row["command_id"],)).fetchone()
        if existing and existing["status"] in ("proposed", "active") and self._plan_matches(existing, goal["kind"]):
            return existing
        if goal["confirmed"]:
            raise ApiError(409, "PLAN_NOT_READY", "확인된 목표에 맞는 활성 계획이 없습니다.")
        if goal["kind"] == "ambiguous":
            raise ApiError(409, "GOAL_AMBIGUOUS", "지시 범위를 먼저 확인하세요.")
        policy = r.knowledge.current_policy(FACILITY)
        steps = ([{"tool": "play_announcement", "zone_id": "announcement-a"},
                  {"tool": "play_announcement", "zone_id": "announcement-b"},
                  {"tool": "set_entry_policy", "depends_on": "both_announcements_played"}]
                 if goal["kind"] == "closing" else
                 [{"tool": "play_announcement", "zone_id": "announcement-a"}])
        plan_id = ident("plan")
        for index, step in enumerate(steps, 1):
            step["step_id"] = f"{plan_id}-step-{index}"
            step["execution_ids"] = []
        with transaction(r.store.db):
            r.business.insert("plans", plan_id=plan_id, facility_id=FACILITY, run_id=body.run_id,
                incident_id=None, command_id=row["command_id"], trigger_followup_id=None,
                steps_json=encoded(steps), model_ref="autonomous_" + body.mode,
                policy_version=policy.policy_version, budget_json=encoded({"model_calls": 4, "tool_calls": 16,
                    "wall_seconds": 30}), status="proposed")
            previous = json.loads(row["normalized_goal_json"]) if row["normalized_goal_json"] else {}
            r.business.changed("commands", "command_id", row["command_id"], body.run_id,
                normalized_goal_json=encoded({"kind": goal["kind"], "confirmed": False,
                                              "clarified": previous.get("clarified") is True}),
                aggregate_status="held")
        return r.business.scoped("plans", plan_id, "plan_id")

    @staticmethod
    def _plan_matches(plan, kind):
        steps = json.loads(plan["steps_json"])
        return (kind == "closing" and len(steps) == 3
                and [step.get("tool") for step in steps] ==
                    ["play_announcement", "play_announcement", "set_entry_policy"]
                and [step.get("zone_id") for step in steps[:2]] ==
                    ["announcement-a", "announcement-b"]
                or kind == "zone_notice" and len(steps) == 1
                and steps[0].get("tool") == "play_announcement"
                and steps[0].get("zone_id") == "announcement-a")

    async def close(self):
        self.enabled = None
        self.closed = True
        while self.active:
            await asyncio.sleep(0.01)
