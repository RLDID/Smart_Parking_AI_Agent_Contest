"""Resumable synthetic business repetition and deterministic watcher evaluation.

Prepare-only is the default. --run --mode live is an explicit paid opt-in using
the existing shared ledger. Mock/fake never read credentials or that ledger.
Expected results stay in the evaluator, never in model context or knowledge.
No socket server, retries, or ledger replacement. The watcher starts the real
business_loop; simulation movement is explicitly stepped by the evaluator.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "code"), str(ROOT / "scripts")]
ARTIFACT_ROOT = ROOT / "Work_tree/artifacts/model-quality"
SCENARIOS = ("s1a", "s1b", "s1c", "s2", "s3")
VERSION = "model-quality-v1"

from fastapi.testclient import TestClient
from agent.live import LiveConfiguration, LiveModels
from agent.operating_models import LiveOperationsAdapter, public_context
from backend.app import Settings, create_app
from run_local_e2e import FACILITY, FIXTURES, FlowStop, Rig, checked, require, s2, s3


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()


def source_digest():
    """Pin product/fixtures/manuals/configuration without reading Git or secrets."""
    files = list((ROOT / "code").rglob("*.py")) + list((ROOT / "data/samples").rglob("*.json"))
    files += [ROOT / "requirements.lock.txt", Path(__file__), ROOT / "scripts/run_local_e2e.py"]
    return digest({str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
                   for p in sorted(files)})


def prepare(mode="mock", providers=("openai", "gemini"), scenarios=SCENARIOS,
            repeats=3, watcher_cycles=3, config=None, source_ref="unspecified", watcher_repeats=1):
    if mode not in ("mock", "fake", "live") or not 1 <= repeats <= 100 or not 1 <= watcher_cycles <= 20:
        raise ValueError("Invalid evaluation mode or repeat bound")
    if not providers or len(set(providers)) != len(providers) or not set(providers) <= {"openai", "gemini"}:
        raise ValueError("Select distinct supported providers")
    if len(set(scenarios)) != len(scenarios) or not set(scenarios) <= set(SCENARIOS):
        raise ValueError("Select distinct supported scenarios")
    if not 1 <= watcher_repeats <= 20:
        raise ValueError("Invalid watcher repeat bound")
    tasks = [{"id": f"{p}-{scene}-{i:03}", "provider": p, "scenario": scene,
              "repeat": i, "status": "pending"}
             for i in range(1, repeats + 1) for scene in scenarios for p in providers]
    tasks += [{"id": f"{p}-watcher-{i:03}", "provider": p, "scenario": "watcher",
               "repeat": i, "status": "pending"}
              for i in range(1, watcher_repeats + 1) for p in providers]
    return {"version": VERSION, "created_at_utc": utc(), "mode": mode,
        "source_ref": source_ref, "source_digest": source_digest(),
        "config": config.model_dump(mode="json") if config is not None else None,
        "condition": {"providers": list(providers), "scenarios": list(scenarios),
            "repeats": repeats, "watcher_cycles": watcher_cycles, "watcher_repeats": watcher_repeats,
            "s1": [list(row) for row in FIXTURES], "s1_seed": 42, "initial_ticks": 60,
            "s2": {"fixture": "s2-crossing-v1", "seed": 6, "max_ticks": 45},
            "s3": {"fixture": "s3-closing-v1", "seed": 3},
            "watcher": "one enable, one run, repeated confirmed A-zone notices",
            "watcher_idle_ticks": 10, "watcher_stabilization_limit": 8,
            "watcher_inter_cycle_wall_seconds": 31},
        "scope": {"transport": "in_process_ASGI", "observations": "synthetic",
            "paid_calls": 0, "socket_server_started": False,
            "final_acceptance": "not_evaluated", "expected_results_in_model_input": False,
            "quality_claim": "per-scene repeat outcomes only; no model superiority inference",
            "watcher_claim": "real business_loop scheduling; manually stepped synthetic world, no deployed daemon reliability proof",
            "mock_fake_claim": "harness verification only; not actual LLM quality"},
        "tasks": tasks, "budget_baseline": None, "budget_after": None, "events": []}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def summarize(report):
    groups = {}
    for task in report["tasks"]:
        key = f"{task['provider']}/{task['scenario']}"
        group = groups.setdefault(key, {"planned": 0, "outcomes": Counter(), "latencies_ms": []})
        group["planned"] += 1
        group["outcomes"][task["status"]] += 1
        group["latencies_ms"].extend(call["elapsed_ms"] for call in task.get("calls", [])
                                     if call.get("elapsed_ms") is not None)
    for group in groups.values():
        group["outcomes"] = dict(group["outcomes"])
        values = group.pop("latencies_ms")
        group["model_latency_ms"] = {"n": len(values), "min": min(values) if values else None,
            "max": max(values) if values else None,
            "median": statistics.median(values) if values else None}
    complete = all(t["status"] == "passed" for t in report["tasks"])
    return {"groups": groups, "selected_matrix_passed": complete,
            "live_selected_matrix_passed": complete and report["mode"] == "live",
            "final_acceptance": "not_evaluated"}


class RoleClient:
    """Isolate the three demo sessions while sharing the in-process transport."""
    def __init__(self, client):
        self.client, self.cookie = client, None

    def request(self, method, path, *, headers=None, json=None):
        headers = dict(headers or {})
        self.client.cookies.clear()
        if self.cookie:
            headers["Cookie"] = "parking_session=" + self.cookie
        result = self.client.request(method, path, headers=headers, json=json)
        cookie = result.cookies.get("parking_session")
        if cookie:
            self.cookie = cookie
        return result

    def close(self):
        self.cookie = None


class SelectedOperationsAdapter(LiveOperationsAdapter):
    """Explicit comparison route; never simulate missing primary credentials."""
    def __init__(self, models, check_context, provider):
        self.models = models
        self.route = models.select_route(provider)
        self.adapter = models.adapter(provider, check_context)
        self.created_at = time.monotonic()
        self.model_calls = 0
        self.lock = asyncio.Lock()


class PublicFakeClient:
    """A deterministic transport double over public context, with no test answers."""
    def credentials_ready(self):
        return True

    def input_token_bound(self, model_input):
        return len(json.dumps(model_input, ensure_ascii=False).encode()) + 2048

    async def complete(self, model_input):
        context = model_input["request"]["context"]
        public_context(context)
        analysis = context.get("analysis") or {}
        goal = (context.get("command") or {}).get("normalized_goal")
        target = context.get("target_ref")
        if goal:
            action = goal["action"] if goal["action"] in ("clarify", "announce", "restrict_entry") else "hold"
        elif context.get("incident"):
            action = "recheck" if (analysis.get("clearance_sustained")
                or analysis.get("metrics", {}).get("clearance_sustained")
                or (context.get("notification") or {}).get("response")) else "hold"
        elif analysis.get("support_status") == "supported" and target:
            action = "report" if analysis.get("metrics", {}).get("status") == "risk_candidate" else "notify"
        else:
            action = "hold"
        decision = {"action": action, "target_ref": target if action == "notify" else None,
                    "reason_code": "FAKE_PUBLIC_CONTEXT", "rationale": "가상 전송 검사"}
        return SimpleNamespace(turn={"finish": {"status": "completed", "reason_code": "READ_COMPLETED",
            "answer": json.dumps(decision, ensure_ascii=False)}},
            input_tokens=40, output_tokens=20, error_code=None)


class EvaluationRig(Rig):
    def __init__(self, client, runtime, report, task, persist, stop, budget_krw):
        self.origin, self.report = "http://testserver", {"scenes": {s: {"steps": []} for s in SCENARIOS}}
        self.clients = {role: RoleClient(client) for role in ("operator", "driver", "owner")}
        self.headers, self.count = {}, 0
        self.client, self.runtime, self.session_report, self.task = client, runtime, report, task
        self.persist, self.stop, self.budget_krw = persist, stop, budget_krw
        self.business_worker = None
        self.scheduler_ticks = 0
        self.worker_task = None
        self.process_tasks = set()
        self.watcher_failure = None

    def check_stop(self):
        if self.stop and self.stop.exists():
            raise FlowStop("STOP_FILE_PRESENT")

    def budget(self):
        models = self.runtime.queries.live_models
        if models is None:
            return None
        return models.ledger.snapshot(models.configuration.limits, models.now()).model_dump()

    def install_observers(self):
        service, models = self.runtime.autonomous, self.runtime.queries.live_models
        original_process = service.process
        async def observed_process(*args, **kwargs):
            current = asyncio.current_task()
            self.process_tasks.add(current)
            started = time.monotonic()
            try:
                result = await original_process(*args, **kwargs)
                if kwargs.get("watcher"):
                    self.task["decisions"].append({"source": "watcher", "result": result,
                        "wall_elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
                    if result.get("status") not in ("confirmation_required", "accepted", "succeeded"):
                        self.watcher_failure = result.get("reason_code", "WATCHER_UNEXPECTED_RESULT")
                        service.enabled = None  # Stop this experiment at its first failed sample.
                return result
            except BaseException as error:
                if kwargs.get("watcher"):
                    self.watcher_failure = getattr(error, "code", getattr(error, "reason_code", "WATCHER_JOB_FAILED"))
                    service.enabled = None
                raise
            finally:
                self.process_tasks.discard(current)
                self.persist()
        service.process = observed_process
        original_snapshot = service._snapshot
        def snapshot(*args):
            self.check_stop()
            context = original_snapshot(*args)
            public_context(context)
            self.task["contexts"].append({"sha256": digest(context), "scenario": context["scenario"],
                "state_version": context["state_version"], "trigger": context["trigger"],
                "top_level_keys": sorted(context), "private_input_guard": "passed"})
            if models:
                current = self.budget()
                self.session_report["budget_after"] = current
                baseline = self.session_report["budget_baseline"]
                used = (current["total_spent_krw"] + current["total_pending_krw"]
                        - baseline["total_spent_krw"] - baseline["total_pending_krw"])
                settings = models.configuration.providers[self.task["provider"]]
                product_input = {"request": {"goal": "operations", "context": {
                    k: v for k, v in context.items() if k != "scenario"}},
                    "allowed_tools": [], "tool_results": []}
                bound = models.client(self.task["provider"]).input_token_bound(product_input)
                quote = int(((Decimal(bound) * Decimal(settings.pricing.input_krw_per_million)
                    + Decimal(settings.max_output_tokens) * Decimal(settings.pricing.output_krw_per_million))
                    / Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
                self.task["contexts"][-1]["next_quote_krw"] = quote
                self.persist()
                if used + quote > self.budget_krw:
                    self.task["evaluator_stop_reason"] = "EVALUATION_COST_GUARD"
                    raise FlowStop("EVALUATION_COST_GUARD")
            self.persist()
            return context
        service._snapshot = snapshot
        if models:
            original_factory = models.client_factory
            def factory(provider, *settings):
                value = original_factory(provider, *settings)
                original_complete = value.complete
                async def counted(model_input):
                    self.check_stop()
                    require(provider == self.task["provider"], "route", "UNEXPECTED_PROVIDER")
                    call = {"provider": provider, "status": "started", "started_at_utc": utc(),
                            "input_sha256": digest(model_input)}
                    self.task["calls"].append(call)
                    if self.session_report["mode"] == "live":
                        self.session_report["scope"]["paid_calls"] += 1
                    self.persist()  # Durable before the possibly billable send.
                    started = time.monotonic()
                    try:
                        reply = await original_complete(model_input)
                        call.update(status="returned", input_tokens=reply.input_tokens,
                                    output_tokens=reply.output_tokens, error_code=reply.error_code)
                        return reply
                    except BaseException as error:
                        call.update(status="failed", error_type=type(error).__name__,
                                    error_code=getattr(error, "code", None))
                        raise
                    finally:
                        call["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
                        self.persist()
                value.complete = counted
                return value
            models.client_factory = factory
            service.live_adapter_factory = lambda m, check: SelectedOperationsAdapter(m, check, self.task["provider"])

    async def pump(self):
        # Same business-loop components, explicitly clocked; no periodic watcher.
        await self.runtime.business.deliver_one()
        async with self.runtime.lock:
            self.runtime.devices.reconcile()
            self.runtime.business.process_followups()
            self.runtime.business.publish_outbox()

    def write(self, *args, **kwargs):
        if kwargs.get("stage") != "watcher_stop":
            self.check_stop()
        result = super().write(*args, **kwargs)
        if self.business_worker is None:
            self.client.portal.call(self.pump)
        return result

    def record(self, scene, stage, data):
        super().record(scene, stage, data)
        self.task["steps"].append({"scene": scene, "stage": stage, **data})
        self.persist()

    def get(self, role, path, *, stage):
        # A watcher creates its first plan asynchronously; absent is a poll state.
        return checked(self.clients[role], "GET", path, stage=stage,
                       expected=(200, 404) if stage == "watcher_plan" else (200,))

    def agent(self, run, scenario, *, command_id=None):
        mode = "mock" if self.session_report["mode"] == "mock" else "live"
        started = time.monotonic()
        result = self.write("operator", "POST", "/api/v1/test/agent/operations",
            {"run_id": run, "action": "process", "mode": mode, "scenario": scenario,
             "command_id": command_id}, stage=f"agent/{scenario}")
        self.task["decisions"].append({"wall_elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                                       "result": result})
        if mode == "live":
            require(result.get("model", {}).get("provider") == self.task["provider"], "route", "WRONG_PROVIDER")
            require(result.get("model", {}).get("usage_status") == "known", "usage", "UNSETTLED_USAGE")
        self.persist()
        return result

    def wait_for(self, read, predicate, *, stage, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            if self.watcher_failure:
                raise FlowStop(self.watcher_failure)
            if self.business_worker is None:
                self.client.portal.call(self.pump)
            result = read()
            if predicate(result):
                return result
            if self.business_worker is not None:
                time.sleep(.05)
        raise FlowStop(f"{stage}: STATE_NOT_OBSERVED")

    def state_counts(self):
        async def read():
            async with self.runtime.lock:
                db = self.runtime.store.db
                return {"jobs": db.execute("SELECT count(*) FROM autonomous_jobs").fetchone()[0],
                        "notifications": db.execute("SELECT count(*) FROM notifications").fetchone()[0],
                        "executions": db.execute("SELECT count(*) FROM executions").fetchone()[0],
                        "incident_versions": [tuple(r) for r in db.execute("SELECT incident_id,resource_version FROM incidents ORDER BY incident_id")],
                        "calls": len(self.task["calls"])}
        return self.client.portal.call(read)

    def wait_scheduler(self, ticks=1):
        self.check_stop()
        async def wait():
            target = self.scheduler_ticks + ticks
            async def ready():
                while self.scheduler_ticks < target or self.runtime.autonomous.active:
                    if self.runtime.failure or not self.runtime.autonomous.enabled:
                        raise FlowStop("WATCHER_DISABLED_OR_RUNTIME_FAILED")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 20)
        self.client.portal.call(wait)
        self.persist()

    def stable_idle(self, label):
        for _ in range(8):
            before = self.state_counts()
            self.wait_scheduler()
            if self.state_counts() == before:
                break
        else:
            raise FlowStop("WATCHER_STABILIZATION_LIMIT")
        before = self.state_counts()
        self.wait_scheduler(10)
        after = self.state_counts()
        require(before == after, label, "IDLE_DUPLICATE")
        self.task["steps"].append({"stage": label, "idle_ticks": 10, "before": before, "after": after})
        self.persist()

    def wait_cooldown(self):
        # Repeating the same broadcast is subject to the adopted 30s wall-time
        # cooldown. Simulation stepping must never be used to bypass this guard.
        seconds = 31
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            if self.watcher_failure:
                raise FlowStop(self.watcher_failure)
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        self.task["steps"].append({"stage": "broadcast_cooldown", "wall_seconds": seconds})

    def stop_worker(self):
        async def stop():
            self.runtime.autonomous.enabled = None
            if self.worker_task:
                self.worker_task.cancel()
                await asyncio.gather(self.worker_task, return_exceptions=True)
            await asyncio.sleep(0)  # Register already-created watcher jobs.
            pending = list(self.process_tasks)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 35)
        try:
            self.client.portal.call(stop)
            self.task["watcher"].update(business_loop_stopped=True, jobs_drained=True,
                                       scheduler_ticks=self.scheduler_ticks)
        finally:
            self.business_worker = None


def s1_flow(rig, scenario):
    _, fixture, config, recovery_ticks = next(row for row in FIXTURES if row[0] == scenario)
    run = rig.run(fixture, config, 42)["run_id"]
    rig.task["run_id"] = run
    rig.step(run, 60)
    result = rig.agent(run, scenario)
    require(result.get("status") == "accepted", "notification", f"{result.get('status')}/{result.get('reason_code')}")
    incident = result["incident_id"]
    notices = rig.wait_for(lambda: rig.get("driver", "/api/v1/notifications", stage="inbox"),
        lambda body: any(n["incident_id"] == incident for n in body["items"]), stage="inbox")
    nid = next(n["notification_id"] for n in notices["items"] if n["incident_id"] == incident)
    rig.write("driver", "POST", f"/api/v1/notifications/{nid}/receipts",
        {"client_request_id": f"receipt-{rig.task['id']}", "received_at": utc()}, stage="receipt")
    rig.write("driver", "POST", f"/api/v1/notifications/{nid}/responses",
        {"client_request_id": f"response-{rig.task['id']}", "response": "will_move"}, stage="response")
    before = rig.get("operator", f"/api/v1/incidents/{incident}", stage="response_only")
    require(before["status"] != "resolved", "response_only", "RESPONSE_WAS_RESOLUTION")
    rig.record(scenario, "response_only_unresolved", {"status": "passed"})
    rig.step(run, recovery_ticks, {"request_vehicle_move": "obj-car-02"})
    followup = rig.agent(run, scenario)
    current = rig.get("operator", f"/api/v1/incidents/{incident}", stage="resolution")
    require(followup.get("status") == current.get("status") == "resolved", "resolution", "NOT_RESOLVED")
    rig.record(scenario, "delivery_response_motion_resolution", {"status": "passed",
        "incident_status": current["status"], "recovery_ticks": recovery_ticks,
        "movement_source": "explicit synthetic test control"})


def watcher_flow(rig, cycles):
    run = rig.run("s3-closing-v1", "sim0-v1", 3)["run_id"]
    rig.task["run_id"] = run
    rig.step(run, 60)
    mode = "mock" if rig.session_report["mode"] == "mock" else "live"
    rig.write("operator", "POST", "/api/v1/test/agent/operations",
        {"run_id": run, "action": "start", "mode": mode}, stage="watcher_enable")
    service = rig.runtime.autonomous
    original_tick = service.tick
    async def observed_tick():
        await original_tick()
        # Yield once so spawned process tasks register before completion checks.
        await asyncio.sleep(0)
        rig.scheduler_ticks += 1
    service.tick = observed_tick
    async def business_worker():
        rig.worker_task = asyncio.current_task()
        await rig.runtime.business_loop()
    rig.business_worker = rig.client.portal.start_task_soon(business_worker)
    rig.task["watcher"] = {"business_loop_started": True, "manual_watcher_tick_calls": 0,
                           "enabled_once": True, "completed_cycles": 0}
    try:
        rig.stable_idle("idle_before_commands")
        for cycle in range(1, cycles + 1):
            if cycle > 1:
                rig.wait_cooldown()
            state = rig.get("owner", f"/api/v1/facilities/{FACILITY}/state?run_id={run}", stage="state")
            command = rig.write("owner", "POST", f"/api/v1/facilities/{FACILITY}/commands",
                {"run_id": run, "purpose": "operational_goal", "text": "A구역 쓰레기 안내해",
                 "based_on_state_version": state["applied_state_version"]}, stage="watcher_command", expected=(201,))
            cid = command["command_id"]
            plan = rig.wait_for(lambda: rig.get("owner", f"/api/v1/commands/{cid}/plan", stage="watcher_plan"),
                                lambda p: p.get("status") == "proposed", stage="watcher_proposal", seconds=20)
            require(plan["status"] == "proposed", "watcher_change", "NO_PROPOSAL")
            rig.stable_idle(f"cycle_{cycle}_awaiting_confirmation")
            rig.write("owner", "POST", f"/api/v1/commands/{cid}/confirm",
                {"expected_resource_version": plan["command_version"]}, stage="watcher_confirm")
            devices = rig.wait_for(lambda: rig.get("operator", f"/api/v1/facilities/{FACILITY}/devices?run_id={run}", stage="watcher_broadcast"),
                lambda d: any(b["simulated_playback"] == "pending" for b in d["broadcasts"]),
                stage="watcher_pending", seconds=20)
            require(any(b["simulated_playback"] == "pending" for b in devices["broadcasts"]),
                    "watcher_acceptance", "NO_PENDING_PLAYBACK")
            rig.stable_idle(f"cycle_{cycle}_accepted_not_played")
            rig.step(run, 3)
            latest = rig.wait_for(lambda: rig.get("owner", f"/api/v1/commands/{cid}", stage="watcher_followup"),
                lambda c: c.get("aggregate_status") == "succeeded", stage="watcher_completion", seconds=20)
            plan = rig.get("owner", f"/api/v1/commands/{cid}/plan", stage="watcher_followup")
            require(latest["aggregate_status"] == "succeeded" and plan["status"] == "completed",
                    "watcher_followup", "NOT_COMPLETED")
            rig.stable_idle(f"cycle_{cycle}_completed_idle")
            rig.task["steps"].append({"stage": "watcher_cycle", "cycle": cycle,
                "command_id": cid, "plan_status": plan["status"], "status": "passed"})
            rig.task["watcher"]["completed_cycles"] = cycle
            rig.persist()
    finally:
        try:
            rig.write("operator", "POST", "/api/v1/test/agent/operations",
                {"run_id": run, "action": "stop", "mode": mode}, stage="watcher_stop")
        finally:
            rig.stop_worker()
    before = rig.state_counts()
    # After stop, tick must remain inert even if called explicitly for verification.
    async def stopped_ticks():
        for _ in range(10):
            await original_tick()
    rig.client.portal.call(stopped_ticks)
    require(before == rig.state_counts(), "watcher_stop", "WORK_AFTER_STOP")
    rig.task["steps"].append({"stage": "stopped_idle", "idle_ticks": 10, "status": "passed"})


def execute_task(report, task, directory, persist, *, ledger=None, stop=None, budget_krw=1000,
                 fake_factory=None):
    attempt = directory / task["id"]
    attempt.mkdir(exist_ok=False)
    task.update(status="started", started_at_utc=utc(), calls=[], contexts=[], decisions=[], steps=[])
    persist()  # Resume never replays a started task, even if this process dies.
    config = LiveConfiguration.model_validate(report["config"]) if report["config"] else None
    app = create_app(Settings(database=attempt / "foundation.sqlite3", test_control=True,
        origins=("http://testserver",), background_ticks=False,
        live_configuration=config if report["mode"] == "live" else None,
        budget_database=ledger if report["mode"] == "live" else attempt / "unused.sqlite3"))
    started, rig = time.monotonic(), None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            runtime = app.state.runtime
            if report["mode"] == "fake":
                runtime.queries.live_models = LiveModels(config, directory / "fake-cost.sqlite3",
                    client_factory=fake_factory or (lambda *_: PublicFakeClient()))
            rig = EvaluationRig(client, runtime, report, task, persist, stop, budget_krw)
            before = rig.budget()
            if before is not None and report["budget_baseline"] is None:
                report["budget_baseline"] = before
            task["budget_before"] = before
            rig.install_observers()
            rig.login()
            try:
                if task["scenario"] in ("s1a", "s1b", "s1c"):
                    s1_flow(rig, task["scenario"])
                elif task["scenario"] == "s2":
                    s2(rig)
                elif task["scenario"] == "s3":
                    s3(rig)
                else:
                    watcher_flow(rig, report["condition"]["watcher_cycles"])
                task["status"] = "passed"
            finally:
                async def evidence():
                    db = runtime.store.db
                    return {"jobs": [{**dict(row), "result": json.loads(row["result_json"]) if row["result_json"] else None,
                                      "result_json": None} for row in db.execute(
                        "SELECT job_id,scenario,status,result_json FROM autonomous_jobs ORDER BY rowid")],
                        "counts": {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                   for table in ("incidents", "notifications", "executions", "followups")},
                        "notification_purposes": dict(db.execute("SELECT purpose,count(*) FROM notifications GROUP BY purpose").fetchall()),
                        "device_executions": [dict(row) for row in db.execute(
                            "SELECT tool_name,target_ref,status FROM executions WHERE tool_name IN ('play_announcement','set_entry_policy') ORDER BY rowid")]}
                if rig.business_worker is not None:
                    rig.stop_worker()
                task["business_evidence"] = client.portal.call(evidence)
                task["budget_after"] = report["budget_after"] = rig.budget()
                if task["budget_after"]:
                    before, after = task["budget_before"], task["budget_after"]
                    task["cost"] = {"basis": "simulated" if report["mode"] == "fake" else "buffered_estimate_not_invoice",
                        "new_known_krw": after["total_spent_krw"] - before["total_spent_krw"],
                        "new_pending_krw": after["total_pending_krw"] - before["total_pending_krw"],
                        "new_unknown_count": after["unknown_count"] - before["unknown_count"]}
                rig.close()
                if task["status"] == "passed":
                    facts = task["business_evidence"]
                    scene = task["scenario"]
                    if scene.startswith("s1"):
                        require(facts["notification_purposes"] == {"move_request": 1}, "side_effects", "DUPLICATE_OR_UNEXPECTED_CONTACT")
                    elif scene == "s2":
                        require(facts["notification_purposes"] == {"owner_report": 1}, "side_effects", "DUPLICATE_OR_UNEXPECTED_REPORT")
                    else:
                        expected = report["condition"]["watcher_cycles"] if scene == "watcher" else 3
                        require(len(facts["device_executions"]) == expected
                            and all(e["status"] == "succeeded" for e in facts["device_executions"])
                            and not facts["notification_purposes"], "side_effects", "DUPLICATE_OR_UNEXPECTED_DEVICE_OPERATION")
    except BaseException as error:
        reason = task.pop("evaluator_stop_reason", str(error) if isinstance(error, FlowStop) else "EVALUATION_EXCEPTION")
        stopped = isinstance(error, KeyboardInterrupt) or reason in ("STOP_FILE_PRESENT", "EVALUATION_COST_GUARD")
        task.update(status="stopped" if stopped else "failed", error_type=type(error).__name__, reason=reason)
    finally:
        task["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        task["finished_at_utc"] = utc()
        task["cleanup"] = {"testclient_closed": True, "socket_server_started": False,
            "active_jobs": len(app.state.runtime.autonomous.active) if hasattr(app.state, "runtime") else None,
            "watcher_enabled": bool(app.state.runtime.autonomous.enabled) if hasattr(app.state, "runtime") else None}
        persist()


def run_pending(report, directory, *, ledger=None, stop=None, budget_krw=1000, max_items=None,
                providers=None, scenarios=None, fake_factory=None):
    if report["source_digest"] != source_digest():
        raise ValueError("Source changed; start a distinct evaluation rather than mix versions")
    if report["mode"] == "live" and (ledger is None or not ledger.is_file()):
        raise ValueError("Existing shared ledger required for live execution")
    identity = str(ledger.resolve()) if ledger is not None and report["mode"] == "live" else None
    if report.get("ledger_path", identity) != identity:
        raise ValueError("Resume cannot switch the shared ledger")
    report["ledger_path"] = identity
    persist_lock = threading.RLock()
    def persist():
        with persist_lock:
            report["summary"] = summarize(report)
            for task in report["tasks"]:
                if task["status"] != "pending":
                    save_json(directory / task["id"] / "result.json", task)
            save_json(directory / "summary.json", report)
    for task in report["tasks"]:
        if task["status"] == "started":
            task.update(status="interrupted", reason="NO_AUTOMATIC_REPLAY_AFTER_INTERRUPTION")
    persist()
    count = 0
    for task in report["tasks"]:
        if task["status"] != "pending" or providers and task["provider"] not in providers or scenarios and task["scenario"] not in scenarios:
            continue
        if stop and stop.exists() or max_items is not None and count >= max_items:
            break
        execute_task(report, task, directory, persist, ledger=ledger, stop=stop,
                     budget_krw=budget_krw, fake_factory=fake_factory)
        count += 1
        if task["status"] != "passed":
            break  # Preserve failed attempts; next invocation can select other pending work.
    persist()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--mode", choices=("mock", "fake", "live"), default="mock")
    parser.add_argument("--config", type=Path, default=ROOT / "data/samples/live-read-defaults.json")
    parser.add_argument("--ledger", type=Path, default=ROOT / "data/local/model-budget.sqlite3")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--providers", nargs="+", choices=("openai", "gemini"))
    parser.add_argument("--scenarios", nargs="+", choices=(*SCENARIOS, "watcher"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--watcher-cycles", type=int, default=3)
    parser.add_argument("--watcher-repeats", type=int, default=1)
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--evaluation-budget-krw", type=int, default=1000,
                        help="Evaluator guard only; does not change the product's budget policy")
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--source-ref", default="unspecified")
    args = parser.parse_args(argv)
    directory = (args.resume or args.output or ARTIFACT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")).resolve()
    if not directory.is_relative_to(ARTIFACT_ROOT.resolve()):
        parser.error("Evaluation output/resume must stay in Work_tree/artifacts/model-quality")
    if args.evaluation_budget_krw <= 0 or args.max_items is not None and args.max_items < 1:
        parser.error("Positive evaluator budget/max-items required")
    if args.resume:
        report = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if report["version"] != VERSION or report["mode"] != args.mode:
            parser.error("Resume version/mode mismatch")
        if report["config"] != (LiveConfiguration.read(args.config).model_dump(mode="json") if args.mode != "mock" else None):
            parser.error("Resume configuration changed")
    else:
        if directory.exists():
            parser.error("New evaluation directory must not already exist; use --resume")
        config = LiveConfiguration.read(args.config) if args.mode != "mock" else None
        report = prepare(args.mode, args.providers or ("openai", "gemini"),
                         [s for s in (args.scenarios or SCENARIOS) if s != "watcher"],
                         args.repeats, args.watcher_cycles, config, args.source_ref, args.watcher_repeats)
        directory.mkdir(parents=True)
        save_json(directory / "summary.json", report)
    if args.run:
        run_pending(report, directory, ledger=args.ledger if args.mode == "live" else None,
            stop=args.stop_file or directory / "STOP", budget_krw=args.evaluation_budget_krw,
            max_items=args.max_items, providers=args.providers, scenarios=args.scenarios)
    print(json.dumps({"directory": str(directory), "mode": args.mode, "executed": args.run,
        "planned_items": len(report["tasks"]), "summary": summarize(report),
        "provider_calls": report["scope"]["paid_calls"],
        "estimated_business_calls": len(report["condition"]["providers"]) * (
            report["condition"]["repeats"] * sum(2 if s.startswith("s1") else 1 if s == "s2" else 6
                               for s in report["condition"]["scenarios"])
            + 3 * report["condition"]["watcher_cycles"] * report["condition"]["watcher_repeats"]),
        "cost_note": "Full matrix may exceed 1000 KRW; select/max-items and resume, quotes checked before each dispatch"}, ensure_ascii=False))
    return 0 if not args.run or report["summary"]["selected_matrix_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
