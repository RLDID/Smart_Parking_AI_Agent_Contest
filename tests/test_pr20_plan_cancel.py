"""A cancelled command reports the completed portion of its actual plan."""

from fastapi.testclient import TestClient

from backend.app import Settings, create_app
from backend.knowledge import transaction
from simulator.world import FACILITY, advance, initial_world


ORIGIN = "http://testserver"


def test_cancel_after_first_closing_announcement_is_partial(tmp_path):
    app = create_app(Settings(database=tmp_path / "plan-cancel.sqlite3", test_control=True,
                              origins=(ORIGIN,), background_ticks=False))
    with TestClient(app) as client:
        runtime = app.state.runtime

        async def setup():
            runtime.world = initial_world(3, "s3-closing-v1")
            for _ in range(60):
                advance(runtime.world)
            runtime.store.commit(runtime.world, runtime.event(runtime.world))
            return runtime.world["run_id"], runtime.world["state_version"]

        run_id, state_version = client.portal.call(setup)
        login = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                            json={"username": "demo-owner", "password": "parking-demo-only"})
        assert login.status_code == 200
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}

        created = client.post(f"/api/v1/facilities/{FACILITY}/commands",
            json={"run_id": run_id, "purpose": "operational_goal", "text": "영업 종료",
                  "based_on_state_version": state_version},
            headers=headers | {"Idempotency-Key": "closing-command"})
        assert created.status_code == 201, created.json()
        cid = created.json()["command_id"]
        body = {"run_id": run_id, "action": "process", "mode": "mock",
                "scenario": "s3", "command_id": cid}

        def process(key):
            result = client.post("/api/v1/test/agent/operations", json=body,
                                 headers=headers | {"Idempotency-Key": key})
            assert result.status_code == 200, result.json()
            return result.json()

        assert process("s3-clarification")["status"] == "clarification_required"
        clarified = client.post(f"/api/v1/commands/{cid}/clarify",
            json={"expected_resource_version": client.get(f"/api/v1/commands/{cid}").json()["resource_version"],
                  "goal": "closing"}, headers=headers | {"Idempotency-Key": "clarify-closing"})
        assert clarified.status_code == 200, clarified.json()
        assert process("s3-propose")["status"] == "confirmation_required"
        preview = client.get(f"/api/v1/commands/{cid}/plan").json()
        assert [(step["tool"], step.get("zone_id")) for step in preview["steps"]] == [
            ("play_announcement", "announcement-a"),
            ("play_announcement", "announcement-b"),
            ("set_entry_policy", None)]
        confirmed = client.post(f"/api/v1/commands/{cid}/confirm",
            json={"expected_resource_version": preview["command_version"]},
            headers=headers | {"Idempotency-Key": "confirm-closing"})
        assert confirmed.status_code == 200, confirmed.json()
        plan_id = confirmed.json()["plan_id"]

        assert process("s3-first-broadcast")["status"] == "accepted"

        async def playback():
            # Advance on a deep copy, as normal run control does, then persist it.
            from copy import deepcopy
            candidate = deepcopy(runtime.world)
            runtime.advance_candidate(candidate)
            with transaction(runtime.store.db):
                runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
            runtime.world = candidate
            runtime.devices.reconcile()

        client.portal.call(playback)
        client.portal.call(playback)

        async def rows():
            return [dict(row) for row in runtime.store.db.execute(
                "SELECT execution_id,tool_name,target_ref,status,plan_id FROM executions WHERE command_id=? ORDER BY rowid", (cid,))]

        first = client.portal.call(rows)
        assert len(first) == 1
        assert first[0]["status"] == "succeeded"
        assert first[0]["plan_id"] == plan_id
        assert first[0]["target_ref"] == "announcement-a"
        before = client.get(f"/api/v1/commands/{cid}").json()
        cancelled = client.post(f"/api/v1/commands/{cid}/cancel",
            json={"expected_resource_version": before["resource_version"]},
            headers=headers | {"Idempotency-Key": "cancel-after-first"})
        assert cancelled.status_code == 200, cancelled.json()
        assert cancelled.json()["aggregate_status"] == "partial"
        assert client.get(f"/api/v1/commands/{cid}").json()["cancellation_requested_at"]
        assert client.portal.call(rows) == first

        async def remaining():
            row = runtime.store.db.execute("SELECT status FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            return row[0], runtime.public_devices(runtime.world)

        plan_status, devices = client.portal.call(remaining)
        assert plan_status == "cancelled"
        assert next(g for g in devices["gates"] if g["direction"] == "entry")["entry_policy"] == "allow"
        assert [item["zone_id"] for item in devices["broadcasts"]] == ["announcement-a"]
        assert client.post(f"/api/v1/commands/{cid}/cancel",
            json={"expected_resource_version": before["resource_version"]},
            headers=headers | {"Idempotency-Key": "cancel-after-first"}).json() == cancelled.json()
        current = client.get(f"/api/v1/commands/{cid}").json()
        repeated = client.post(f"/api/v1/commands/{cid}/cancel",
            json={"expected_resource_version": current["resource_version"]},
            headers=headers | {"Idempotency-Key": "cancel-after-first-again"})
        assert repeated.status_code == 200, repeated.json()
        assert repeated.json()["aggregate_status"] == "partial"
        assert client.portal.call(rows) == first
