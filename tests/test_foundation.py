import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from shapely.geometry import box

from backend.app import Settings, create_app
from backend.auth import Session
from backend.runtime import Runtime
from backend.storage import Store
from contracts.models import ObservedObject, Observation
from simulator.world import (FACILITY, MAP, NORTH_ROUTE, advance, footprint,
                             initial_world, public_state, swept_translation)

ORIGIN = "http://testserver"
RUN = {"facility_id": FACILITY, "fixture_ref": "s1a-foundation-v1", "seed": 42,
       "config_ref": "foundation-v1"}
OPERATOR = Session("demo-operator", "test_operator", "test-only", 999999999)


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database=tmp_path / "world.sqlite3", test_control=True,
                              origins=(ORIGIN,), background_ticks=False))
    with TestClient(app) as client:
        yield client


def login(client, username="demo-operator"):
    response = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                           json={"username": username, "password": "parking-demo-only"})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}


def create_run(client, headers, key="new-run"):
    response = client.post("/api/v1/test/runs", json=RUN,
                           headers={**headers, "Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


def control(client, headers, run_id, action, key="control", params=None):
    return client.post(f"/api/v1/test/runs/{run_id}/control",
                       headers={**headers, "Idempotency-Key": key},
                       json={"action": action, "action_params": params})


def test_auth_csrf_origin_and_no_secret_echo(client):
    assert client.get(f"/api/v1/facilities/{FACILITY}/state?run_id=unknown").status_code == 401
    bad = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                      json={"username": "demo-operator", "password": "do-not-echo", "role": "owner"})
    assert bad.status_code == 422 and "do-not-echo" not in bad.text
    assert client.post("/api/v1/auth/session", json={"username": "demo-owner", "password": "parking-demo-only"}).status_code == 403
    headers = login(client)
    assert "HttpOnly" in client.cookies.jar._cookies["testserver.local"]["/"]["parking_session"]._rest
    assert client.post("/api/v1/test/runs", json=RUN, headers={"Origin": ORIGIN, "Idempotency-Key": "missing-csrf"}).status_code == 403
    assert client.post("/api/v1/test/runs", json=RUN, headers={**headers, "Origin": "https://evil.invalid", "Idempotency-Key": "origin"}).status_code == 403
    assert client.post("/api/v1/test/runs", json=RUN, headers=headers).status_code == 400
    assert client.get(f"/api/v1/facilities/other/map").status_code == 404


@pytest.mark.parametrize("username", ["demo-owner", "demo-driver"])
def test_only_test_operator_can_change_world(client, username):
    headers = login(client, username)
    assert client.post("/api/v1/test/runs", json=RUN, headers={**headers, "Idempotency-Key": "forbidden"}).status_code == 403
    expected = 200 if username == "demo-owner" else 403
    assert client.get(f"/api/v1/facilities/{FACILITY}/map").status_code == expected


def test_disabled_test_controls(tmp_path):
    app = create_app(Settings(database=tmp_path/"disabled.sqlite3", origins=(ORIGIN,), background_ticks=False))
    with TestClient(app) as client:
        headers = login(client)
        assert client.get("/devtools").status_code == 404
        assert client.post("/api/v1/test/runs", json=RUN, headers={**headers, "Idempotency-Key": "off"}).status_code == 403


def test_nonloopback_cannot_use_public_demo_accounts(tmp_path):
    app = create_app(Settings(database=tmp_path/"remote.sqlite3", background_ticks=False))
    with TestClient(app, client=("192.0.2.10", 4000)) as client:
        assert client.get("/health/ready").status_code == 403


def test_login_rate_limit_and_logout_revocation(client):
    for _ in range(10):
        response = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                               json={"username": "unknown", "password": "wrong"})
        assert response.status_code == 401
    assert client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                       json={"username": "demo-owner", "password": "parking-demo-only"}).status_code == 429


def test_logout_invalidates_old_cookie_and_repeated_logout(client):
    headers = login(client)
    token = client.cookies.get("parking_session")
    assert client.delete("/api/v1/auth/session", headers=headers).status_code == 204
    assert client.delete("/api/v1/auth/session", headers={"Origin": ORIGIN}).status_code == 204
    client.cookies.set("parking_session", token)
    assert client.get("/api/v1/me").status_code == 401


def test_idempotency_input_validation_and_unsupported_actions(client):
    headers = login(client)
    first = create_run(client, headers)
    assert create_run(client, headers) == first
    conflict = client.post("/api/v1/test/runs", json={**RUN, "seed": 43},
                           headers={**headers, "Idempotency-Key": "new-run"})
    assert conflict.status_code == 409
    bad = client.post("/api/v1/test/runs", json={**RUN, "fixture_ref": "../../tests/expected"},
                      headers={**headers, "Idempotency-Key": "path"})
    assert bad.status_code == 422
    assert control(client, headers, first["run_id"], "teleport", "teleport").status_code == 422
    reset = control(client, headers, first["run_id"], "reset", "reset")
    assert reset.status_code == 200 and reset.json()["run_id"] != first["run_id"]
    assert control(client, headers, first["run_id"], "reset", "reset").json() == reset.json()
    replay = control(client, headers, reset.json()["run_id"], "replay", "replay")
    assert replay.status_code == 200 and replay.json()["run_id"] != reset.json()["run_id"]
    assert control(client, headers, "old-run", "step").status_code == 404


def test_step_observation_cadence_and_start_pause(client):
    headers = login(client)
    run_id = create_run(client, headers)["run_id"]
    one = control(client, headers, run_id, "step", "step-1").json()
    assert one["applied_sim_time_ms"] == 100
    assert one["snapshot"]["sim_time_ms"] == 0
    assert control(client, headers, run_id, "step", "step-1").json() == one
    two = control(client, headers, run_id, "step", "step-2").json()
    assert two["snapshot"]["sim_time_ms"] == 200
    assert control(client, headers, run_id, "start", "start").json()["run_status"] == "running"
    assert control(client, headers, run_id, "step", "live-step").status_code == 409
    assert control(client, headers, run_id, "pause", "pause").json()["run_status"] == "paused"
    assert client.get(f"/api/v1/facilities/{FACILITY}/state").status_code == 422


def test_synthetic_driver_move_does_not_teleport_or_resolve_incident(client):
    headers = login(client)
    run_id = create_run(client, headers)["run_id"]
    one = control(client, headers, run_id, "step", "move", {"request_vehicle_move": "obj-car-02"})
    assert one.status_code == 200
    two = control(client, headers, run_id, "step", "next").json()
    vehicle = next(x for x in two["snapshot"]["objects"] if x["object_id"] == "obj-car-02")
    assert vehicle["position"]["y"] == pytest.approx(20.4)
    assert "incident" not in json.dumps(two)
    assert "move_requested" not in json.dumps(two)


def test_public_projection_and_contract_reject_invalid_coordinates():
    world = initial_world(1)
    encoded = json.dumps(public_state(world))
    for forbidden in ("actor_id", "actors", "seed", "move_requested", "pending_events", "fixture_ref", "scenario", "expected", "future"):
        assert forbidden not in encoded
    observation = world["observation"]
    assert Observation.model_validate(observation)
    for bad in (-1, 0.5, True):
        with pytest.raises(ValidationError):
            Observation.model_validate({**observation, "sim_time_ms": bad})
    obj = deepcopy(observation["objects"][0])
    obj["position"]["x"] = float("nan")
    with pytest.raises(ValidationError):
        ObservedObject.model_validate(obj)
    with pytest.raises(ValidationError):
        ObservedObject.model_validate({**observation["objects"][0], "future_path": []})
    for field, invalid in [("schema_version", "future-schema"), ("facility_id", ""),
                           ("observed_at", "2026-09-30T09:00:00+09:00")]:
        with pytest.raises(ValidationError):
            Observation.model_validate({**observation, field: invalid})
    future_event = {"object_id": "x", "event_type": "exited", "portal_id": "portal-north",
                    "sim_time_ms": 1, "observed_at": observation["observed_at"],
                    "quality": {"visibility": "visible"}}
    with pytest.raises(ValidationError):
        Observation.model_validate({**observation, "object_events": [future_event]})


def test_full_body_sweep_blocks_between_endpoints_and_nonconvex_escape():
    before = dict(initial_world(1)["actors"][1], length_m=.2, width_m=.2, y=20)
    after = dict(before, y=22)
    obstacle = box(3.99, 20.9, 4.01, 21.1)
    assert not footprint(before).intersects(obstacle)
    assert not footprint(after).intersects(obstacle)
    assert swept_translation(before, after).intersects(obstacle)
    assert NORTH_ROUTE.covers(swept_translation(before, after))
    assert not NORTH_ROUTE.covers(swept_translation(before, dict(after, x=8)))
    with pytest.raises(ValueError):
        swept_translation(before, dict(after, heading_deg=0))


def test_collision_prevents_movement_and_exit_requires_observed_crossing():
    world = initial_world(1)
    world["move_requested"] = True
    blocker = dict(world["actors"][0], x=4, y=24.6)
    world["actors"][0] = blocker
    advance(world)
    assert world["movement_blocked"] and world["actors"][1]["y"] == 20
    world["actors"][0]["x"] = 9.5
    exits = []
    for _ in range(80):
        advance(world)
        exits.extend(world["observation"]["object_events"])
    assert any(event["event_type"] == "exited" and event["portal_id"] == "portal-north" for event in exits)
    assert all(obj["object_id"] != "obj-car-02" for obj in world["observation"]["objects"])
    # Disappearance isn't itself synthesized as a portal event.
    empty = initial_world(1)
    empty["actors"].clear()
    advance(empty); advance(empty)
    assert empty["observation"]["object_events"] == []


def test_sqlite_recovery_idempotency_and_duplicate_writer(tmp_path):
    db = tmp_path / "recovery.sqlite3"
    async def first():
        runtime = Runtime(db)
        try:
            result = await runtime.mutate(OPERATOR, "create", "create", RUN)
            await runtime.mutate(OPERATOR, "start", "control", {"action": "start"}, result["run_id"])
            await runtime.tick()
            with pytest.raises(RuntimeError, match="world writer"):
                Store(db)
            return result
        finally:
            runtime.store.close()
    original = asyncio.run(first())
    async def restart():
        runtime = Runtime(db)
        try:
            assert runtime.world["run_status"] == "paused"
            assert runtime.world["recovery_required"]
            assert runtime.world["sim_time_ms"] == 100
            assert await runtime.mutate(OPERATOR, "create", "create", RUN) == original
            assert runtime.world["sim_time_ms"] == 100
            await runtime.mutate(OPERATOR, "pause-after-restart", "control", {"action": "pause"}, original["run_id"])
            assert runtime.world["recovery_required"]
            await runtime.mutate(OPERATOR, "step-after-restart", "control", {"action": "step"}, original["run_id"])
            assert not runtime.world["recovery_required"]
            assert runtime.world["sim_time_ms"] == 200
        finally:
            runtime.store.close()
    asyncio.run(restart())


def test_database_failure_does_not_publish_or_change_memory(client):
    headers = login(client)
    run = create_run(client, headers)
    def inject():
        runtime = client.app.state.runtime
        runtime.store.db.execute("CREATE TRIGGER fail_writes BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'test'); END")
    client.portal.call(inject)
    result = control(client, headers, run["run_id"], "step", "fail")
    assert result.status_code == 503
    state = client.get(f"/api/v1/facilities/{FACILITY}/state?run_id={run['run_id']}").json()
    assert state["applied_sim_time_ms"] == 0
    assert state["applied_state_version"] == 0


def test_sse_initial_resume_expired_cursor_and_run_switch(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path/"stream.sqlite3")
        try:
            first = await runtime.mutate(OPERATOR, "a", "create", RUN)
            run_id = first["run_id"]
            batch, cursor = await runtime.stream_batch(None, run_id)
            assert [e["type"] for e in batch] == ["state.snapshot"]
            await runtime.mutate(OPERATOR, "s", "control", {"action": "step"}, run_id)
            resumed, current = await runtime.stream_batch(cursor, run_id)
            assert len(resumed) == 1 and resumed[0]["event_id"] != cursor
            assert (await runtime.stream_batch(current, run_id))[0] == []
            reset, _ = await runtime.stream_batch("evt-999999", run_id)
            assert [e["type"] for e in reset] == ["reset_required", "state.snapshot"]
            second = await runtime.mutate(OPERATOR, "b", "create", RUN)
            reset, _ = await runtime.stream_batch(current, run_id)
            assert all(e["run_id"] == second["run_id"] for e in reset)
        finally:
            runtime.store.close()
    asyncio.run(exercise())


def test_background_ticks_continue_during_independent_async_wait(tmp_path):
    async def exercise():
        runtime = Runtime(tmp_path/"async.sqlite3")
        task = None
        try:
            created = await runtime.mutate(OPERATOR, "create", "create", RUN)
            await runtime.mutate(OPERATOR, "start", "control", {"action": "start"}, created["run_id"])
            task = asyncio.create_task(runtime.loop())
            await asyncio.sleep(.45)  # Represents an independent I/O wait, not a real LLM.
            assert runtime.world["sim_time_ms"] >= 200
            assert runtime.world["observation"]["sim_time_ms"] >= 200
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            runtime.store.close()
    asyncio.run(exercise())


def test_schema_mismatch_preserves_database(tmp_path):
    path = tmp_path/"old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="schema"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_runtime_source_has_no_evaluation_imports():
    root = Path(__file__).resolve().parents[1]
    for directory in ("backend", "contracts", "simulator"):
        for path in (root/"code"/directory).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "tests/expected" not in text and "tests.expected" not in text


def test_readiness_and_devtools_are_honest(client):
    ready = client.get("/health/ready").json()
    assert ready["llm"] == "not_configured"
    assert ready["notification"] == "local_web_inbox"
    assert ready["rag"] == "ready"
    assert "개발 검증용" in client.get("/devtools").text
    assert client.get("/devtools/app.js").status_code == 200
