"""Run the local contest story through the real loopback API once.

This is a development demonstration, not the 194-case acceptance runner. It
uses synthetic observations, demo accounts, and the mock operations adapter.
No live model configuration or external service is accepted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
FACILITY = "fac-demo-01"
PYTHON = Path(sys.executable)
FIXTURES = (
    ("s1a", "s1a-foundation-v1", "foundation-v1", 100),
    ("s1b", "s1b-blocked-v1", "sim0-v1", 230),
    ("s1c", "s1c-overlap-v1", "sim0-v1", 300),
)


class FlowStop(Exception):
    pass


def checked(client, method, path, *, headers=None, payload=None, expected=(200,), stage="request"):
    try:
        response = client.request(method, path, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise FlowStop(f"{stage}: {method} {path.split('?')[0]} {type(exc).__name__}") from exc
    if response.status_code not in expected:
        try:
            error = response.json().get("error", {}).get("code")
        except ValueError:
            error = "INVALID_RESPONSE"
        raise FlowStop(f"{stage}: HTTP {response.status_code} {error or 'UNKNOWN'}")
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise FlowStop(f"{stage}: non-JSON response") from exc


def require(condition, stage, detail):
    if not condition:
        raise FlowStop(f"{stage}: {detail}")


class Rig:
    def __init__(self, origin, report):
        self.origin = origin
        self.report = report
        self.clients = {name: httpx.Client(base_url=origin, timeout=10, trust_env=False)
                        for name in ("operator", "driver", "owner")}
        self.headers = {}
        self.count = 0

    def close(self):
        for client in self.clients.values():
            client.close()

    def record(self, scene, stage, data):
        self.report["scenes"][scene]["steps"].append({"stage": stage, **data})

    def login(self):
        for role, username in (("operator", "demo-operator"), ("driver", "demo-driver"),
                               ("owner", "demo-owner")):
            client = self.clients[role]
            checked(client, "POST", "/api/v1/auth/session", headers={"Origin": self.origin},
                    payload={"username": username, "password": "parking-demo-only"}, stage=f"login/{role}")
            identity = checked(client, "GET", "/api/v1/me", stage=f"me/{role}")
            self.headers[role] = {"Origin": self.origin, "X-CSRF-Token": identity["csrf_token"]}

    def get(self, role, path, *, stage):
        return checked(self.clients[role], "GET", path, stage=stage)

    def write(self, role, method, path, payload, *, stage, expected=(200,)):
        self.count += 1
        headers = {**self.headers[role], "Idempotency-Key": f"e2e-{self.count}-{uuid4().hex[:8]}"}
        return checked(self.clients[role], method, path, headers=headers, payload=payload,
                       expected=expected, stage=stage)

    def run(self, fixture, config, seed):
        return self.write("operator", "POST", "/api/v1/test/runs",
                          {"facility_id": FACILITY, "fixture_ref": fixture, "config_ref": config,
                           "seed": seed}, stage="create_run", expected=(201,))

    def step(self, run, count=1, params=None):
        result = None
        for index in range(count):
            result = self.write("operator", "POST", f"/api/v1/test/runs/{run}/control",
                                {"action": "step", "action_params": params if index == 0 else None},
                                stage=f"step/{index + 1}")
        return result

    def agent(self, run, scenario, *, command_id=None):
        return self.write("operator", "POST", "/api/v1/test/agent/operations",
                          {"run_id": run, "action": "process", "mode": "mock",
                           "scenario": scenario, "command_id": command_id},
                          stage=f"agent/{scenario}")

    def wait_for(self, read, predicate, *, stage, seconds=6):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            last = read()
            if predicate(last):
                return last
            time.sleep(.1)
        raise FlowStop(f"{stage}: expected API state not observed within {seconds}s"
                       + (f" (last={str(last)[:180]})" if last is not None else ""))


def s1(rig, scenario, fixture, config, recovery_ticks):
    scene = rig.report["scenes"][scenario]
    created = rig.run(fixture, config, 42)
    run = created["run_id"]
    scene["run_id"] = run
    rig.step(run, 60)
    state = rig.get("operator", f"/api/v1/facilities/{FACILITY}/state?run_id={run}", stage="observation")
    require(state["snapshot"]["sim_time_ms"] >= 6000, "observation", "initial history is too short")
    rig.record(scenario, "observation", {"status": "passed", "sim_time_ms": state["snapshot"]["sim_time_ms"]})
    result = rig.agent(run, scenario)
    require(result.get("status") == "accepted", "agent", f"result={result.get('status')} reason={result.get('reason_code')}")
    require(result.get("model", {}).get("model_call_count") == 0, "agent", "unexpected live model call")
    incident = result["incident_id"]
    rig.record(scenario, "agent", {"status": "passed", "result": result["status"], "incident_id": incident})
    inbox = rig.wait_for(lambda: rig.get("driver", "/api/v1/notifications", stage="driver_inbox"),
                         lambda item: any(n["incident_id"] == incident for n in item["items"]),
                         stage="driver_inbox")
    notice = next(n for n in inbox["items"] if n["incident_id"] == incident)
    nid = notice["notification_id"]
    rig.record(scenario, "driver_inbox", {"status": "passed", "delivery_status": notice["delivery_status"]})
    rig.write("driver", "POST", f"/api/v1/notifications/{nid}/receipts",
              {"client_request_id": f"receipt-{uuid4().hex}", "received_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
              stage="driver_receipt")
    rig.write("driver", "POST", f"/api/v1/notifications/{nid}/responses",
              {"client_request_id": f"response-{uuid4().hex}", "response": "will_move"},
              stage="driver_response")
    rig.record(scenario, "driver_response", {"status": "passed", "response": "will_move"})
    rig.step(run, recovery_ticks, {"request_vehicle_move": "obj-car-02"})
    current = rig.get("operator", f"/api/v1/facilities/{FACILITY}/state?run_id={run}", stage="movement")
    rig.record(scenario, "movement", {"status": "passed", "sim_time_ms": current["snapshot"]["sim_time_ms"]})
    followup = rig.agent(run, scenario)
    latest = rig.get("operator", f"/api/v1/incidents/{incident}", stage="incident_followup")
    require(followup.get("status") == "resolved" and latest.get("status") == "resolved",
            "incident_followup", f"agent={followup.get('status')} incident={latest.get('status')}")
    rig.record(scenario, "incident_followup", {"status": "passed", "incident_status": latest["status"]})


def s2(rig):
    scenario = "s2"
    scene = rig.report["scenes"][scenario]
    created = rig.run("s2-crossing-v1", "sim0-v1", 6)
    run = created["run_id"]
    scene["run_id"] = run
    alarms = []
    for _ in range(45):
        rig.step(run)
        devices = rig.get("operator", f"/api/v1/facilities/{FACILITY}/devices?run_id={run}", stage="independent_alarm")
        alarms = devices.get("alarms", [])
        if any(a.get("desired_active") and a.get("visual") == a.get("audio") == "on" for a in alarms):
            break
    require(any(a.get("desired_active") and a.get("visual") == a.get("audio") == "on" for a in alarms),
            "independent_alarm", "visual/audio safety alarm not active")
    rig.record(scenario, "independent_alarm", {"status": "passed", "active_alarms": sum(bool(a.get("desired_active")) for a in alarms)})
    result = rig.agent(run, scenario)
    require(result.get("status") == "accepted" and result.get("report", {}).get("status") == "accepted",
            "risk_agent", f"result={result.get('status')} report={result.get('report', {}).get('status')}")
    rig.record(scenario, "risk_agent", {"status": "passed", "incident_id": result.get("incident_id")})
    owner_inbox = rig.wait_for(lambda: rig.get("owner", "/api/v1/notifications", stage="owner_inbox"),
                               lambda item: any(n["purpose"] == "owner_report"
                                                and n["incident_id"] == result["incident_id"]
                                                for n in item["items"]),
                               stage="owner_inbox")
    rig.record(scenario, "owner_inbox", {"status": "passed", "reports": sum(
        n["purpose"] == "owner_report" and n["incident_id"] == result["incident_id"]
        for n in owner_inbox["items"])})


def s3(rig):
    scenario = "s3"
    scene = rig.report["scenes"][scenario]
    created = rig.run("s3-closing-v1", "sim0-v1", 3)
    run = created["run_id"]
    scene["run_id"] = run
    rig.step(run, 60)
    state = rig.get("owner", f"/api/v1/facilities/{FACILITY}/state?run_id={run}", stage="owner_state")
    command = rig.write("owner", "POST", f"/api/v1/facilities/{FACILITY}/commands",
                        {"run_id": run, "purpose": "operational_goal", "text": "문 닫아",
                         "based_on_state_version": state["applied_state_version"]},
                        stage="command", expected=(201,))
    cid = command["command_id"]
    first = rig.agent(run, scenario, command_id=cid)
    require(first.get("status") == "clarification_required", "clarification", f"result={first.get('status')}")
    rig.record(scenario, "clarification", {"status": "passed", "command_id": cid})
    clarified = rig.write("owner", "POST", f"/api/v1/commands/{cid}/clarify",
                          {"expected_resource_version": command["resource_version"], "goal": "closing"},
                          stage="clarify_goal")
    proposal = rig.agent(run, scenario, command_id=cid)
    preview = rig.get("owner", f"/api/v1/commands/{cid}/plan", stage="plan_preview")
    require(proposal.get("status") == "confirmation_required" and preview.get("status") == "proposed"
            and len(preview.get("steps", [])) == 3, "plan_preview", f"proposal={proposal.get('status')} plan={preview.get('status')}")
    rig.record(scenario, "plan_preview", {"status": "passed", "steps": len(preview["steps"]),
                                          "goal": clarified["goal"]})
    confirmed = rig.write("owner", "POST", f"/api/v1/commands/{cid}/confirm",
                          {"expected_resource_version": preview["command_version"]},
                          stage="plan_confirmation")
    require(confirmed.get("plan_id") == preview["plan_id"], "plan_confirmation", "plan identity changed")
    rig.record(scenario, "plan_confirmation", {"status": "passed", "plan_id": preview["plan_id"]})
    for index, zone in enumerate(("announcement-a", "announcement-b"), 1):
        result = rig.agent(run, scenario, command_id=cid)
        require(result.get("status") == "accepted", f"announcement_{index}", f"result={result.get('status')}")
        execution_id = result["execution"]["execution_id"]
        rig.step(run, 3)
        rig.wait_for(lambda: rig.get("operator", f"/api/v1/facilities/{FACILITY}/devices?run_id={run}", stage="broadcast"),
                     lambda item: any(b.get("zone_id") == zone and b.get("simulated_playback") == "played"
                                      for b in item.get("broadcasts", [])), stage=f"broadcast/{zone}")
        rig.wait_for(lambda: rig.get("operator", f"/api/v1/executions/{execution_id}", stage="broadcast_execution"),
                     lambda item: item.get("status") == "succeeded", stage=f"broadcast_execution/{zone}")
        rig.record(scenario, f"broadcast_{index}", {"status": "passed", "zone_id": zone})
    gate = rig.agent(run, scenario, command_id=cid)
    require(gate.get("status") == "succeeded", "entry_policy",
            f"result={gate.get('status')} reason={gate.get('reason_code')}")
    devices = rig.get("operator", f"/api/v1/facilities/{FACILITY}/devices?run_id={run}", stage="gate_state")
    entry = next((g for g in devices.get("gates", []) if g.get("direction") == "entry"), {})
    exit_gate = next((g for g in devices.get("gates", []) if g.get("direction") == "exit"), {})
    require(entry.get("entry_policy") == "deny" and exit_gate.get("physical_state") == "open",
            "gate_state", f"entry={entry.get('entry_policy')} exit={exit_gate.get('physical_state')}")
    rig.record(scenario, "gate_state", {"status": "passed", "entry_policy": "deny", "exit_physical_state": "open"})
    rig.step(run, 1, {"request_portal_attempt": "obj-car-s3-u"})
    rig.step(run, 1, {"request_portal_attempt": "obj-car-s3-w"})
    rig.step(run, 70)
    state = rig.get("operator", f"/api/v1/facilities/{FACILITY}/state?run_id={run}", stage="entry_exit")
    object_ids = {item["object_id"] for item in state["snapshot"]["objects"]}
    require("obj-car-s3-u" in object_ids and "obj-car-s3-w" not in object_ids,
            "entry_exit", "public observation did not show denied entrant retained and outbound vehicle gone")
    rig.record(scenario, "entry_exit", {"status": "passed", "sim_time_ms": state["snapshot"]["sim_time_ms"],
                                        "entrant_retained": True, "outbound_absent": True})
    followup = rig.agent(run, scenario, command_id=cid)
    command = rig.get("owner", f"/api/v1/commands/{cid}", stage="command_followup")
    plan = rig.get("owner", f"/api/v1/commands/{cid}/plan", stage="command_followup")
    require(followup.get("status") == "succeeded"
            and command.get("aggregate_status") == "succeeded"
            and plan.get("status") == "completed", "command_followup",
            f"agent={followup.get('status')} command={command.get('aggregate_status')} plan={plan.get('status')}")
    rig.record(scenario, "command_followup", {"status": "passed",
                                               "command_status": command["aggregate_status"],
                                               "plan_status": plan["status"]})


def main():
    parser = argparse.ArgumentParser(description="Run local mock Agent end-to-end HTTP story once")
    parser.add_argument("--output", type=Path, default=ROOT / "Work_tree/artifacts/local-e2e/report.json")
    parser.add_argument("--scenario", action="append", choices=("s1a", "s1b", "s1c", "s2", "s3"),
                        help="Run only the selected scene; repeat to select multiple scenes")
    args = parser.parse_args()
    selected = set(args.scenario or ("s1a", "s1b", "s1c", "s2", "s3"))
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "Work_tree/artifacts").resolve()):
        parser.error("Output must stay inside this checkout's Work_tree/artifacts directory")
    if output.exists() or (output.parent / "server.log").exists():
        parser.error("Report or server log already exists; select a new run directory")
    if not PYTHON.is_file():
        parser.error("Configured local CPython runtime is unavailable")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="parking-e2e-"))
    report = {"kind": "local-mock-http-e2e-v1", "source_commit": None,
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": {"transport": "real_loopback_http", "observations": "synthetic",
                        "selected_scenarios": sorted(selected),
                        "model": "mock_operations_adapter", "provider_calls": 0,
                        "browser": "not_run", "external_deployment": "not_run",
                        "final_acceptance": "0/194 measured by this runner"},
              "scenes": {key: {"status": "not_run", "steps": []} for key in (*[row[0] for row in FIXTURES], "s2", "s3")},
              "cleanup": {"server_stopped": False, "scratch_removed": False, "db_reopened": False}}
    try:
        report["source_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                  capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        report["source_commit"] = "unavailable"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    origin = f"http://127.0.0.1:{port}"
    child_env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "PARKING_LIVE_CONFIG",
                 "PARKING_TEAM_DEMO_ORIGIN", "PYTHONPATH"):
        child_env.pop(name, None)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    db = scratch / "e2e.sqlite3"
    process = None
    log = None
    rig = None
    try:
        log = (output.parent / "server.log").open("x", encoding="utf-8")
        process = subprocess.Popen([str(PYTHON), str(ROOT / "scripts/run_backend.py"),
                                    "--dev-controls", "--database", str(db), "--port", str(port)],
                                   cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with httpx.Client(base_url=origin, timeout=1, trust_env=False) as probe:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise FlowStop(f"server_start: process exited {process.returncode}")
                try:
                    ready = probe.get("/health/ready")
                    if ready.status_code == 200:
                        require(ready.json().get("test_control_enabled") is True
                                and ready.json().get("llm") == "not_configured",
                                "server_start", "unexpected server configuration")
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise FlowStop("server_start: readiness timeout")
        rig = Rig(origin, report)
        rig.login()
        for scenario, fixture, config, ticks in FIXTURES:
            if scenario not in selected:
                continue
            try:
                s1(rig, scenario, fixture, config, ticks)
            except (FlowStop, httpx.HTTPError, KeyError, ValueError) as exc:
                report["scenes"][scenario]["status"] = "partial"
                report["scenes"][scenario]["reason"] = str(exc)
            else:
                report["scenes"][scenario]["status"] = "passed"
        for scenario, run_scene in (("s2", s2), ("s3", s3)):
            if scenario not in selected:
                continue
            try:
                run_scene(rig)
            except (FlowStop, httpx.HTTPError, KeyError, ValueError) as exc:
                report["scenes"][scenario]["status"] = "partial"
                report["scenes"][scenario]["reason"] = str(exc)
            else:
                report["scenes"][scenario]["status"] = "passed"
    except (FlowStop, httpx.HTTPError, OSError) as exc:
        report["startup_error"] = str(exc)
    finally:
        if rig:
            rig.close()
        if process:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            report["cleanup"]["server_stopped"] = process.poll() is not None
        if log:
            log.close()
        if db.exists():
            import sqlite3
            try:
                connection = sqlite3.connect(db, timeout=.5)
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                connection.close()
                report["cleanup"]["db_reopened"] = True
            except sqlite3.Error:
                pass
        shutil.rmtree(scratch, ignore_errors=False)
        report["cleanup"]["scratch_removed"] = not scratch.exists()
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {status: sum(scene["status"] == status for scene in report["scenes"].values())
                             for status in ("passed", "partial", "not_run")}
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"output": str(output), "summary": report["summary"],
                          "cleanup": report["cleanup"]}, ensure_ascii=False))
    return 0 if report["summary"]["passed"] == len(selected) and all(report["cleanup"].values()) else 3


if __name__ == "__main__":
    sys.exit(main())
