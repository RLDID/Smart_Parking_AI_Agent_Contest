"""Opt-in synthetic inbox consumers, using the same recipient and reply checks."""
from copy import deepcopy
import json
import time

from agent.tools import validate_session
from backend.auth import ApiError, Session
from backend.business import CLOSED, instant
from backend.knowledge import transaction
from contracts.business import ReceiptInput, ResponseInput
from contracts.synthetic_users import SyntheticUserPolicy


def migrate_synthetic_users(db):
    with transaction(db):
        db.execute("""CREATE TABLE IF NOT EXISTS synthetic_inbox_events (
            notification_id TEXT PRIMARY KEY REFERENCES notifications(notification_id),
            run_id TEXT NOT NULL REFERENCES runs(run_id), mode TEXT NOT NULL,
            receipt_id TEXT, response_id TEXT, action_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('responded','queued','ignored','held')),
            reason_code TEXT NOT NULL, created_at TEXT NOT NULL)""")


class SyntheticUsers:
    def __init__(self, runtime):
        self.runtime = runtime

    def configure(self, session, run_id, body, key):
        r = self.runtime
        validate_session(r, session)
        if session.role != "test_operator":
            raise ApiError(403, "FORBIDDEN", "합성 이용자 설정은 시험 운영자만 변경할 수 있습니다.")
        r.ensure_run(run_id)
        if r.world.get("replay_state") is not None:
            raise ApiError(409, "REPLAY_INPUT_REJECTED", "기록 재생 중에는 새 합성 반응을 설정하지 않습니다.")
        fingerprint, old = r.business.key(session.username, key, "synthetic_user_policy", {"run_id": run_id, **body.model_dump()})
        if old:
            return old
        if r.failure or r.world["recovery_required"]:
            raise ApiError(409, "RECOVERY_REQUIRED", "현재 실행 복구부터 확인하세요.")
        if r.world["state_version"] != body.expected_state_version:
            raise ApiError(409, "RESOURCE_CHANGED", "현재 실행 버전을 확인하세요.")
        candidate = deepcopy(r.world)
        policy = SyntheticUserPolicy.model_validate(body.model_dump(exclude={"expected_state_version"})).model_dump()
        candidate["synthetic_user_policy"] = policy
        candidate["state_version"] += 1
        result = {"run_id": run_id, "mode": "synthetic_consumer", "policy": policy,
                  "applied_state_version": candidate["state_version"]}
        with transaction(r.store.db):
            r.store.commit(candidate, r.event(candidate, "run.updated"))
            r.business.save_key(session.username, key, fingerprint, result)
            r.business.audit(session.username, "synthetic_user_policy", run_id, run_id)
        r.world = candidate
        return result

    def process(self):
        """Caller holds the runtime lock. Never infer delivery or a customer identity."""
        r = self.runtime
        if r.failure or not r.world or r.world["recovery_required"] or r.world.get("replay_state") is not None:
            return
        policy = SyntheticUserPolicy.model_validate(r.world.get("synthetic_user_policy", {}))
        if policy.mode == "manual":
            return
        b = r.business
        rows = b.db.execute("""SELECT n.* FROM notifications n
            WHERE n.run_id=? AND n.purpose='move_request' AND n.mode='live'
            AND n.delivery_status IN ('channel_accepted','client_received')
            AND NOT EXISTS (SELECT 1 FROM synthetic_inbox_events s WHERE s.notification_id=n.notification_id)
            ORDER BY n.rowid LIMIT 16""", (r.world["run_id"],)).fetchall()
        for row in rows:
            age_ms = (instant(b.clock()) - instant(row["updated_at"])).total_seconds() * 1000
            if age_ms < policy.response_delay_ms:
                continue
            username, nid = row["recipient_user_id"], row["notification_id"]
            if r.store.registry.role(username) != "driver" or not b.notification_allowed(username, row):
                continue
            incident = b.scoped("incidents", row["incident_id"], "incident_id")
            if incident["status"] in CLOSED:
                continue
            action_key = "synthetic-action:" + nid
            receipt_id = response_id = None
            status, reason = "ignored", "SYNTHETIC_SILENT"
            candidate = None
            with transaction(b.db):
                if policy.mode != "silent":
                    session = Session(username, "driver", "", time.monotonic() + 30)
                    validate_session(r, session)
                    existing = b.db.execute("SELECT response_id,response FROM notification_responses WHERE notification_id=? ORDER BY rowid DESC LIMIT 1", (nid,)).fetchone()
                    # An actual person's reply is never replaced by a synthetic one.
                    if existing:
                        response_id, response = existing["response_id"], existing["response"]
                        reason = "EXISTING_RESPONSE"
                    else:
                        receipt_id = b.reply(session, nid, "receipt", ReceiptInput(
                            client_request_id="synthetic-receipt:" + nid, received_at=b.clock()), "synthetic-receipt:" + nid)["receipt_id"]
                        response = policy.mode
                        response_id = b.reply(session, nid, "response", ResponseInput(
                            client_request_id="synthetic-response:" + nid, response=response,
                            text="합성 이용자 시험 반응"), "synthetic-response:" + nid)["response_id"]
                        reason = "SYNTHETIC_RESPONSE"
                    status = "responded"
                    if response == "will_move" and not existing:
                        from simulator.environment import queue_vehicle_response
                        execution = b.scoped("executions", row["execution_id"], "execution_id")
                        object_id = json.loads(execution["payload_json"])["_server"]["object_id"]
                        candidate = deepcopy(r.world)
                        try:
                            queued = queue_vehicle_response(candidate, object_id, response, action_key=action_key,
                                                            delay_ms=policy.movement_delay_ms)
                            if queued["status"] != "queued":
                                raise ValueError("Unsupported synthetic movement")
                            candidate["state_version"] += 1
                            status = "queued"
                        except ValueError:
                            candidate = None
                            status, reason = "held", "SYNTHETIC_MOVEMENT_UNSUPPORTED"
                b.db.execute("INSERT INTO synthetic_inbox_events VALUES (?,?,?,?,?,?,?,?,?)",
                    (nid, row["run_id"], policy.mode, receipt_id, response_id, action_key, status, reason, b.clock()))
                if candidate is not None:
                    r.store.commit(candidate, r.event(candidate, "run.updated"))
                b.audit(username, "synthetic_inbox_consumer", nid, row["run_id"], status, reason)
            if candidate is not None:
                r.world = candidate

    def validate_pending(self, candidate):
        """Revoke queued responses without undoing already observed movement."""
        r, b = self.runtime, self.runtime.business
        rows = b.db.execute("""SELECT n.*,s.action_key FROM synthetic_inbox_events s
            JOIN notifications n USING(notification_id) WHERE s.run_id=? AND s.status='queued'""",
            (candidate["run_id"],)).fetchall()
        for row in rows:
            incident = b.scoped("incidents", row["incident_id"], "incident_id")
            execution = b.scoped("executions", row["execution_id"], "execution_id")
            command = b.scoped("commands", execution["command_id"], "command_id") if execution["command_id"] else None
            if (r.store.registry.role(row["recipient_user_id"]) != "driver"
                    or not b.notification_allowed(row["recipient_user_id"], row)
                    or incident["status"] in {"closed_no_issue", "closed_false_positive"}
                    or execution["cancellation_requested_at"]
                    or command and command["cancellation_requested_at"]):
                from simulator.environment import cancel_vehicle_action
                cancel_vehicle_action(candidate, row["action_key"])
                b.db.execute("UPDATE synthetic_inbox_events SET status='held',reason_code='RESPONSE_CONTEXT_CHANGED' WHERE notification_id=?", (row["notification_id"],))
                continue
            action = next((item for item in candidate.get("action_queue", []) if item["action_key"] == row["action_key"]), None)
            if action and action["status"] in {"completed", "exited", "cancelled", "blocked"}:
                status = "responded" if action["status"] in {"completed", "exited"} else "held"
                b.db.execute("UPDATE synthetic_inbox_events SET status=?,reason_code=? WHERE notification_id=?",
                    (status, "SYNTHETIC_MOVEMENT_" + action["status"].upper(), row["notification_id"]))

    def view(self, run_id):
        self.runtime.ensure_run(run_id)
        return {"run_id": run_id, "mode": "synthetic_consumer",
                "policy": self.runtime.world.get("synthetic_user_policy", SyntheticUserPolicy().model_dump()),
                "items": [dict(row) for row in self.runtime.store.db.execute(
                    "SELECT notification_id,mode,status,reason_code,created_at FROM synthetic_inbox_events WHERE run_id=? ORDER BY rowid DESC LIMIT 100", (run_id,))]}
