"""Supplemental local completion flows exercise the real product path."""
import importlib.util
import asyncio
from copy import deepcopy
from contextlib import contextmanager
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_local_flows.py"
SPEC = importlib.util.spec_from_file_location("local_flow_completion", SCRIPT)
flows = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flows)


@pytest.mark.parametrize("case", flows.CASES)
def test_local_product_flow(tmp_path, case):
    result = flows.run_case(case, tmp_path)
    assert result["temporary_database_removed"]
    assert result["status"] == "passed", result.get("failure")


def place(rig, pose):
    candidate = deepcopy(rig.r.world)
    actor = next((a for a in candidate["actors"] if a["object_id"] == "obj-car-02"), None)
    if actor is None:
        actor = {"actor_id": "internal-b", "object_id": "obj-car-02", "object_type": "vehicle",
                 "length_m": 4.6, "width_m": 1.8}
        candidate["actors"].append(actor)
    actor.update(pose)
    candidate["move_requested"] = False
    candidate["state_version"] += 1
    rig.r.store.commit(candidate, flows.Runtime.event(candidate, "run.updated"))
    rig.r.world = candidate


@contextmanager
def isolated_rig(directory):
    with tempfile.TemporaryDirectory(prefix="boundary-", dir=directory) as temporary:
        with flows.Rig(temporary, []) as rig:
            yield rig
    assert not Path(temporary).exists()


def test_s1_incident_partial_close_restart_revoke_and_recurrence(tmp_path):
    async def exercise():
        with isolated_rig(tmp_path) as rig:
            iid = await flows.new_impact(rig)
            version = rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"]
            with pytest.raises(flows.ApiError) as partial:
                await rig.incident([flows.IMPACTS["s1a"]], iid=iid, status="resolved")
            assert partial.value.code == "RECOVERY_NOT_CONFIRMED"
            assert rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"] == version
            duplicate = await rig.incident([flows.IMPACTS["s1b"]])
            assert duplicate["resource_version"] == version
            assert rig.count("incident_impacts") == 2
            # Saved cumulative impacts survive explicit recovery without resend.
            rig.r.store.close()
            rig.r = None
            rig.open()
            await rig.step(60)
            await rig.agent("s1a")
            await rig.deliver()
            assert rig.count("incident_impacts") == 2 and len(rig.inbox.sent) == 1
            # A cleared old impact cannot suppress current-recipient review.
            rig.clock.advance(1)
            rel = flows.Relationships(rig.db)
            body = flows.VehicleUserChange(expected_version=0, user_id=None, reason="가상 연결 해제")
            rel.execute("demo-operator", rig.key("unlink"), "vehicle.user", body.model_dump(),
                lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body))
            await rig.agent("s1a")
            assert rig.status(iid) == "needs_review"
            assert rig.count("notifications", "purpose='owner_report'") == 1
            await rig.agent("s1b")
            assert rig.count("notifications", "purpose='owner_report'") == 1
            # Both known impacts must clear before this episode can close.
            place(rig, {"x": 27.0, "y": 21.7, "heading_deg": 0.0})
            await rig.step(60)
            assert rig.assessment("s1a")["clearance_sustained"]
            assert rig.assessment("s1b")["clearance_sustained"]
            await rig.agent("s1a")
            assert rig.status(iid) == "resolved"
            with pytest.raises(flows.ApiError) as closed:
                await rig.incident([flows.IMPACTS["s1a"]], iid=iid, status="active")
            assert closed.value.code == "INCIDENT_CLOSED"
            place(rig, {"x": 4.0, "y": 20.0, "heading_deg": 90.0})
            await rig.step(60)
            next_episode = await rig.incident([flows.IMPACTS["s1a"]])
            assert next_episode["incident_id"] != iid
            assert rig.status(iid) == "resolved"
            assert rig.r.business.scoped("incidents", next_episode["incident_id"], "incident_id")["previous_incident_id"] == iid
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["new_impact_unlinked", "review_relinked"])
def test_s1_incident_review_relationship_transition(tmp_path, change, record_property):
    async def exercise():
        with isolated_rig(tmp_path) as rig:
            await rig.create()
            rig.configure("will_move" if change == "new_impact_unlinked" else "manual")
            original = deepcopy(next(a for a in rig.r.world["actors"] if a["object_id"] == "obj-car-02"))
            result, old_nid = await rig.notify()
            iid = result["incident_id"]
            if change == "new_impact_unlinked":
                # Compatibility input: a pre-change durable S1-a incident held
                # only its aisle impact. It is not new geometric truth.
                rig.db.execute("DELETE FROM incident_impacts WHERE incident_id=? AND type!='aisle_obstruction'", (iid,))
                rig.db.commit()
                rig.record("legacy.persisted_single_impact", {"incident_id": iid, "stored_impacts": rig.count("incident_impacts")})
                assert rig.count("incident_impacts") == 1
                await rig.deliver()
                rig.r.synthetic_users.process()
                await rig.step(100)
                assert rig.assessment("s1a")["clearance_sustained"]
            rel = flows.Relationships(rig.db)
            rig.clock.advance(1)
            body = flows.VehicleUserChange(expected_version=0, user_id=None, reason="가상 연결 해제")
            rel.execute("demo-operator", rig.key("unlink"), "vehicle.user", body.model_dump(),
                lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body))
            if change == "new_impact_unlinked":
                place(rig, original | {"x": 15.1, "y": 21.7, "heading_deg": 0.0})
                await rig.step(60)
                assert rig.assessment("s1b")["violation_candidate"]
                assert rig.assessment("s1a")["clearance_sustained"]
                assert not rig.assessment("s1a")["violation_candidate"]
                assert rig.count("incident_impacts") == 1
                rig.record("new_impact_unlinked.before", {"aisle": rig.assessment("s1a"), "exit": rig.assessment("s1b")})
            await rig.agent("s1a")
            assert rig.status(iid) == "needs_review"
            assert rig.count("notifications", "purpose='owner_report'") == 1
            if change == "new_impact_unlinked":
                assert rig.count("incident_impacts", f"incident_id='{iid}'") == 2
                assert rig.count("incidents") == 1
            else:
                rig.clock.advance(1)
                body = flows.VehicleUserChange(expected_version=1, user_id="demo-driver-2", reason="가상 현재 차주 재연결")
                rel.execute("demo-operator", rig.key("relink"), "vehicle.user", body.model_dump(),
                    lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body))
                await rig.step(2)
                replanned = await rig.agent("s1a")
                assert replanned["status"] == "accepted"
                assert replanned["incident_id"] == iid and rig.status(iid) == "active"
                new = rig.db.execute("SELECT notification_id FROM notifications WHERE purpose='move_request' AND recipient_user_id='demo-driver-2'").fetchone()[0]
                for _ in range(3):
                    await rig.deliver()
                assert rig.r.business.scoped("notifications", old_nid, "notification_id")["delivery_status"] == "failed"
                assert [x["notification_id"] for x in rig.r.business.notifications("demo-driver-2")["items"]] == [new]
                rig.r.business.reply(rig.sessions["demo-driver-2"], new, "receipt", flows.ReceiptInput(
                    client_request_id="relink-receipt", received_at=rig.clock()), rig.key("receipt"))
                rig.r.business.reply(rig.sessions["demo-driver-2"], new, "response", flows.ResponseInput(
                    client_request_id="relink-response", response="will_move"), rig.key("response"))
                assert rig.count("notification_receipts") == 1 and rig.count("notification_responses") == 1
                assert rig.count("incidents") == 1 and rig.status(iid) != "resolved"
            record_property("observations", flows.json.dumps(rig.observations, ensure_ascii=False))
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["other_object", "other_run", "s2", "duplicate_s1", "delayed_observation"])
def test_s1_incident_scope_and_incomplete_evidence(tmp_path, boundary):
    async def exercise():
        with isolated_rig(tmp_path) as rig:
            await rig.create()
            result, _ = await rig.notify()
            iid = result["incident_id"]
            if boundary == "other_run":
                await rig.r.mutate(rig.operator, rig.key("new-run"), "create", {"seed": 42, "fixture_ref": flows.FIXTURES["s1a"]})
                assert rig.r.business.open_s1_incident("obj-car-02") is None
                assert rig.status(iid) == "active"
                return
            if boundary == "delayed_observation":
                await rig.step(12, observation_mode="delayed")
                assert rig.assessment("s1a")["support_status"] == "insufficient_data"
                with pytest.raises(flows.ApiError) as stale:
                    await rig.incident([flows.IMPACTS["s1a"]], iid=iid, status="resolved")
                assert stale.value.code == "RECOVERY_NOT_CONFIRMED"
                await rig.deliver()
                assert len(rig.inbox.sent) == 0 and rig.status(iid) != "resolved"
                return
            # Synthetic prior stored rows exercise ambiguous legacy state. They
            # are not assertions of current geometric violation or safety.
            other = "legacy-boundary-incident"
            obj = "obj-car-01" if boundary == "other_object" else "obj-car-02"
            kind, zone = ("approach_risk", "announcement-a") if boundary == "s2" else flows.IMPACTS["s1b"]
            rig.r.business.insert("incidents", incident_id=other, facility_id=flows.FACILITY, run_id=rig.run,
                primary_object_id=obj, status="active", dedup_key="legacy-boundary",
                policy_version=rig.r.knowledge.current_policy(flows.FACILITY).policy_version,
                reason_summary="독립 가상 이전 저장 경계 입력")
            rig.r.business.insert("incident_impacts", impact_id="legacy-boundary-impact", facility_id=flows.FACILITY,
                run_id=rig.run, incident_id=other, object_id=obj, type=kind, zone_id=zone, condition_json="{}")
            rig.db.commit()
            if boundary == "duplicate_s1":
                with pytest.raises(flows.ApiError) as duplicate:
                    await rig.incident([flows.IMPACTS["s1a"]])
                assert duplicate.value.code == "INCIDENT_REVIEW_REQUIRED"
                with pytest.raises(flows.ApiError) as decision:
                    await rig.agent("s1a")
                assert decision.value.code == "INCIDENT_REVIEW_REQUIRED"
            else:
                duplicate = await rig.incident([flows.IMPACTS["s1a"]])
                assert duplicate["incident_id"] == iid
                assert rig.r.business.open_s1_incident("obj-car-02")["incident_id"] == iid
                assert rig.count("incident_impacts", f"incident_id='{other}'") == 1
            assert rig.count("incidents") == 2 and len(rig.inbox.sent) == 0
    asyncio.run(exercise())


async def queued_exit_episode_with_new_impact(rig, seed=96090, pose=None):
    """Synthetic placement with no response/delivery/manual incident repair."""
    await rig.r.mutate(rig.operator, rig.key("create"), "create",
                       {"seed": seed, "fixture_ref": flows.FIXTURES["s1a"]})
    place(rig, {"x": 9.5, "y": 21.7, "heading_deg": 0.0})
    await rig.step(60)
    first, nid = await rig.notify("s1b")
    iid = first["incident_id"]
    stored = [dict(row) for row in rig.db.execute(
        "SELECT * FROM incident_impacts WHERE incident_id=? ORDER BY impact_id", (iid,))]
    assert {(row["type"], row["zone_id"]) for row in stored} == {
        flows.IMPACTS["s1a"], flows.IMPACTS["s1b"]}
    assert rig.r.business.scoped("notifications", nid, "notification_id")["delivery_status"] == "queued"
    assert rig.count("notification_receipts") == rig.count("notification_responses") == 0
    executions = rig.count("executions")
    waiting = await rig.agent("s1b")
    assert waiting["reason_code"] == "AWAITING_RESPONSE_OR_MOVEMENT"
    assert rig.count("executions") == executions
    place(rig, pose or {"x": 11.0, "y": 26.5, "heading_deg": 90.0})
    await rig.step(60)
    new = rig.assessment("s1c")
    assert new["support_status"] == "supported" and new["violation_candidate"]
    return iid, nid, stored


@pytest.mark.parametrize("scenario,seed,pose", [
    ("s1c", 96090, {"x": 11.0, "y": 26.5, "heading_deg": 90.0}),
    ("s1c", 96091, {"x": 11.0, "y": 26.5, "heading_deg": 90.0}),
    ("s1a", 96090, {"x": 10.0, "y": 22.3, "heading_deg": 60.0}),
])
def test_queued_unanswered_contact_preserves_and_links_fresh_s1_impact(tmp_path, monkeypatch, scenario, seed, pose):
    async def exercise():
        with isolated_rig(tmp_path) as rig:
            iid, nid, prior_impacts = await queued_exit_episode_with_new_impact(rig, seed, pose)
            if scenario == "s1a":
                assert rig.assessment("s1a")["violation_candidate"]
                assert not rig.assessment("s1a")["clearance_sustained"]
                # The baseline mock chooses hold here. Keep that contract;
                # independently verify the server's valid recheck guard, as a
                # bounded recommendation can choose recheck without contact.
                from backend.autonomous import MockOperationsAdapter
                baseline = MockOperationsAdapter.decide
                async def recommend_recheck(adapter, context):
                    decision = await baseline(adapter, context)
                    assert decision["action"] == "hold"
                    return decision | {"action": "recheck"}
                monkeypatch.setattr(MockOperationsAdapter, "decide", recommend_recheck)
            prior_execution_ids = {row[0] for row in rig.db.execute("SELECT execution_id FROM executions")}
            prior_audit_ids = {row[0] for row in rig.db.execute("SELECT audit_id FROM audit_events")}
            before_version = rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"]
            body = flows.AutonomousControl(run_id=rig.run, action="process", mode="mock", scenario=scenario)
            key = rig.key("new-impact")
            auth = lambda: rig.auth.require(rig.tokens["demo-operator"])
            changed = await rig.r.autonomous.control(rig.operator, body, key, auth)
            assert changed["status"] == "monitoring" and changed["incident_id"] == iid, changed
            assert changed["model"]["model_call_count"] == 0
            assert rig.count("incidents") == 1 and rig.status(iid) == "monitoring"
            assert rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"] == before_version + 1
            after = [dict(row) for row in rig.db.execute(
                "SELECT * FROM incident_impacts WHERE incident_id=? ORDER BY impact_id", (iid,))]
            assert {(row["type"], row["zone_id"]) for row in after} == {
                flows.IMPACTS["s1a"], flows.IMPACTS["s1b"], flows.IMPACTS["s1c"]}
            by_id = {row["impact_id"]: row for row in after}
            assert all(by_id[row["impact_id"]] == row for row in prior_impacts)
            assert prior_execution_ids <= {row[0] for row in rig.db.execute("SELECT execution_id FROM executions")}
            assert prior_audit_ids <= {row[0] for row in rig.db.execute("SELECT audit_id FROM audit_events")}
            assert rig.count("notifications") == 1 and rig.inbox.sent == []
            assert rig.count("notification_receipts") == rig.count("notification_responses") == 0
            assert rig.r.business.scoped("notifications", nid, "notification_id")["delivery_status"] == "queued"
            assert rig.count("executions") == len(prior_execution_ids) + 1
            evidence = {row[0] for row in rig.db.execute("SELECT observation_id FROM incident_evidence WHERE incident_id=?", (iid,))}
            assert set(rig.assessment("s1c")["observation_ids"]) <= evidence
            replay = await rig.r.autonomous.control(rig.operator, body, key, auth)
            assert replay == changed
            repeated = await rig.agent(scenario)
            assert repeated["reason_code"] == "AWAITING_RESPONSE_OR_MOVEMENT"
            assert rig.count("executions") == len(prior_execution_ids) + 1
            assert rig.count("incident_impacts") == 3 and rig.count("notifications") == 1
            assert rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"] == before_version + 1
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["delayed", "logout"])
def test_queued_new_s1_impact_still_requires_freshness_and_authority(tmp_path, boundary):
    async def exercise():
        with isolated_rig(tmp_path) as rig:
            iid, _, stored = await queued_exit_episode_with_new_impact(rig)
            executions = rig.count("executions")
            version = rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"]
            if boundary == "delayed":
                await rig.step(12, observation_mode="delayed")
                assert rig.assessment("s1c")["support_status"] == "insufficient_data"
                result = await rig.agent("s1c")
                assert result["status"] == "held"
            else:
                rig.auth.sessions.pop(rig.tokens["demo-operator"])
                with pytest.raises(flows.ApiError) as denied:
                    await rig.agent("s1c")
                assert denied.value.code == "UNAUTHENTICATED"
            assert rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"] == version
            after = [dict(row) for row in rig.db.execute(
                "SELECT * FROM incident_impacts WHERE incident_id=? ORDER BY impact_id", (iid,))]
            assert after == stored and rig.status(iid) == "active"
            assert rig.count("executions") == executions and rig.count("notifications") == 1
    asyncio.run(exercise())
