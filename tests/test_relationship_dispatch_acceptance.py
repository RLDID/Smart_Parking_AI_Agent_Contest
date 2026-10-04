"""Local fake-channel checks of recipient absence and dispatch boundaries.

The event barriers observe the real prepare/send/finish operations. No sleeps,
external channel, background server, or replacement delivery implementation.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.auth import ApiError, Auth
from backend.business import MockChannel, WebInbox
from backend.relationships import Relationships
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from contracts.business import ReceiptInput, ResponseInput
from contracts.relationships import VehicleChange, VehicleUserChange
from simulator.world import FACILITY, advance, initial_world, observe, utc_now
from test_business import notice, rig


@pytest.mark.parametrize("report_case", ("ready", "owner_disabled", "owner_ambiguous", "forbidden", "unknown", "owner_added"))
def test_initially_unregistered_target_holds_real_autonomous_work(tmp_path, report_case):
    """Synthetic initial identity, not a previously registered recipient's unlink.

    The public run API seeds fixed registered object IDs. This local preparation
    gives the blocker a new public ID before its first stored observation; no
    mapping or vehicle-user relation is deleted, revoked or made uncertain.
    It covers the fail-closed subset of T07, not frozen-final equivalence or
    movement of this renamed actor. Owner reports do not resolve the incident.
    """
    async def scenario():
        runtime = Runtime(tmp_path / "unregistered.sqlite3")
        try:
            db, business = runtime.store.db, runtime.business
            auth = Auth(runtime.store)
            token, session = auth.login("demo-operator", "parking-demo-only", "test")
            authenticate = lambda: auth.require(token)
            target = "obj-car-unregistered"
            world = initial_world(1)
            blocker = next(a for a in world["actors"] if a["object_id"] == "obj-car-02")
            blocker["object_id"] = target
            # initial_world already observed once. Rebuild that unpersisted
            # frame so the entire history describes the new public target.
            world.pop("observation")
            world["observation_history"] = []
            world["observation_queue"] = []
            observe(world)
            for _ in range(60):
                advance(world)
            runtime.world = world
            runtime.store.commit(world, Runtime.event(world))
            run = world["run_id"]
            for frame in world["observation_history"]:
                ids = {obj["object_id"] for obj in frame["objects"]}
                assert target in ids and "obj-car-02" not in ids
            assert db.execute("SELECT count(*) FROM object_mappings WHERE object_id=?",
                              (target,)).fetchone()[0] == 0
            # Valid registered recipients remain available as distractors;
            # the service must not choose one by position or a default ID.
            assert business.resolve_recipient("obj-car-01")["user_id"] == "demo-driver-2"
            registry_before = {table: [tuple(row) for row in db.execute(
                f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("object_mappings", "vehicles", "vehicle_users")}
            assessment = business.impact_assessment("aisle_obstruction", target, "aisle-west")
            assert assessment["support_status"] == "supported"
            assert assessment["violation_candidate"] and assessment["occupied"]
            assert assessment["observation_ids"] and not assessment["clearance_sustained"]
            runtime.ensure_autonomous_policy()
            search = await runtime.read_tool(session, "search_operating_knowledge", {
                "facility_id": FACILITY, "run_id": run, "query": "통로 차단 이동 요청과 미응답",
                "topic": "parking_order", "zone_id": "aisle-west"}, runtime.read_task(session, run))
            assert search["status"] == "matched" and search["references"]
            retrievals_before = db.execute("SELECT count(*) FROM knowledge_retrievals").fetchone()[0]
            assert db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
            if report_case == "owner_disabled":
                db.execute("UPDATE users SET disabled_at=? WHERE user_id='demo-owner'", (utc_now(),))
            elif report_case == "owner_ambiguous":
                db.execute("UPDATE memberships SET role='owner' WHERE user_id='demo-driver-2'")
            elif report_case == "forbidden":
                policy = runtime.knowledge.current_policy(FACILITY)
                payload = policy.model_dump()
                payload["execution_rules"]["allowed_tools"].remove("report_to_owner")
                # Preserve immutable policy content and install a new window
                # in this private fixture instead of bypassing its DB guard.
                payload["policy_version"] += 1
                payload["effective_at"] = utc_now()
                db.execute("UPDATE policies SET retired_at=? WHERE facility_id=? AND policy_version=?",
                           (payload["effective_at"], FACILITY, policy.policy_version))
                db.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)",
                           (FACILITY, payload["policy_version"], policy.knowledge_release_id,
                            payload["effective_at"], None, json.dumps(payload)))
                assert runtime.knowledge.current_policy(FACILITY).policy_version == payload["policy_version"]
            db.commit()
            channel = MockChannel(["unknown"] if report_case == "unknown" else None)
            business.channel = channel
            world_before = deepcopy(world)
            body = AutonomousControl(run_id=run, action="process", mode="mock", scenario="s1a")
            result = await runtime.autonomous.control(session, body, "initial-unregistered", authenticate)
            assert (result["status"], result["reason_code"]) == ("held", "RECIPIENT_UNVERIFIED")
            assert result["decision"]["action"] == "notify"
            assert result["decision"]["target_ref"] == target
            assert result["model"]["model_call_count"] == 0
            job = db.execute("SELECT status,result_json FROM autonomous_jobs WHERE job_id=?",
                             (result["job_id"],)).fetchone()
            assert job["status"] == "held" and json.loads(job["result_json"]) == result
            incident = db.execute("SELECT incident_id,primary_object_id,status FROM incidents").fetchall()
            assert [tuple(row) for row in incident] == [(result["incident_id"], target, "needs_review")]
            # The actual control call performs and persists its own retrieval.
            searches = [json.loads(row[0]) for row in db.execute(
                "SELECT result_json FROM knowledge_retrievals ORDER BY rowid")]
            assert len(searches) == retrievals_before + 1
            assert searches[-1]["status"] == "matched" and searches[-1]["references"]

            notices = [dict(row) for row in db.execute(
                "SELECT notification_id,purpose,recipient_user_id FROM notifications")]
            expected_reports = int(report_case in ("ready", "unknown", "owner_added"))
            assert len(notices) == expected_reports
            assert all(n["purpose"] == "owner_report" and n["recipient_user_id"] == "demo-owner"
                       for n in notices)
            if expected_reports:
                assert result["report"]["status"] == "accepted"
                assert result["report"]["result"]["delivery_status"] == "queued"
            else:
                assert "report" not in result
                assert result["report_reason_code"] == (
                    "TOOL_NOT_ALLOWED" if report_case == "forbidden" else "RECIPIENT_UNVERIFIED")
            if report_case == "owner_added":
                db.execute("UPDATE memberships SET role='owner' WHERE user_id='demo-driver-2'")
                db.commit()
            while await business.deliver_one():
                assert len(channel.calls) <= len(notices)
            assert set(channel.calls) <= {n["notification_id"] for n in notices}
            if report_case == "owner_added":
                assert channel.calls == []
                report_row = db.execute("SELECT status,error_code FROM executions WHERE tool_name='report_to_owner'").fetchone()
                assert tuple(report_row) == ("held", "RECIPIENT_CHANGED")
            assert db.execute("""SELECT count(*) FROM delivery_attempts a JOIN notifications n
                USING(notification_id) WHERE n.purpose!='owner_report'""").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM executions WHERE tool_name='notify_vehicle_user'").fetchone()[0] == 0
            assert runtime.world == world_before
            assert registry_before == {table: [tuple(row) for row in db.execute(
                f"SELECT * FROM {table} ORDER BY rowid")] for table in registry_before}
            changes_before_retry = db.total_changes
            calls_before_retry = list(channel.calls)
            repeated = await runtime.autonomous.control(session, body, "initial-unregistered", authenticate)
            assert repeated == result
            assert db.total_changes == changes_before_retry
            assert await business.deliver_one() is False
            assert channel.calls == calls_before_retry
            assert runtime.world == world_before
            # A distinct job key follows the normal mock recheck path. It
            # preserves review status and reuses an accepted/unknown report.
            next_result = await runtime.autonomous.control(session, body, "unregistered-new-job", authenticate)
            assert next_result["status"] == "held"
            assert db.execute("SELECT status FROM incidents WHERE incident_id=?",
                              (result["incident_id"],)).fetchone()[0] == "needs_review"
            assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == expected_reports
            assert await business.deliver_one() is False
            assert channel.calls == calls_before_retry
            if report_case == "unknown":
                assert next_result["report"]["status"] == "unknown"
            assert not runtime.autonomous.active
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("incidents", "autonomous_jobs", "plans", "executions",
                                    "notifications", "delivery_attempts", "followups")}
            evidence = {"condition": "synthetic initially unmapped public target; no unlink",
                        "seed": 1, "ticks": 60, "sim_time_ms": world["sim_time_ms"],
                        "target": target, "assessment": assessment, "searches": searches,
                        "result": result, "incident": dict(incident[0]), "counts": counts,
                        "notifications": notices, "channel_calls": channel.calls,
                        "registry_unchanged": True, "retry_db_changes": db.total_changes - changes_before_retry,
                        "owner_report_count": sum(n["purpose"] == "owner_report" for n in notices),
                        "report_case": report_case,
                        "limits": "Owner report is not resolution; frozen-final equivalence not established"}
            (tmp_path / "unregistered-trace.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            await runtime.autonomous.close()
            runtime.store.close()

    asyncio.run(scenario())


class ObservedInbox(WebInbox):
    """In-process fake retaining local inbox projection semantics only."""

    def __init__(self, trace, entered, release):
        self.trace, self.entered, self.release = trace, entered, release
        self.calls = []

    async def send(self, notification_id, message):
        self.trace.append("send_entry")
        self.calls.append(notification_id)
        self.entered.set()
        await self.release.wait()
        return "accepted"


async def commit_relationship(rig, change, trace):
    # Same service transaction and runtime lock used by relationship_routes.
    async with rig.runtime.lock:
        rel = Relationships(rig.db)
        if change == "vehicle_disabled":
            body = VehicleChange(expected_version=0, active=False, reason="dispatch boundary")
            operation = lambda: rel.vehicle_change("demo-operator", "veh-demo-02", body)
            action = "vehicle.change:veh-demo-02"
        else:
            body = VehicleUserChange(expected_version=0,
                user_id="demo-driver-2" if change == "transfer" else None,
                reason="dispatch boundary")
            operation = lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body)
            action = "vehicle.user:veh-demo-02"
        result = rel.execute("demo-operator", "dispatch-relationship", action,
                             body.model_dump(), operation)
        assert result["resource_version"] == 1
        assert not rig.db.in_transaction
        trace.append("relationship_commit")


def assert_old_message_inaccessible(rig, notification_id):
    business = rig.runtime.business
    for username in ("demo-driver", "demo-driver-2"):
        assert business.notifications(username)["items"] == []
        for action, body in (
            ("receipt", ReceiptInput(client_request_id="late-receipt", received_at=utc_now())),
            ("response", ResponseInput(client_request_id="late-response", response="will_move")),
        ):
            with pytest.raises(ApiError) as error:
                business.reply(rig.sessions[username], notification_id, action, body,
                               username + "-" + action)
            assert (error.value.status, error.value.code) == (404, "NOT_FOUND")
    assert rig.db.execute("SELECT count(*) FROM notification_receipts").fetchone()[0] == 0
    assert rig.db.execute("SELECT count(*) FROM notification_responses").fetchone()[0] == 0


@pytest.mark.parametrize("boundary,change", [
    ("before_prepare", "unlink"), ("before_prepare", "vehicle_disabled"),
    *((boundary, change) for boundary in ("after_prepare", "during_send", "after_finish")
      for change in ("transfer", "unlink", "vehicle_disabled")),
])
def test_relationship_commit_at_actual_dispatch_boundary(rig, monkeypatch, tmp_path, change, boundary):
    _, accepted = notice(rig)
    business = rig.runtime.business
    notification_id = accepted["result"]["notification_id"]
    trace = []

    async def scenario():
        prepared, entered, release, waiting = (asyncio.Event() for _ in range(4))
        channel = ObservedInbox(trace, entered, release)
        business.channel = channel
        original_prepare, original_finish = business.prepare_delivery, business.finish_delivery

        def observe_prepare():
            pending = original_prepare()
            if pending is not None:
                assert not rig.db.in_transaction
                trace.append("prepare_commit")
                prepared.set()
            else:
                trace.append("prepare_blocked")
            return pending

        def observe_finish(pending, outcome):
            original_finish(pending, outcome)
            assert not rig.db.in_transaction
            trace.append("finish_commit")

        monkeypatch.setattr(business, "prepare_delivery", observe_prepare)
        monkeypatch.setattr(business, "finish_delivery", observe_finish)

        async def mutate():
            waiting.set()
            await (prepared if boundary == "after_prepare" else entered).wait()
            await commit_relationship(rig, change, trace)
            release.set()

        tasks = []
        try:
            if boundary == "before_prepare":
                await commit_relationship(rig, change, trace)
                release.set()
                delivered = await business.deliver_one()
            elif boundary == "after_finish":
                release.set()
                delivered = await business.deliver_one()
                assert delivered is True
                assert [item["notification_id"] for item in
                        business.notifications("demo-driver")["items"]] == [notification_id]
                await commit_relationship(rig, change, trace)
            else:
                # Park the mutator before starting delivery. A prepare observer
                # only signals; it does not suspend or change prepare semantics.
                tasks.append(asyncio.create_task(mutate()))
                await waiting.wait()
                tasks.append(asyncio.create_task(business.deliver_one()))
                _, delivered = await asyncio.wait_for(asyncio.gather(*tasks), 5)
            row = rig.db.execute("SELECT status,error_code FROM executions WHERE execution_id=?",
                                 (accepted["execution_id"],)).fetchone()
            attempts = [dict(row) for row in rig.db.execute(
                "SELECT status,attempt_number FROM delivery_attempts WHERE notification_id=?",
                (notification_id,))]
            evidence = {"boundary": boundary, "change": change, "events": trace,
                        "channel_calls": channel.calls, "delivered": delivered,
                        "execution": dict(row), "attempts": attempts}
            (tmp_path / "dispatch-trace.json").write_text(
                json.dumps(evidence, indent=2), encoding="utf-8")
            assert_old_message_inaccessible(rig, notification_id)
            if boundary == "before_prepare":
                assert trace == ["relationship_commit", "prepare_blocked"], evidence
                assert delivered is False and attempts == [], evidence
                # Current relation is checked before the channel is entered.
                assert channel.calls == [], evidence
                assert dict(row) == {"status": "held", "error_code": "RECIPIENT_UNVERIFIED"}, evidence
                notices = rig.db.execute("SELECT notification_id,delivery_status FROM notifications").fetchall()
                assert [tuple(item) for item in notices] == [(notification_id, "failed")], evidence
            else:
                expected = (["prepare_commit", "send_entry", "relationship_commit", "finish_commit"]
                            if boundary in ("after_prepare", "during_send") else
                            ["prepare_commit", "send_entry", "finish_commit", "relationship_commit"])
                # On the supported CPython 3.12 runtime, positive wait_for
                # enters send before the prepare-event waiter can mutate.
                # This is not evidence of a pre-send revocation leak.
                assert trace == expected, evidence
                assert channel.calls == [notification_id], evidence
                # Once send has started, preserve its actual accepted result;
                # neither a receipt nor incident resolution follows from it.
                assert dict(row) == {"status": "succeeded", "error_code": None}, evidence
                assert attempts == [{"status": "accepted", "attempt_number": 1}], evidence
                assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] == "active"
                assert await business.deliver_one() is False
                assert channel.calls == [notification_id]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
