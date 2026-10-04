"""P06 execution subchecks with synthetic users/observations and disposable DBs."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.auth import ApiError, Auth
from backend.business import Business, MockChannel, later
from backend.runtime import Runtime
from contracts.business import ReceiptInput, ResponseInput
from simulator.world import FACILITY, advance, initial_world, set_observation_mode, utc_now


@pytest.fixture
def rig(tmp_path):
    r = Runtime(tmp_path / "business.sqlite3")
    auth = Auth(r.store)
    sessions = {name: auth.login(name, "parking-demo-only", "test")[1] for name in
                ("demo-operator", "demo-owner", "demo-driver", "demo-driver-2")}
    r.world = initial_world(1)
    for _ in range(60):
        advance(r.world)
    r.store.commit(r.world, Runtime.event(r.world))
    value = SimpleNamespace(runtime=r, db=r.store.db, sessions=sessions, run=r.world["run_id"], path=tmp_path / "business.sqlite3")
    try:
        yield value
    finally:
        value.runtime.store.close()


def call(rig, name, args, key="tool-key", user="demo-operator"):
    r, session = rig.runtime, rig.sessions[user]
    return asyncio.run(r.business_tool(session, name, args, key, r.read_task(session, rig.run)))


def context(rig):
    return {"facility_id": FACILITY, "run_id": rig.run, "based_on_state_version": rig.runtime.world["state_version"], "policy_version": 2}


def incident(rig, **changes):
    args = context(rig) | {"primary_object_id": "obj-car-02", "status": "active",
        "impacts": [{"type": "aisle_obstruction", "zone_id": "aisle-west"}],
        "evidence_ids": rig.runtime.business.analysis().observation_ids, "reason_summary": "서측 통로 차단 정지 관측"} | changes
    return call(rig, "create_or_update_incident", args, "incident-"+str(changes))


def notice(rig, *, key="notice", user="demo-operator"):
    i = incident(rig)["result"]
    r, session = rig.runtime, rig.sessions[user]
    task = r.read_task(session, rig.run)
    search = asyncio.run(r.read_tool(session,
        "search_operating_knowledge", {"facility_id": FACILITY, "run_id": rig.run, "query": "통로 차단 이동 요청과 미응답", "topic": "parking_order"}, task))
    recipient = asyncio.run(r.recipient_tool(session, "obj-car-02", task))
    args = context(rig) | {"incident_id": i["incident_id"], "expected_resource_version": i["resource_version"],
        "recipient_ref": recipient["recipient_ref"], "contact_sequence": 1, "template_args": {"zone_label": "서측 통로"},
        "knowledge_evidence": {"retrieval_id": search["retrieval_id"], "reference_ids": [x["reference_id"] for x in search["references"]]}}
    result = asyncio.run(r.business_tool(session, "notify_vehicle_user", args, key, task))
    return args, result


def deliver(rig):
    return asyncio.run(rig.runtime.business.deliver_one())


def execution(rig, eid):
    return rig.runtime.business.execution_view(rig.runtime.business.scoped("executions", eid, "execution_id"))


def test_commit_before_delivery_receipt_response_and_no_motion(rig):
    args, accepted = notice(rig)
    nid = accepted["result"]["notification_id"]
    assert accepted["status"] == "accepted"
    assert rig.db.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 0
    assert rig.runtime.business.notifications("demo-driver")["items"] == []
    assert rig.db.execute("SELECT count(*) FROM outbox_events WHERE dispatch_status='pending'").fetchone()[0] > 0
    assert deliver(rig)
    assert execution(rig, accepted["execution_id"])["status"] == "succeeded"
    items = rig.runtime.business.notifications("demo-driver")["items"]
    assert len(items) == 1 and items[0]["delivery_status"] == "channel_accepted"
    assert rig.runtime.business.notifications("demo-driver-2")["items"] == []
    world_before = deepcopy(rig.runtime.world)
    b = rig.runtime.business
    response = b.reply(rig.sessions["demo-driver"], nid, "response", ResponseInput(client_request_id="response-1", response="will_move"), "response-key")
    assert b.notification_view(b.scoped("notifications", nid, "notification_id"))["delivery_status"] == "channel_accepted"
    receipt = b.reply(rig.sessions["demo-driver"], nid, "receipt", ReceiptInput(client_request_id="receipt-1", received_at="2099-01-01T00:00:00Z"), "receipt-key")
    assert receipt["receipt_id"] and response["response_id"]
    assert b.notifications("demo-driver")["items"][0]["delivery_status"] == "client_received"
    assert rig.runtime.world == world_before
    assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] == "active"
    assert rig.db.execute("SELECT received_at,recorded_at FROM notification_receipts").fetchone()[0] != rig.db.execute("SELECT received_at,recorded_at FROM notification_receipts").fetchone()[1]


def test_same_key_replays_terminal_and_new_key_cannot_repeat_contact(rig):
    args, original = notice(rig)
    deliver(rig)
    repeated = call(rig, "notify_vehicle_user", args, "notice")
    assert repeated["execution_id"] == original["execution_id"] and repeated["status"] == "succeeded"
    assert rig.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    with pytest.raises(ApiError) as error:
        call(rig, "notify_vehicle_user", args | {"contact_sequence": 2}, "notice")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ApiError) as error:
        call(rig, "notify_vehicle_user", args, "different-key")
    assert error.value.code == "DUPLICATE_CONTACT"
    with pytest.raises(ApiError) as error:
        call(rig, "notify_vehicle_user", args | {"contact_sequence": 2}, "second-contact")
    assert error.value.code == "CONTACT_INTERVAL"


@pytest.mark.parametrize("change,expected", [
    ("role", "ACCESS_CHANGED"), ("mapping", "RECIPIENT_UNVERIFIED"),
    ("resource", "RESOURCE_CHANGED"), ("observation", "OBSERVATION_NOT_READY"),
    ("manual", "KNOWLEDGE_CHANGED"), ("recovery", "OBSERVATION_NOT_READY")])
def test_dispatch_rechecks_and_holds_without_channel_effect(rig, change, expected):
    channel = MockChannel()
    rig.runtime.business = Business(rig.runtime, channel)
    args, result = notice(rig)
    if change == "role":
        rig.db.execute("UPDATE memberships SET revoked_at=? WHERE user_id='demo-operator'", (utc_now(),))
    if change == "mapping":
        rig.db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'")
    if change == "resource":
        rig.db.execute("UPDATE incidents SET resource_version=resource_version+1")
    if change == "observation":
        set_observation_mode(rig.runtime.world, "occluded_vehicle")
        advance(rig.runtime.world)
        advance(rig.runtime.world)
    if change == "manual":
        rig.runtime.knowledge.document_access(FACILITY, "manual-parking-order", "v1", status="withdrawn")
    if change == "recovery":
        rig.runtime.world["recovery_required"] = True
    rig.db.commit()
    assert not deliver(rig)
    view = execution(rig, result["execution_id"])
    assert view["status"] == "held" and view["error"]["code"] == expected
    assert channel.calls == []
    assert rig.db.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("outcomes,expected,count", [(["failed", "accepted"], "succeeded", 2),
    (["failed", "failed"], "failed", 2), (["unknown", "accepted"], "unknown", 1)])
def test_bounded_retry_same_logical_message_and_unknown_no_resend(rig, outcomes, expected, count):
    channel = MockChannel(outcomes)
    rig.runtime.business = Business(rig.runtime, channel)
    args, result = notice(rig)
    for _ in range(4):
        deliver(rig)
    assert execution(rig, result["execution_id"])["status"] == expected
    assert len(channel.calls) == count and len(set(channel.calls)) == 1
    assert rig.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    assert call(rig, "notify_vehicle_user", args, "notice")["status"] == expected


def test_restart_unknown_preserves_and_web_pending_rechecks_recovery(rig):
    args, result = notice(rig)
    pending = rig.runtime.business.prepare_delivery()
    assert pending and execution(rig, result["execution_id"])["status"] == "running"
    rig.runtime.store.close()
    rig.runtime = Runtime(rig.path)
    rig.db = rig.runtime.store.db
    assert rig.runtime.world["recovery_required"]
    deliver(rig)
    assert execution(rig, result["execution_id"])["status"] == "held"
    assert rig.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_simulated_transport_cannot_claim_browser_receipt(rig):
    rig.runtime.business = Business(rig.runtime, MockChannel())
    args, result = notice(rig)
    deliver(rig)
    nid = result["result"]["notification_id"]
    assert rig.runtime.business.notifications("demo-driver")["items"] == []
    with pytest.raises(ApiError) as error:
        rig.runtime.business.reply(rig.sessions["demo-driver"], nid, "receipt", ReceiptInput(client_request_id="r1", received_at=utc_now()), "r1")
    assert error.value.status == 404


def test_receipt_response_uniqueness_and_other_driver_denied(rig):
    _, result = notice(rig)
    deliver(rig)
    nid = result["result"]["notification_id"]
    args = ResponseInput(client_request_id="one-response", response="cannot_move", text="가상 시험")
    b, driver = rig.runtime.business, rig.sessions["demo-driver"]
    first = b.reply(driver, nid, "response", args, "response-key")
    assert b.reply(driver, nid, "response", args, "response-key") == first
    assert b.reply(driver, nid, "response", args, "other-key") == first
    assert rig.db.execute("SELECT count(*) FROM notification_responses").fetchone()[0] == 1
    with pytest.raises(ApiError) as error:
        b.reply(driver, nid, "response", args.model_copy(update={"response": "will_move"}), "changed")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ApiError) as error:
        b.reply(rig.sessions["demo-driver-2"], nid, "response", args, "forbidden")
    assert error.value.status == 404


def test_current_and_historical_owner_required(rig):
    _, result = notice(rig)
    deliver(rig)
    b = rig.runtime.business
    assert b.notifications("demo-driver")["items"]
    now = utc_now()
    rig.db.execute("UPDATE vehicle_users SET valid_until=? WHERE user_id='demo-driver'", (now,))
    rig.db.execute("INSERT INTO vehicle_users VALUES ('veh-demo-02','demo-driver-2',?,NULL)", (now,))
    rig.db.commit()
    assert b.notifications("demo-driver")["items"] == []
    assert b.notifications("demo-driver-2")["items"] == []


def test_outbox_after_commit_order_replay_and_projection(rig):
    _, result = notice(rig)
    assert all(evt["type"] in ("state.snapshot", "run.updated") for _, evt in rig.runtime.store.events())
    deliver(rig)
    b = rig.runtime.business
    b.publish_outbox()
    before = rig.db.execute("SELECT count(*) FROM events").fetchone()[0]
    b.publish_outbox()
    assert rig.db.execute("SELECT count(*) FROM events").fetchone()[0] == before
    seqs = [r[0] for r in rig.db.execute("SELECT stream_seq FROM outbox_events ORDER BY rowid")]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for _, evt in rig.runtime.store.events():
        if evt["type"] not in ("state.snapshot", "run.updated"):
            assert b.project_event("demo-driver-2", evt) is None
    cursor = f"evt-{rig.runtime.store.events()[0][0]}"
    events, _ = asyncio.run(rig.runtime.stream_batch(cursor, rig.run))
    assert any(e["type"] == "notification.updated" for e in events)


def test_followup_clock_and_single_plan_restart(rig):
    iid = incident(rig)["result"]["incident_id"]
    args = context(rig) | {"incident_id": iid, "clock": "sim", "due_sim_time_ms": rig.runtime.world["sim_time_ms"]+2000,
        "condition": "spatial_recheck", "max_attempts": 3}
    result = call(rig, "request_followup", args, "followup")
    b = rig.runtime.business
    b.process_followups()
    assert rig.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    # Wall time does not advance a sim deadline.
    b.clock = lambda: later(utc_now(), 100000)
    b.process_followups()
    assert rig.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    for _ in range(20):
        advance(rig.runtime.world)
    rig.runtime.store.commit(rig.runtime.world, None)
    b.process_followups()
    b.process_followups()
    assert rig.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
    assert rig.db.execute("SELECT status,attempt_count FROM followups").fetchone()[:] == ("completed", 1)
    assert rig.db.execute("SELECT status FROM plans").fetchone()[0] == "held"
    rig.runtime.store.close()
    rig.runtime = Runtime(rig.path)
    rig.db = rig.runtime.store.db
    rig.runtime.business.process_followups()
    assert rig.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 1


def test_resolve_requires_fresh_sustained_observation(rig):
    result = incident(rig)["result"]
    update = {"incident_id": result["incident_id"], "expected_resource_version": 1, "status": "resolved"}
    with pytest.raises(ApiError) as error:
        incident(rig, **update)
    assert error.value.code == "RECOVERY_NOT_CONFIRMED"
    rig.runtime.world["move_requested"] = True  # Explicit fixture input, not reply.
    for _ in range(100):
        advance(rig.runtime.world)
    assert rig.runtime.business.analysis().metrics.clearance_sustained
    resolved = incident(rig, **update)
    assert resolved["result"]["status"] == "resolved"


def test_stale_state_version_reevaluates_current_and_safety_report_without_index(rig):
    _, accepted = notice(rig)
    i = accepted["result"]["notification_id"]
    row = rig.runtime.business.scoped("notifications", i, "notification_id")
    for _ in range(2):
        advance(rig.runtime.world)
    assert deliver(rig)  # World version changed but current safe evidence still holds.
    rig.runtime.knowledge._index = lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable"))
    report = call(rig, "report_to_owner", context(rig) | {"incident_id": row["incident_id"], "reason_code": "cannot_move", "summary": "가상 안전 보고"}, "report")
    assert deliver(rig)
    assert execution(rig, report["execution_id"])["status"] == "succeeded"


def test_expired_policy_cancels_due_followup_without_new_plan(rig):
    iid = incident(rig)["result"]["incident_id"]
    args = context(rig) | {"incident_id": iid, "clock": "sim", "due_sim_time_ms": rig.runtime.world["sim_time_ms"]+100,
        "condition": "spatial_recheck", "max_attempts": 1}
    call(rig, "request_followup", args, "followup-policy")
    rig.db.execute("UPDATE policies SET retired_at=? WHERE policy_version=2", (utc_now(),))
    rig.db.commit()
    advance(rig.runtime.world)
    rig.runtime.business.process_followups()
    assert rig.db.execute("SELECT status FROM followups").fetchone()[0] == "cancelled"
    assert rig.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    assert rig.db.execute("SELECT reason_code FROM audit_events WHERE action='followup_cancel'").fetchone()[0] == "POLICY_CHANGED"


def test_unknown_survives_restart_and_never_redelivers(rig):
    rig.runtime.business = Business(rig.runtime, MockChannel(["unknown"]))
    _, result = notice(rig)
    deliver(rig)
    rig.runtime.store.close()
    rig.runtime = Runtime(rig.path)
    rig.db = rig.runtime.store.db
    assert execution(rig, result["execution_id"])["status"] == "unknown"
    assert not deliver(rig)
    assert rig.db.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 1
