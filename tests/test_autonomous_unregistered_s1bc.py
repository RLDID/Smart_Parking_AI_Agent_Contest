"""Local Runtime/Business/Agent acceptance for initially unmapped S1-b/c targets.

These reuse the supported SIM-0 fixtures, not frozen-final acceptance inputs.
Only the blocker public ID changes before the first persisted observation.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.auth import ApiError, Auth
from backend.business import MockChannel
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from contracts.synthetic_users import SyntheticUserInput
from simulator.world import advance, initial_world, observe


@pytest.mark.parametrize("scenario,fixture,impact,ticks", [
    ("s1b", "s1b-blocked-v1", "exit_blocked", 230),
    ("s1c", "s1c-overlap-v1", "bay_intrusion", 300),
])
def test_initially_unregistered_s1bc_holds_without_synthetic_movement(
        tmp_path, scenario, fixture, impact, ticks):
    async def exercise():
        runtime = Runtime(tmp_path / f"{scenario}-unregistered.sqlite3")
        try:
            db, business = runtime.store.db, runtime.business
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            authenticate = lambda: auth.require(token)
            target = f"obj-car-unregistered-{scenario}"
            world = initial_world(2, fixture)
            blocker = next(a for a in world["actors"] if a["object_id"] == "obj-car-02")
            blocker["object_id"] = target
            # Discard the factory's unpersisted frame; there is no historical
            # registration/unlink or old identity in the stored observation.
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
            with pytest.raises(ApiError) as error:
                business.resolve_recipient(target)
            assert error.value.code == "RECIPIENT_UNVERIFIED"
            # Seeded registrations remain valid, including the old public ID.
            assert business.resolve_recipient("obj-car-02")["user_id"] == "demo-driver"
            registry_before = {table: [tuple(row) for row in db.execute(
                f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("object_mappings", "vehicles", "vehicle_users")}
            before = business.impact_assessment(impact, target, "B01")
            assert before["support_status"] == "supported" and before["violation_candidate"]
            assert before["observation_ids"] and not before["clearance_sustained"]
            eligible = [item for item in runtime.operating_candidates(scenario)
                        if item["assessment"]["support_status"] == "supported"
                        and item["assessment"]["violation_candidate"]]
            assert [(item["object_id"], item["zone_id"]) for item in eligible] == [(target, "B01")]
            assert db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
            # Exercise the real opt-in consumer as well. An owner report must
            # never become a private move request or synthesize a driver reply.
            runtime.synthetic_users.configure(operator, run,
                SyntheticUserInput(mode="will_move", expected_state_version=world["state_version"]),
                f"{scenario}-consumer")
            channel = MockChannel()
            business.channel = channel
            actors_before = deepcopy(runtime.world["actors"])
            body = AutonomousControl(run_id=run, action="process", mode="mock", scenario=scenario)

            def assert_no_private_effects():
                assert db.execute("SELECT count(*) FROM notifications WHERE purpose!='owner_report'").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM executions WHERE tool_name='notify_vehicle_user'").fetchone()[0] == 0
                assert db.execute("""SELECT count(*) FROM delivery_attempts a JOIN notifications n
                    USING(notification_id) WHERE n.purpose!='owner_report'""").fetchone()[0] == 0
                for table in ("notification_receipts", "notification_responses", "synthetic_inbox_events"):
                    assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                for username in ("demo-driver", "demo-driver-2"):
                    assert business.notifications(username)["items"] == []
                assert runtime.world["actors"] == actors_before
                assert runtime.world["action_queue"] == []
                assert not runtime.world["move_requested"]
                assert registry_before == {table: [tuple(row) for row in db.execute(
                    f"SELECT * FROM {table} ORDER BY rowid")] for table in registry_before}

            first = await runtime.autonomous.control(operator, body, f"{scenario}-first", authenticate)
            assert (first["status"], first["reason_code"]) == ("held", "RECIPIENT_UNVERIFIED")
            assert first["decision"]["action"] == "notify" and first["decision"]["target_ref"] == target
            assert first["model"]["model_call_count"] == 0
            assert first["report"]["status"] == "accepted"
            assert first["report"]["result"]["delivery_status"] == "queued"
            iid = first["incident_id"]
            incident = db.execute("SELECT incident_id,primary_object_id,status FROM incidents").fetchall()
            assert [tuple(row) for row in incident] == [(iid, target, "needs_review")]
            assert [tuple(row) for row in db.execute("SELECT type,zone_id FROM incident_impacts")] == [(impact, "B01")]
            searches = [json.loads(row[0]) for row in db.execute(
                "SELECT result_json FROM knowledge_retrievals ORDER BY rowid")]
            assert len(searches) == 1 and searches[0]["status"] == "matched" and searches[0]["references"]
            notices = [dict(row) for row in db.execute(
                "SELECT notification_id,purpose,recipient_user_id FROM notifications")]
            assert len(notices) == 1
            assert notices[0]["purpose"] == "owner_report" and notices[0]["recipient_user_id"] == "demo-owner"
            assert await business.deliver_one() is True
            assert await business.deliver_one() is False
            runtime.synthetic_users.process()
            assert channel.calls == [notices[0]["notification_id"]]
            assert_no_private_effects()

            # Same key is a persisted replay, and a new key rechecks the same
            # incident without scheduling a second report/delivery.
            changes = db.total_changes
            same = await runtime.autonomous.control(operator, body, f"{scenario}-first", authenticate)
            assert same == first and db.total_changes == changes
            retry = await runtime.autonomous.control(operator, body, f"{scenario}-new-key", authenticate)
            assert (retry["status"], retry["reason_code"]) == ("held", "RECIPIENT_UNVERIFIED")
            assert retry["incident_id"] == iid and retry["decision"]["action"] == "recheck"
            assert_no_private_effects()

            # Reuse the existing scenario's 230/300 additional ticks. No
            # customer input is injected: later observations alone cannot
            # resolve a still-blocked/unregistered vehicle's incident.
            for _ in range(ticks):
                runtime.advance_candidate(runtime.world)
                runtime.synthetic_users.process()
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            after = business.impact_assessment(impact, target, "B01")
            assert runtime.world["sim_time_ms"] == (60 + ticks) * 100
            assert after["support_status"] == "supported" and after["violation_candidate"]
            assert after["observation_ids"] != before["observation_ids"]
            assert not after["clearance_sustained"]
            followup = await runtime.autonomous.control(operator, body, f"{scenario}-followup", authenticate)
            assert (followup["status"], followup["reason_code"]) == ("held", "RECIPIENT_UNVERIFIED")
            assert followup["incident_id"] == iid and followup["decision"]["action"] == "recheck"
            assert db.execute("SELECT status FROM incidents WHERE incident_id=?", (iid,)).fetchone()[0] == "needs_review"
            assert db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM executions WHERE tool_name='report_to_owner'").fetchone()[0] == 1
            assert await business.deliver_one() is False
            runtime.synthetic_users.process()
            assert channel.calls == [notices[0]["notification_id"]]
            assert_no_private_effects()
            assert not runtime.autonomous.active
            jobs = [dict(row) for row in db.execute("SELECT status,result_json FROM autonomous_jobs ORDER BY rowid")]
            assert len(jobs) == 3 and all(row["status"] == "held" for row in jobs)
            assert [json.loads(row["result_json"]) for row in jobs] == [first, retry, followup]
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("incidents", "autonomous_jobs", "executions", "notifications",
                                    "delivery_attempts", "notification_responses", "synthetic_inbox_events")}
            evidence = {"scenario": scenario, "fixture": fixture, "seed": 2,
                        "initial_ticks": 60, "followup_ticks": ticks,
                        "sim_time_ms": runtime.world["sim_time_ms"], "target": target,
                        "condition": "initially unmapped public identity; registry unchanged; will_move consumer enabled",
                        "before": before, "after": after, "search": searches[0],
                        "first": first, "retry": retry, "followup": followup,
                        "counts": counts, "channel_calls": channel.calls,
                        "limits": "local mock supported fixture; no frozen-final or whole T07/T12 acceptance"}
            (tmp_path / f"{scenario}-unregistered-trace.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            await runtime.autonomous.close()
            runtime.store.close()

    asyncio.run(exercise())
