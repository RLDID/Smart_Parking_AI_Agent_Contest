"""Cancellation must stay effective across watcher dispatch and recorded replay."""
import asyncio

from fastapi.testclient import TestClient
import pytest

from backend.app import Settings, create_app


@pytest.mark.parametrize("cancel_scope", ["execution", "command"])
def test_cancelled_broadcast_is_not_recreated_or_played_in_replay(tmp_path, cancel_scope):
    app = create_app(Settings(database=tmp_path / f"{cancel_scope}.sqlite3",
        test_control=True, origins=("http://testserver",), background_ticks=False))
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/session", headers={"Origin": "http://testserver"},
            json={"username": "demo-operator", "password": "parking-demo-only"})
        assert login.status_code == 200
        headers = {"Origin": "http://testserver",
                   "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}

        def post(path, payload, key):
            response = client.post(path, json=payload, headers=headers | {"Idempotency-Key": key})
            assert response.status_code in (200, 201), response.text
            return response.json()

        source = post("/api/v1/test/runs", {"facility_id": "fac-demo-01", "config_ref": "sim0-v1",
            "seed": 1, "fixture_ref": "s3-closing-v1"}, "create")
        run = source["run_id"]
        command = post("/api/v1/facilities/fac-demo-01/commands",
            {"run_id": run, "text": "영업 종료, 입차 제한하고 출차는 유지",
             "purpose": "operational_goal", "based_on_state_version": source["applied_state_version"]},
            "command")
        cid = command["command_id"]
        body = {"run_id": run, "action": "process", "mode": "mock", "scenario": "s3", "command_id": cid}
        proposal = post("/api/v1/test/agent/operations", body, "proposal")
        assert proposal["status"] == "confirmation_required"
        preview = client.get(f"/api/v1/commands/{cid}/plan").json()
        post(f"/api/v1/commands/{cid}/confirm",
             {"expected_resource_version": preview["command_version"]}, "confirm")
        accepted = post("/api/v1/test/agent/operations", body, "broadcast")
        assert accepted["status"] == "accepted"
        eid = accepted["execution"]["execution_id"]
        resource = client.get(f"/api/v1/{'executions' if cancel_scope == 'execution' else 'commands'}/{eid if cancel_scope == 'execution' else cid}").json()
        post(f"/api/v1/{'executions' if cancel_scope == 'execution' else 'commands'}/{eid if cancel_scope == 'execution' else cid}/cancel",
             {"expected_resource_version": resource["resource_version"]}, "cancel")

        post("/api/v1/test/agent/operations", {"run_id": run, "action": "start", "mode": "mock"}, "watch")

        async def run_watcher_cycles():
            runtime = app.state.runtime
            for _ in range(8):
                await runtime.autonomous.tick()
                for _ in range(100):
                    if not runtime.autonomous.active:
                        break
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0)
            return runtime.store.db.execute(
                "SELECT count(*) FROM executions WHERE tool_name='play_announcement'").fetchone()[0]

        assert client.portal.call(run_watcher_cycles) == 1
        for index in range(12):
            post(f"/api/v1/test/runs/{run}/control", {"action": "step"}, f"original-{index}")

        async def playback():
            return app.state.runtime.public_devices(app.state.runtime.world)["broadcasts"]

        original = client.portal.call(playback)
        assert len(original) == 1 and original[0]["simulated_playback"] == "cancelled"
        execution = client.get(f"/api/v1/executions/{eid}").json()
        assert execution["status"] == "cancelled"

        replay = post(f"/api/v1/test/runs/{run}/control", {"action": "replay"}, "replay")
        replay_run = replay["run_id"]
        for index in range(12):
            post(f"/api/v1/test/runs/{replay_run}/control", {"action": "step"}, f"replay-{index}")
        assert client.portal.call(playback) == original

        async def replay_writes():
            db = app.state.runtime.store.db
            return tuple(db.execute(f"SELECT count(*) FROM {table} WHERE run_id=?", (replay_run,)).fetchone()[0]
                         for table in ("commands", "plans", "executions", "notifications"))

        assert client.portal.call(replay_writes) == (0, 0, 0, 0)
