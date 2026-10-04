"""Explicit local HTTP smoke run; importing this module never creates a run.

Use the existing project environment:
  .venv/Scripts/python.exe code/frontend/tests/integration_smoke.py --run

This creates one synthetic S1-a run through frontend proxy 8000. No model
endpoint is called. Cookies, CSRF, passwords, request keys and raw bodies stay
in memory and are never included in stdout or the evidence JSON.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import httpx


ORIGIN = "http://127.0.0.1:8000"
FACILITY = "fac-demo-01"
FIXTURE = "s1a-foundation-v1"
ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "Work_tree" / "artifacts" / "frontend-integration" / "api-smoke.json"


class SmokeFailure(Exception):
    def __init__(self, check: str, status: int = 0, code: str = "CHECK_FAILED"):
        super().__init__(check)
        self.check = check
        self.status = status
        self.code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code) else "HTTP_ERROR"


def object_value(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def item_list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def require(condition: bool, check: str, report: dict) -> None:
    report["checks"].append({"check": check, "status": "passed" if condition else "failed"})
    if not condition:
        raise SmokeFailure(check)


def public_id(value: object, check: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise SmokeFailure(check, code="INVALID_PUBLIC_ID")
    return value


class Session:
    def __init__(self, stack: ExitStack, report: dict):
        self.client = stack.enter_context(httpx.Client(
            base_url=ORIGIN, timeout=5.0, trust_env=False, follow_redirects=False,
        ))
        self.report = report
        self.csrf = ""

    def request(self, check: str, method: str, path: str, *, body: dict | None = None,
                expected: int = 200, attempt: str | None = None, record: bool = True) -> dict:
        headers = {"Origin": ORIGIN}
        if method != "GET" and path != "/api/v1/auth/session":
            headers["X-CSRF-Token"] = self.csrf
            headers["Idempotency-Key"] = attempt or str(uuid4())
        try:
            response = self.client.request(method, path, headers=headers, json=body)
        except httpx.RequestError:
            raise SmokeFailure(check, code="LOCAL_TRANSPORT_ERROR") from None
        try:
            data = object_value(response.json())
        except (ValueError, UnicodeError):
            raise SmokeFailure(check, response.status_code, "INVALID_RESPONSE") from None
        code = object_value(data.get("error")).get("code", "HTTP_ERROR")
        if response.status_code != expected:
            raise SmokeFailure(check, response.status_code, code if isinstance(code, str) else "HTTP_ERROR")
        if record:
            entry = {"check": check, "status": "passed", "http_status": response.status_code}
            if response.status_code >= 400:
                entry["error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code) else "HTTP_ERROR"
            self.report["checks"].append(entry)
        return data

    def login(self, account: str, role: str) -> dict:
        self.request(f"{role}.login", "POST", "/api/v1/auth/session", body={
            "username": account, "password": "parking-demo-only",
        })
        me = self.request(f"{role}.session", "GET", "/api/v1/me")
        token = me.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise SmokeFailure(f"{role}.session", code="SESSION_UNAVAILABLE")
        self.csrf = token
        grants = item_list(me.get("facility_roles"))
        require(any(g.get("facility_id") == FACILITY and
                    role in (g.get("roles") if isinstance(g.get("roles"), list) else [])
                    for g in grants), f"{role}.grant", self.report)
        return me

    def inbox(self, check: str, *, record: bool = True) -> list[dict]:
        return item_list(self.request(check, "GET", "/api/v1/notifications?cursor=0&limit=100", record=record).get("items"))


def wait_for_notice(session: Session, notification: str, check: str) -> dict:
    deadline = time.monotonic() + 5.0
    while True:
        notice = next((item for item in session.inbox(check, record=False)
                       if item.get("notification_id") == notification), None)
        if notice is not None:
            session.report["checks"].append({"check": check, "status": "passed", "http_status": 200})
            return notice
        if time.monotonic() >= deadline:
            raise SmokeFailure(check, code="DELIVERY_POLL_TIMEOUT")
        time.sleep(0.05)


def run_smoke(report: dict) -> None:
    with ExitStack() as stack:
        operator = Session(stack, report)
        operator.login("demo-operator", "test_operator")
        ready = operator.request("service.readiness", "GET", "/health/ready")
        require(ready.get("test_control_enabled") is True, "service.test_control_enabled", report)
        created = operator.request("run.create", "POST", "/api/v1/test/runs", expected=201, body={
            "facility_id": FACILITY, "fixture_ref": FIXTURE, "seed": 42, "config_ref": "foundation-v1",
        })
        run_id = public_id(created.get("run_id"), "run.id")
        report["ids"]["run_id"] = run_id
        require(created.get("run_status") == "paused", "run.starts_paused", report)
        last = created
        for _ in range(60):
            last = operator.request("run.step", "POST", f"/api/v1/test/runs/{run_id}/control",
                                    body={"action": "step"}, record=False)
            time.sleep(0.02)
        snapshot = object_value(last.get("snapshot"))
        require(last.get("run_status") == "paused" and snapshot.get("run_id") == run_id,
                "run.60_steps_paused", report)
        base = f"/api/v1/facilities/{FACILITY}"
        state_path = f"{base}/state?run_id={run_id}"
        before = operator.request("operator.state_before_notify", "GET", state_path)
        notify_attempt = str(uuid4())
        notify_body = {"run_id": run_id, "action": "notify"}
        notified = operator.request("manual.notify", "POST", "/api/v1/test/s1a/manual",
                                    body=notify_body, attempt=notify_attempt)
        require(notified.get("status") == "accepted" and notified.get("mode") == "manual",
                "manual.notify_accepted", report)
        incident = public_id(notified.get("incident_id"), "incident.id")
        execution = object_value(notified.get("execution"))
        execution_id = public_id(execution.get("execution_id"), "execution.id")
        notification = public_id(object_value(execution.get("result")).get("notification_id"), "notification.id")
        report["ids"].update(incident_id=incident, execution_id=execution_id,
                             notification_id=notification,
                             plan_id=public_id(notified.get("plan_id"), "plan.id"))
        incident_list = operator.request("operator.incident_list", "GET", f"{base}/incidents?run_id={run_id}&cursor=0&limit=100")
        require(any(item.get("incident_id") == incident for item in item_list(incident_list.get("items"))),
                "incident.in_current_run_list", report)
        detail = operator.request("operator.incident_detail", "GET", f"/api/v1/incidents/{incident}")
        require(detail.get("run_id") == run_id and any(i.get("type") == "aisle_obstruction"
                for i in item_list(detail.get("impacts"))), "incident.supported_impact", report)

        driver = Session(stack, report)
        driver.login("demo-driver", "driver")
        own = driver.request("driver.own_state", "GET", state_path)
        own_snapshot = object_value(own.get("snapshot"))
        require(own.get("view_scope") == "own_vehicles" and
                own.get("registered_vehicle_ids") == ["veh-demo-02"] and
                {item.get("object_id") for item in item_list(own_snapshot.get("objects"))} == {"obj-car-02"} and
                own_snapshot.get("devices") == [], "driver.state_scoped_to_own_vehicle", report)
        denied_paths = {
            "driver.map_denied": f"{base}/map",
            "driver.relationships_denied": f"{base}/relationships",
            "driver.devices_denied": f"{base}/devices?run_id={run_id}",
            "driver.incidents_denied": f"{base}/incidents?run_id={run_id}",
            "driver.incident_detail_denied": f"/api/v1/incidents/{incident}",
            "driver.execution_denied": f"/api/v1/executions/{execution_id}",
        }
        for check, path in denied_paths.items():
            driver.request(check, "GET", path, expected=403)
        driver.request("driver.owner_command_denied", "POST", f"{base}/commands", expected=403, body={
            "run_id": run_id, "purpose": "operational_goal", "text": "가상 영업 종료 시험",
            "based_on_state_version": object_value(before.get("snapshot")).get("state_version"),
        })
        driver.request("driver.manual_denied", "POST", "/api/v1/test/s1a/manual", expected=403, body=notify_body)
        notice = wait_for_notice(driver, notification, "driver.notification_delivered")
        require(notice.get("delivery_status") == "channel_accepted" and notice.get("mode") == "live"
                and notice.get("purpose") == "move_request" and notice.get("incident_id") == incident,
                "notification.local_inbox_channel_accepted", report)
        delivered_execution = operator.request("operator.delivery_execution", "GET", f"/api/v1/executions/{execution_id}")
        require(delivered_execution.get("status") == "succeeded", "execution.channel_send_succeeded", report)

        other_driver = Session(stack, report)
        other_driver.login("demo-driver-2", "driver")
        require(not all_item_ids(other_driver.inbox("other_driver.inbox")) & {notification},
                "other_driver.notification_hidden", report)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        other_driver.request("other_driver.receipt_denied", "POST", f"/api/v1/notifications/{notification}/receipts",
                             expected=404, body={"client_request_id": str(uuid4()), "received_at": now})
        other_driver.request("other_driver.response_denied", "POST", f"/api/v1/notifications/{notification}/responses",
                             expected=404, body={"client_request_id": str(uuid4()), "response": "question"})
        receipt_body = {"client_request_id": str(uuid4()), "received_at": now}
        receipt_attempt = str(uuid4())
        receipt_path = f"/api/v1/notifications/{notification}/receipts"
        receipt = driver.request("driver.receipt", "POST", receipt_path, body=receipt_body, attempt=receipt_attempt)
        receipt_repeat = driver.request("driver.receipt_replay", "POST", receipt_path,
                                        body=receipt_body, attempt=receipt_attempt)
        receipt_id = public_id(receipt.get("receipt_id"), "receipt.id")
        report["ids"]["receipt_id"] = receipt_id
        require(receipt_repeat.get("receipt_id") == receipt_id, "receipt.same_attempt_idempotent", report)
        received = next((item for item in driver.inbox("driver.received_inbox")
                         if item.get("notification_id") == notification), {})
        require(received.get("delivery_status") == "client_received", "notification.screen_received", report)
        response_body = {"client_request_id": str(uuid4()), "response": "question"}
        response_attempt = str(uuid4())
        response_path = f"/api/v1/notifications/{notification}/responses"
        response = driver.request("driver.question_response", "POST", response_path,
                                  body=response_body, attempt=response_attempt)
        response_repeat = driver.request("driver.response_replay", "POST", response_path,
                                         body=response_body, attempt=response_attempt)
        response_id = public_id(response.get("response_id"), "response.id")
        report["ids"]["response_id"] = response_id
        require(response_repeat.get("response_id") == response_id, "response.same_attempt_idempotent", report)
        answered = next((item for item in driver.inbox("driver.answered_inbox")
                         if item.get("notification_id") == notification), {})
        answers = item_list(answered.get("responses"))
        require(len(answers) == 1 and answers[0].get("response") == "question" and
                answers[0].get("response_source") == "user", "response.question_saved_once", report)
        after = operator.request("operator.state_after_response", "GET", state_path)
        require(object_value(before.get("snapshot")).get("objects") == object_value(after.get("snapshot")).get("objects")
                and object_value(before.get("snapshot")).get("sim_time_ms") == object_value(after.get("snapshot")).get("sim_time_ms"),
                "response_does_not_move_vehicle", report)
        reviewed = operator.request("manual.review_question", "POST", "/api/v1/test/s1a/manual", body={
            "run_id": run_id, "action": "review_timeout", "incident_id": incident,
        })
        require(reviewed.get("status") == "escalated_review", "manual.question_requires_owner_review", report)
        report_execution = object_value(reviewed.get("report"))
        report_notification = public_id(object_value(report_execution.get("result")).get("notification_id"), "owner_report.id")
        report["ids"].update(owner_notification_id=report_notification,
                             report_execution_id=public_id(report_execution.get("execution_id"), "report_execution.id"))
        owner = Session(stack, report)
        owner.login("demo-owner", "owner")
        owner.request("owner.manual_denied", "POST", "/api/v1/test/s1a/manual", expected=403, body=notify_body)
        owner_notice = wait_for_notice(owner, report_notification, "owner.report_delivered")
        require(owner_notice.get("purpose") == "owner_report" and owner_notice.get("incident_id") == incident
                and owner_notice.get("delivery_status") == "channel_accepted" and owner_notice.get("mode") == "live",
                "owner.report_is_own_local_notification", report)
        require(notification not in all_item_ids(owner.inbox("owner.inbox_scope")), "owner.cannot_receive_driver_notification", report)
        final_incident = owner.request("owner.incident_detail", "GET", f"/api/v1/incidents/{incident}")
        require(final_incident.get("status") not in {"resolved", "closed_no_issue", "closed_false_positive"},
                "question_and_owner_report_do_not_resolve_incident", report)
        report["statuses"] = {
            "run": after.get("run_status"), "notification": answered.get("delivery_status"),
            "response": answers[0].get("response"), "incident": final_incident.get("status"),
            "owner_notification": owner_notice.get("delivery_status"), "manual_review": reviewed.get("status"),
        }


def all_item_ids(items: list[dict]) -> set[str]:
    return {item["notification_id"] for item in items if isinstance(item.get("notification_id"), str)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit synthetic API smoke run through frontend proxy 8000.")
    parser.add_argument("--run", action="store_true", help="Authorize this invocation to create and step one synthetic run.")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return 0
    report = {"status": "running", "transport": "frontend_proxy_8000", "fixture_id": FIXTURE,
              "ids": {"facility_id": FACILITY}, "checks": [], "statuses": {}}
    try:
        run_smoke(report)
        report["status"] = "passed"
    except SmokeFailure as failure:
        report["status"] = "failed"
        if not any(item.get("check") == failure.check and item.get("status") == "failed" for item in report["checks"]):
            report["checks"].append({"check": failure.check, "status": "failed",
                                     "http_status": failure.status, "error_code": failure.code})
    except Exception:
        # Exception strings can contain request data. Never write or print them.
        report["status"] = "failed"
        report["checks"].append({"check": "runner", "status": "failed", "http_status": 0, "error_code": "RUNNER_ERROR"})
    output = json.dumps(report, ensure_ascii=False, indent=2)
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
