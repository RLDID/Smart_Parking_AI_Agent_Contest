"""Autonomous mock path uses real read/business stores and a disposable DB."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from agent.live import LiveModels
from backend.auth import ApiError, Auth
from backend.autonomous import AutonomousService
from backend.business import encoded, ident
from backend import storage
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl, AutonomousDecision
from contracts.business import ResponseInput
from simulator.world import FACILITY, advance, initial_world
from test_live_agent import configuration


@pytest.fixture
def rig(tmp_path):
    runtime = Runtime(tmp_path / "agent.sqlite3")
    auth = Auth(runtime.store)
    token, session = auth.login("demo-operator", "parking-demo-only", "test")
    runtime.world = initial_world(1)
    for _ in range(60):
        advance(runtime.world)
    runtime.store.commit(runtime.world, Runtime.event(runtime.world))
    assert runtime.store.db.execute("PRAGMA user_version").fetchone()[0] == 6
    service = AutonomousService(runtime)
    try:
        yield runtime, service, session, lambda: auth.require(token)
    finally:
        runtime.store.close()


def test_mock_s1a_observes_searches_plans_and_accepts_without_moving(rig):
    runtime, service, session, authenticate = rig
    run = runtime.world["run_id"]
    before = runtime.world["observation"]
    body = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s1a")
    result = asyncio.run(service.control(session, body, "agent-s1a", authenticate))
    assert result["status"] == "accepted"
    assert result["mode"] == "mock"
    assert result["model"]["model_call_count"] == 0
    assert result["incident_id"] and result["plan_id"]
    assert result["execution"]["status"] == "accepted"
    assert runtime.world["observation"] == before
    assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1
    assert runtime.store.db.execute("SELECT count(*) FROM followups").fetchone()[0] == 1
    again = asyncio.run(service.control(session, body, "agent-s1a", authenticate))
    assert again["job_id"] == result["job_id"]
    assert runtime.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_operational_provider_wait_allows_runtime_tick_and_independent_alarm(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path / "waiting-safety.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            runtime.world = initial_world(6, "s2-crossing-v1")
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            run = runtime.world["run_id"]
            await runtime.mutate(operator, "start-s2", "control", {"action": "start"}, run)
            entered, release = asyncio.Event(), asyncio.Event()

            class WaitingClient:
                calls = 0
                def credentials_ready(self):
                    return True
                def input_token_bound(self, _input):
                    return 2048
                async def complete(self, _input):
                    self.calls += 1
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(turn={"finish": {"status": "completed", "answer": json.dumps({
                        "action": "report", "target_ref": "obj-car-02", "reason_code": "RISK",
                        "rationale": "현재 공개 관측 검토"})}}, input_tokens=40, output_tokens=10, error_code=None)

            client = WaitingClient()
            runtime.queries.live_models = LiveModels(
                configuration(), tmp_path / "waiting-cost.sqlite3",
                client_factory=lambda *_: client)
            service = runtime.autonomous
            job = asyncio.create_task(service.control(operator, AutonomousControl(
                run_id=run, action="process", mode="live", scenario="s2"),
                "waiting-s2", lambda: auth.require(token)))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                before = runtime.world["sim_time_ms"]
                for _ in range(45):
                    await runtime.tick()
                    alarm = runtime.public_devices(runtime.world)["alarms"][0]
                    if (runtime.world.get("safety_state", {}).get("claims")
                            and alarm["desired_active"] and alarm["visual"] == alarm["audio"] == "on"):
                        break
                assert runtime.world["sim_time_ms"] > before
                assert runtime.world.get("safety_state", {}).get("claims")
                assert alarm["claim_count"] >= 1 and alarm["desired_active"]
                assert alarm["visual"] == alarm["audio"] == "on"
                assert runtime.world["s2_contact_at_ms"] is None
                assert not job.done() and client.calls == 1
            finally:
                release.set()
                await asyncio.gather(job, return_exceptions=True)
            result = await job
            assert result["status"] == "held"
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 0
        finally:
            runtime.store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("change,expected_code", (
    ("logout", "UNAUTHENTICATED"),
    ("document", "JOB_CONTEXT_CHANGED"),
    ("relationship", "RECIPIENT_UNVERIFIED"),
    ("run", "RUN_NOT_FOUND"),
))
def test_live_work_rechecks_changed_authority_or_evidence_after_model_reply(tmp_path, change, expected_code):
    async def exercise():
        runtime = Runtime(tmp_path / "changed-work.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            runtime.world = initial_world(1)
            for _ in range(60):
                advance(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            run = runtime.world["run_id"]
            entered, release = asyncio.Event(), asyncio.Event()

            class WaitingClient:
                def __init__(self):
                    self.calls = 0
                def credentials_ready(self):
                    return True
                def input_token_bound(self, _input):
                    return 2048
                async def complete(self, _input):
                    self.calls += 1
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(turn={"finish": {"status": "completed", "answer": json.dumps({
                        "action": "notify", "target_ref": "obj-car-02", "reason_code": "BLOCKED",
                        "rationale": "Current observed blockage"})}},
                        input_tokens=40, output_tokens=10, error_code=None)

            clients = {name: WaitingClient() for name in ("openai", "gemini")}
            models = LiveModels(configuration(), tmp_path / "changed-cost.sqlite3",
                                client_factory=lambda provider, *_: clients[provider])
            runtime.queries.live_models = models
            job = asyncio.create_task(runtime.autonomous.control(operator, AutonomousControl(
                run_id=run, action="process", mode="live", scenario="s1a"),
                "changed-context", lambda: auth.require(token)))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                if change == "logout":
                    auth.sessions.pop(token)
                elif change == "document":
                    runtime.knowledge.document_access(FACILITY, "manual-parking-order", "v1", status="withdrawn")
                elif change == "relationship":
                    runtime.store.db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'")
                    runtime.store.db.commit()
                else:
                    await runtime.mutate(operator, "new-run", "control", {"action": "reset"}, run)
            finally:
                release.set()
                await asyncio.gather(job, return_exceptions=True)
            if change == "relationship":
                result = await job
                assert result["status"] == "held" and result["reason_code"] == expected_code
                assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 0
                assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 1
            else:
                with pytest.raises(ApiError) as error:
                    await job
                assert error.value.code == expected_code
            assert clients["openai"].calls == 1 and clients["gemini"].calls == 0
            assert models.ledger.snapshot(models.configuration.limits, models.now()).unknown_count == 0
            if change != "relationship":
                for table in ("incidents", "plans", "notifications", "executions"):
                    assert runtime.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        finally:
            runtime.store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ("moved", "resolved", "irrelevant"))
def test_waiting_work_rechecks_current_s1_state(rig, tmp_path, monkeypatch, record_property, change):
    """T12 supplemental conditions, not the frozen 194-variant final evaluation."""
    runtime, _, operator, authenticate = rig
    service = runtime.autonomous
    run = runtime.world["run_id"]
    body = AutonomousControl(run_id=run, action="process", mode="live", scenario="s1a")
    trace, channel_calls = [], []
    original_analysis = runtime.business.analysis
    original_send = runtime.business.channel.send

    def counts():
        values = {table: runtime.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("incidents", "plans", "notifications", "executions",
                                "followups", "delivery_attempts")}
        return values | {"channel_calls": len(channel_calls),
                         "device_operations": len(runtime.world["device_state"]["operations"])}

    def record(stage, analysis=None, **extra):
        analysis = original_analysis() if analysis is None else analysis
        target = next((obj for obj in runtime.world["observation"]["objects"] if obj["object_id"] == "obj-car-02"), None)
        trace.append({"stage": stage, "state_version": runtime.world["state_version"],
                      "sim_time_ms": runtime.world["sim_time_ms"],
                      "target_present": target is not None,
                      "target_position": deepcopy(target["position"]) if target else None,
                      "observation_ids": analysis.observation_ids,
                      "violation_candidate": any(o.object_id == "obj-car-02" and o.stationary_candidate
                                                 for o in analysis.metrics.objects),
                      "clearance_sustained": analysis.metrics.clearance_sustained,
                      "counts": counts(), **extra})

    async def observe_send(notification_id, message):
        channel_calls.append(notification_id)
        return await original_send(notification_id, message)

    monkeypatch.setattr(runtime.business.channel, "send", observe_send)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        job = None
        try:
            if change == "irrelevant":
                # Starting the scheduler enables wall-age freshness checks. Keep
                # this condition at a valid fixed wall time, not runner speed.
                observed_at = datetime.fromisoformat(runtime.world["observation"]["received_at"].replace("Z", "+00:00"))

                class ObservationClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return observed_at.astimezone(tz) if tz else observed_at.replace(tzinfo=None)

                monkeypatch.setattr("simulator.spatial.datetime", ObservationClock)
            initial_incident = None
            if change == "resolved":
                first = await service.control(operator, body.model_copy(update={"mode": "mock"}),
                                              "before-wait", authenticate)
                assert first["status"] == "accepted"
                initial_incident = first["incident_id"]
                assert await runtime.business.deliver_one()
                notice = runtime.store.db.execute(
                    "SELECT notification_id,recipient_user_id FROM notifications WHERE purpose='move_request'").fetchone()
                driver = Auth(runtime.store).login(notice["recipient_user_id"], "parking-demo-only", "test")[1]
                runtime.business.reply(driver, notice["notification_id"], "response",
                    ResponseInput(client_request_id="cannot-before-wait", response="cannot_move"),
                    "cannot-before-wait")

            initial = runtime.business.impact_assessment("aisle_obstruction", "obj-car-02", "aisle-west")
            assert initial["support_status"] == "supported" and initial["violation_candidate"]
            assert not initial["clearance_sustained"]
            record("initial")

            class WaitingClient:
                def __init__(self):
                    self.calls = 0
                    self.context = None

                def credentials_ready(self):
                    return True

                def input_token_bound(self, _input):
                    return 2048

                async def complete(self, model_input):
                    self.calls += 1
                    self.context = deepcopy(model_input["request"]["context"])
                    record("model_wait")
                    entered.set()
                    await release.wait()
                    record("model_reply")
                    # The decision reflects the pre-wait blockage/driver response.
                    return SimpleNamespace(turn={"finish": {"status": "completed", "answer": json.dumps({
                        "action": "report" if change == "resolved" else "notify",
                        "target_ref": "obj-car-02", "reason_code": "BLOCKED",
                        "rationale": "Current observed blockage"})}},
                        input_tokens=40, output_tokens=10, error_code=None)

            clients = {name: WaitingClient() for name in ("openai", "gemini")}
            models = LiveModels(configuration(), tmp_path / "wait-state-cost.sqlite3",
                                client_factory=lambda provider, *_: clients[provider])
            runtime.queries.live_models = models
            job = asyncio.create_task(service.control(operator, body, "waiting-state", authenticate))
            await asyncio.wait_for(entered.wait(), 5)
            context = clients["openai"].context
            assert context["knowledge"]["status"] == "matched"
            assert context["target_ref"] == "obj-car-02"
            stamp = service._stamp(operator, run, authenticate)
            before = deepcopy(runtime.world)
            before_counts = counts()
            original_device_operations = deepcopy(runtime.world["device_state"]["operations"])

            if change == "irrelevant":
                # Only the scheduler flag changes paused -> running (and its version).
                # No loop/tick is started: geometry, evidence time and authority stay valid.
                await runtime.mutate(operator, "start-during-wait", "control", {"action": "start"}, run)
                changed_fields = {key for key in before if before[key] != runtime.world[key]}
                assert changed_fields == {"run_status", "state_version"}
                assert runtime.world["run_status"] == "running"
                current = runtime.business.impact_assessment("aisle_obstruction", "obj-car-02", "aisle-west")
                assert current | {"state_version": initial["state_version"]} == initial
            else:
                await runtime.mutate(operator, "move-during-wait", "control",
                                     {"action": "step", "action_params": {"request_vehicle_move": "obj-car-02"}}, run)
                # Use the observation/runtime path until movement invalidates the old
                # stationary candidate; movement alone must not prove sustained recovery.
                for tick in range(20):
                    current = runtime.business.impact_assessment("aisle_obstruction", "obj-car-02", "aisle-west")
                    if not current["violation_candidate"]:
                        break
                    await runtime.mutate(operator, f"move-step-{tick}", "control", {"action": "step"}, run)
                assert current["support_status"] == "supported" and not current["violation_candidate"]
                assert not current["clearance_sustained"]
                record("moved_without_sustained_clearance")
                assert trace[-1]["target_present"]
                assert trace[-1]["target_position"] != trace[0]["target_position"]
                if change == "resolved":
                    # Reuse the fresh sustained-recovery path, with another authorized
                    # requester because the original requester still has a pending job.
                    for tick in range(100):
                        await runtime.mutate(operator, f"recovery-step-{tick}", "control", {"action": "step"}, run)
                    current = runtime.business.impact_assessment("aisle_obstruction", "obj-car-02", "aisle-west")
                    assert current["support_status"] == "supported" and current["clearance_sustained"]
                    assert not current["violation_candidate"]
                    auth = Auth(runtime.store)
                    owner_token, owner = auth.login("demo-owner", "parking-demo-only", "test")
                    resolved = await service.control(owner, body.model_copy(update={"mode": "mock"}),
                                                     "fresh-recovery", lambda: auth.require(owner_token))
                    assert resolved["status"] == "resolved" and resolved["incident_id"] == initial_incident
                    assert runtime.store.db.execute("SELECT status FROM plans").fetchone()[0] == "completed"
                    record("resolved_by_fresh_recovery")

            assert not job.done() and clients["openai"].calls == 1
            assert runtime.world["state_version"] > context["state_version"]
            assert service._stamp(operator, run, authenticate) == stamp
            at_reply = counts()
            incident_at_reply = [dict(row) for row in runtime.store.db.execute("SELECT * FROM incidents")]
            record("change_complete")

            def observe_recheck():
                analysis = original_analysis()
                record("current_recheck", analysis)
                return analysis

            monkeypatch.setattr(runtime.business, "analysis", observe_recheck)
            release.set()
            result = await asyncio.wait_for(asyncio.shield(job), 5)
            monkeypatch.setattr(runtime.business, "analysis", original_analysis)
            record("completed", status=result["status"], reason_code=result.get("reason_code"))
            stages = [item["stage"] for item in trace]
            assert stages.index("model_wait") < stages.index("change_complete") < stages.index("model_reply")
            assert stages.index("model_reply") < stages.index("current_recheck") < stages.index("completed")
            assert all(item["state_version"] == runtime.world["state_version"]
                       for item in trace if item["stage"] == "current_recheck")
            if change == "irrelevant":
                assert result["status"] == result["execution"]["status"] == "accepted"
                assert before_counts["notifications"] == 0
                assert await runtime.business.deliver_one()
                assert counts()["incidents"] == counts()["plans"] == counts()["notifications"] == 1
                assert counts()["channel_calls"] == counts()["delivery_attempts"] == 1
            else:
                assert result["status"] == "held"
                assert result["reason_code"] == ("INCIDENT_CHANGED" if change == "resolved" else "OBSERVATION_CHANGED")
                assert counts() == at_reply
                assert [dict(row) for row in runtime.store.db.execute("SELECT * FROM incidents")] == incident_at_reply
                assert not await runtime.business.deliver_one()
                if change == "resolved":
                    assert incident_at_reply[0]["status"] == "resolved"
                    assert at_reply["notifications"] == at_reply["plans"] == at_reply["channel_calls"] == 1
                else:
                    assert all(value == 0 for name, value in at_reply.items() if name != "device_operations")
            assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 0
            # Fixture startup has device feedback records; no new device operation
            # or replacement of those records may result from these S1 decisions.
            assert runtime.world["device_state"]["operations"] == original_device_operations
            terminal_counts = counts()
            replay = await service.control(operator, body, "waiting-state", authenticate)
            assert replay == result and counts() == terminal_counts
            assert clients["openai"].calls == 1 and clients["gemini"].calls == 0
            assert models.ledger.snapshot(models.configuration.limits, models.now()).unknown_count == 0
            assert job.done() and not service.active and not runtime.queries.active
            record("replay_without_new_effects", fake_model_calls=1, real_provider_calls=0)
        finally:
            release.set()
            if job is not None:
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            await service.close()
            record("resources_released", job_done=job is None or job.done(),
                   active_jobs=len(service.active), query_jobs=len(runtime.queries.active))
            record_property("wait_state_trace", json.dumps(trace))
    asyncio.run(exercise())


def test_invalid_decision_cannot_write_and_marks_job_held(rig):
    runtime, service, session, authenticate = rig
    class InvalidAdapter:
        async def decide(self, context):
            return {"scenario": "s1a", "action": "open_gate", "target_ref": "obj-car-02",
                    "reason_code": "TRY", "rationale": "invalid"}
        def result_metadata(self):
            return {"mode": "mock", "usage_status": "not_sent"}
    service.live_adapter_factory = lambda _models, _check: InvalidAdapter()
    runtime.queries.live_models = object()
    body = AutonomousControl(run_id=runtime.world["run_id"], action="process", mode="live", scenario="s1a")
    # The injected adapter must expose a fixed route before any dispatch.
    InvalidAdapter.route = {"provider": "test", "model_ref": "fake", "routing_mode": "test"}
    with pytest.raises(ApiError) as error:
        asyncio.run(service.control(session, body, "bad-decision", authenticate))
    assert error.value.code == "INVALID_DECISION"
    assert runtime.store.db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
    assert runtime.store.db.execute("SELECT status FROM autonomous_jobs").fetchone()[0] == "held"


def test_decision_contract_rejects_cross_scenario_actuation():
    with pytest.raises(ValueError):
        AutonomousDecision(scenario="s2", action="restrict_entry", reason_code="X", rationale="bad")


def test_real_v5_database_preserves_incident_impact_in_v6(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "migrate_autonomous", lambda _db: None)
        runtime = Runtime(path)
        runtime.world = initial_world(1)
        runtime.store.commit(runtime.world, Runtime.event(runtime.world))
        run = runtime.world["run_id"]
        incident_id = ident("incident")
        policy = runtime.knowledge.current_policy(FACILITY)
        runtime.business.insert("incidents", incident_id=incident_id, facility_id=FACILITY,
            run_id=run, status="active", primary_object_id="obj-car-02", dedup_key="migration-test",
            policy_version=policy.policy_version, reason_summary="preserved")
        runtime.business.insert("incident_impacts", impact_id=ident("impact"), facility_id=FACILITY,
            run_id=run, incident_id=incident_id, type="aisle_obstruction", object_id="obj-car-02",
            zone_id="aisle-west", condition_json=encoded({}))
        runtime.store.db.commit()
        assert runtime.store.db.execute("PRAGMA user_version").fetchone()[0] == 5
        runtime.store.close()
    reopened = storage.Store(path)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert reopened.db.execute("SELECT type FROM incident_impacts WHERE incident_id=?", (incident_id,)).fetchone()[0] == "aisle_obstruction"
        reopened.db.execute("INSERT INTO incident_impacts(impact_id,facility_id,run_id,incident_id,type,object_id,zone_id,condition_json) VALUES (?,?,?,?,?,?,?,?)",
            (ident("impact"), FACILITY, run, incident_id, "exit_blocked", "obj-car-02", "B01", "{}"))
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        reopened.close()


def test_process_key_rejects_changed_body(rig):
    runtime, service, session, authenticate = rig
    run = runtime.world["run_id"]
    first = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s1a")
    asyncio.run(service.control(session, first, "same-job-key", authenticate))
    changed = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s1b")
    with pytest.raises(ApiError) as error:
        asyncio.run(service.control(session, changed, "same-job-key", authenticate))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert runtime.store.db.execute("SELECT count(*) FROM autonomous_jobs").fetchone()[0] == 1


def test_adapter_factory_failure_is_held_without_active_leak(rig):
    runtime, service, session, authenticate = rig
    runtime.queries.live_models = object()
    def fail_factory(_models, _check):
        raise RuntimeError("route unavailable")
    service.live_adapter_factory = fail_factory
    body = AutonomousControl(run_id=runtime.world["run_id"], action="process", mode="live", scenario="s1a")
    with pytest.raises(RuntimeError):
        asyncio.run(service.control(session, body, "factory-fails", authenticate))
    row = runtime.store.db.execute("SELECT status,result_json FROM autonomous_jobs").fetchone()
    assert row["status"] == "held" and json.loads(row["result_json"])["model"]["usage_status"] == "not_sent"
    assert not service.active


def test_live_route_is_saved_before_provider_dispatch(rig):
    runtime, service, session, authenticate = rig
    runtime.queries.live_models = object()
    class RouteProbe:
        route = {"provider": "test", "model_ref": "test-model", "routing_mode": "test"}
        async def decide(self, _context):
            row = runtime.store.db.execute("SELECT status,result_json FROM autonomous_jobs").fetchone()
            assert row["status"] == "pending"
            assert json.loads(row["result_json"])["route"]["model_ref"] == "test-model"
            raise RuntimeError("provider did not dispatch")
        def result_metadata(self):
            return {"usage_status": "not_sent", "model_call_count": 0}
    service.live_adapter_factory = lambda _models, _check: RouteProbe()
    body = AutonomousControl(run_id=runtime.world["run_id"], action="process", mode="live", scenario="s1a")
    with pytest.raises(RuntimeError):
        asyncio.run(service.control(session, body, "route-first", authenticate))
    assert runtime.store.db.execute("SELECT status FROM autonomous_jobs").fetchone()[0] == "held"


def test_replay_rejects_new_autonomous_work(rig):
    runtime, service, session, authenticate = rig
    runtime.world["replay_state"] = {"cursor": 0}
    body = AutonomousControl(run_id=runtime.world["run_id"], action="process", mode="mock", scenario="s1a")
    with pytest.raises(ApiError) as error:
        asyncio.run(service.control(session, body, "replay-reject", authenticate))
    assert error.value.code == "REPLAY_MODE"
    assert runtime.store.db.execute("SELECT count(*) FROM autonomous_jobs").fetchone()[0] == 0


def test_watcher_coalesces_static_blockage_and_reports_cannot_move(rig):
    runtime, service, session, authenticate = rig
    run = runtime.world["run_id"]
    async def exercise():
        await service.control(session, AutonomousControl(run_id=run, action="start", mode="mock"),
                              "watch-on", authenticate)
        await service.tick()
        await asyncio.sleep(0.05)
        assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1
        for _ in range(5):
            await service.tick()
            await asyncio.sleep(0.03)
        incident = runtime.store.db.execute("SELECT resource_version FROM incidents").fetchone()[0]
        for _ in range(5):
            await service.tick()
            await asyncio.sleep(0.03)
        assert runtime.store.db.execute("SELECT resource_version FROM incidents").fetchone()[0] == incident
        assert runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1
        await runtime.business.deliver_one()
        notice = runtime.store.db.execute("SELECT notification_id,recipient_user_id FROM notifications WHERE purpose='move_request'").fetchone()
        driver = Auth(runtime.store).login(notice["recipient_user_id"], "parking-demo-only", "test")[1]
        nid = notice["notification_id"]
        async with runtime.lock:
            runtime.business.reply(driver, nid, "response", ResponseInput(
                client_request_id="driver-cannot", response="cannot_move"), "driver-cannot")
        active_impact = runtime.store.db.execute("SELECT type FROM incident_impacts").fetchone()[0]
        active_scenario = {"aisle_obstruction": "s1a", "exit_blocked": "s1b", "bay_intrusion": "s1c"}[active_impact]
        probe = service._snapshot(session, AutonomousControl(run_id=run, action="process", mode="mock", scenario=active_scenario),
                                  runtime.read_task(session, run))
        assert probe["notification"]["response"] == "cannot_move"
        reports = 0
        for _ in range(10):
            await service.tick()
            await asyncio.sleep(0.05)
            reports = runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0]
            if reports:
                break
        assert reports == 1, [(row["status"], json.loads(row["result_json"]).get("reason_code"),
                               json.loads(row["result_json"]).get("decision", {}).get("action"))
                              for row in runtime.store.db.execute("SELECT status,result_json FROM autonomous_jobs ORDER BY rowid")]
        assert runtime.store.db.execute("SELECT status FROM incidents").fetchone()[0] != "resolved"
        await service.control(session, AutonomousControl(run_id=run, action="stop"), "watch-off", authenticate)
    asyncio.run(exercise())
