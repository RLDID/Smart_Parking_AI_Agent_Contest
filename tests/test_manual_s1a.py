"""Manual S1-a integration (synthetic environment, real business path, no LLM)."""
import asyncio
from copy import deepcopy

import pytest

from agent.manual import ManualS1
from backend.auth import ApiError
from backend.business import later
from contracts.business import ReceiptInput, ResponseInput
from simulator.world import advance, set_observation_mode, utc_now
from test_business import rig  # shared disposable synthetic fixture


def manual(rig, action="notify", key="walkthrough", **fields):
    return asyncio.run(rig.runtime.manual_s1a(rig.sessions["demo-operator"], ManualS1(run_id=rig.run, action=action, **fields), key))


def test_manual_full_path_independent_input_then_fresh_sustained_resolution(rig):
    r = rig.runtime
    result = manual(rig)
    assert result["mode"] == "manual" and result["knowledge"]["status"] == "matched"
    assert result["status"] == "accepted"
    assert not r.business.notifications("demo-driver")["items"]
    asyncio.run(r.business.deliver_one())
    notice = r.business.notifications("demo-driver")["items"][0]
    r.business.reply(rig.sessions["demo-driver"], notice["notification_id"], "receipt", ReceiptInput(client_request_id="screen", received_at=utc_now()), "screen")
    before = deepcopy(r.world)
    r.business.reply(rig.sessions["demo-driver"], notice["notification_id"], "response", ResponseInput(client_request_id="answer", response="will_move"), "answer")
    assert r.world == before
    assert manual(rig, "review_timeout", "review", incident_id=result["incident_id"])["status"] == "awaiting_observation"
    assert manual(rig, "recheck", "still-blocked", incident_id=result["incident_id"])["status"] == "monitoring"
    # Motion is a separate simulator control, not a consequence of receipt/reply.
    asyncio.run(r.mutate(rig.sessions["demo-operator"], "explicit-world-input", "control", {"action": "step", "action_params": {"request_vehicle_move": "obj-car-02"}}, rig.run))
    for _ in range(100):
        advance(r.world)
    r.store.commit(r.world, r.event(r.world))
    resolved = manual(rig, "recheck", "fresh-recovery", incident_id=result["incident_id"])
    assert resolved["status"] == "resolved"
    assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] == "resolved"
    assert rig.db.execute("SELECT status FROM plans WHERE model_ref='manual_s1a'").fetchone()[0] == "completed"
    assert rig.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1
    assert manual(rig)["execution"]["status"] == "succeeded"


def test_missing_observation_cannot_resolve_and_rag_failure_only_reports(rig):
    result = manual(rig)
    set_observation_mode(rig.runtime.world, "missing_vehicle")
    for _ in range(2):
        advance(rig.runtime.world)
    updated = manual(rig, "recheck", "missing", incident_id=result["incident_id"])
    assert updated["status"] == "needs_review"
    assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] != "resolved"


def test_withdrawn_manual_preserves_incident_and_independent_owner_report(rig):
    rig.runtime.knowledge.document_access("fac-demo-01", "manual-parking-order", "v1", status="withdrawn")
    result = manual(rig)
    assert result["status"] == "held" and result["execution"] is None
    assert result["report"]["status"] == "accepted"
    repeated = manual(rig, key="same-held-another-request")
    assert repeated["report"]["execution_id"] == result["report"]["execution_id"]
    assert rig.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 1
    assert rig.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 0
    assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] == "active"
    asyncio.run(rig.runtime.business.deliver_one())
    assert rig.runtime.business.notifications("demo-owner")["items"]


def test_manual_role_paused_and_idempotency_guards(rig):
    with pytest.raises(ApiError) as error:
        asyncio.run(rig.runtime.manual_s1a(rig.sessions["demo-owner"], ManualS1(run_id=rig.run, action="notify"), "owner"))
    assert error.value.status == 403
    rig.runtime.world["run_status"] = "running"
    with pytest.raises(ApiError) as error:
        manual(rig)
    assert error.value.code == "PAUSE_REQUIRED"
    rig.runtime.world["run_status"] = "paused"
    first = manual(rig)
    repeated = manual(rig)
    assert first["execution"]["execution_id"] == repeated["execution"]["execution_id"]
    with pytest.raises(ApiError) as error:
        manual(rig, "recheck", incident_id=first["incident_id"])
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_timeout_report_and_incident_contact_total_budget(rig):
    result = manual(rig)
    asyncio.run(rig.runtime.business.deliver_one())
    with pytest.raises(ApiError) as error:
        manual(rig, "review_timeout", "early", incident_id=result["incident_id"])
    assert error.value.code == "RESPONSE_NOT_DUE"
    clock = [later(utc_now(), 61000)]
    rig.runtime.business.clock = lambda: clock[0]
    reported = manual(rig, "review_timeout", "due", incident_id=result["incident_id"])
    assert reported["status"] == "escalated_review" and reported["report"]["status"] == "accepted"
    repeated = manual(rig, "review_timeout", "due-again", incident_id=result["incident_id"])
    assert repeated["report"]["execution_id"] == reported["report"]["execution_id"]
    assert rig.db.execute("SELECT count(*) FROM notifications WHERE purpose='owner_report'").fetchone()[0] == 1
    assert not rig.runtime.world["move_requested"]
    clock[0] = later(clock[0], 180000)
    blocked = manual(rig, "notify", "expired-contact")
    assert blocked["status"] == "held" and blocked["reason_code"] == "INCIDENT_CONTACT_EXPIRED"
    assert rig.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0] == 1


@pytest.mark.parametrize("steps", ["[]", '[{"tool":"notify_vehicle_user","execution_id":"replaced-execution"}]'])
def test_changed_plan_step_holds_queued_notice_before_dispatch(rig, steps):
    result = manual(rig)
    rig.db.execute("UPDATE plans SET steps_json=? WHERE plan_id=?", (steps, result["plan_id"]))
    rig.db.commit()
    assert not asyncio.run(rig.runtime.business.deliver_one())
    view = rig.runtime.business.execution_view(rig.runtime.business.scoped(
        "executions", result["execution"]["execution_id"], "execution_id"))
    assert view["status"] == "held" and view["error"]["code"] == "PLAN_STEP_CHANGED"
    assert not rig.runtime.business.notifications("demo-driver")["items"]
    assert rig.db.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 0
