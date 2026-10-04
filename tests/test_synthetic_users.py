from copy import deepcopy

import pytest

from backend.auth import ApiError
from contracts.business import ResponseInput
from contracts.synthetic_users import SyntheticUserInput
from test_business import rig, notice, deliver


def configure(rig, mode, key="synthetic-policy"):
    r = rig.runtime
    return r.synthetic_users.configure(rig.sessions["demo-operator"], rig.run,
        SyntheticUserInput(mode=mode, expected_state_version=r.world["state_version"]), key)


@pytest.mark.parametrize("mode", ["silent", "acknowledged", "cannot_move", "question"])
def test_optin_consumer_records_source_once_and_response_is_not_motion(rig, mode):
    configure(rig, mode)
    _, result = notice(rig)
    assert deliver(rig)
    before = deepcopy(rig.runtime.world["actors"])
    rig.runtime.synthetic_users.process()
    rig.runtime.synthetic_users.process()
    assert rig.db.execute("SELECT count(*) FROM synthetic_inbox_events").fetchone()[0] == 1
    row = rig.db.execute("SELECT * FROM synthetic_inbox_events").fetchone()
    assert row["status"] == ("ignored" if mode == "silent" else "responded")
    assert rig.db.execute("SELECT count(*) FROM notification_receipts").fetchone()[0] == (0 if mode == "silent" else 1)
    assert rig.db.execute("SELECT count(*) FROM notification_responses").fetchone()[0] == (0 if mode == "silent" else 1)
    assert rig.runtime.world["actors"] == before
    assert rig.db.execute("SELECT status FROM incidents").fetchone()[0] == "active"


def test_manual_default_and_revoked_recipient_never_get_synthetic_receipt(rig):
    _, _ = notice(rig)
    deliver(rig)
    rig.runtime.synthetic_users.process()
    assert not rig.db.execute("SELECT 1 FROM synthetic_inbox_events").fetchone()
    configure(rig, "acknowledged")
    with rig.db:
        rig.db.execute("UPDATE memberships SET revoked_at='2026-10-01T00:00:00Z' WHERE user_id='demo-driver'")
    rig.runtime.synthetic_users.process()
    assert not rig.db.execute("SELECT 1 FROM notification_receipts").fetchone()


def test_human_reply_is_preserved_and_policy_changes_are_versioned(rig):
    policy = configure(rig, "question")
    r = rig.runtime
    with pytest.raises(ApiError) as wrong_role:
        r.synthetic_users.configure(rig.sessions["demo-owner"], rig.run,
            SyntheticUserInput(mode="silent", expected_state_version=r.world["state_version"]), "owner")
    assert wrong_role.value.code == "FORBIDDEN"
    _, result = notice(rig)
    deliver(rig)
    nid = result["result"]["notification_id"]
    response = r.business.reply(rig.sessions["demo-driver"], nid, "response",
        ResponseInput(client_request_id="human", response="will_move", text="합성 고객이 직접 선택"), "human")
    r.synthetic_users.process()
    assert rig.db.execute("SELECT count(*) FROM notification_responses").fetchone()[0] == 1
    event = rig.db.execute("SELECT * FROM synthetic_inbox_events").fetchone()
    assert event["response_id"] == response["response_id"] and event["reason_code"] == "EXISTING_RESPONSE"
    assert event["receipt_id"] is None
    assert event["status"] == "responded" and not r.world.get("action_queue")
    assert policy["mode"] == "synthetic_consumer"
