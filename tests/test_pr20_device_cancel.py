"""HTTP cancellation must agree with the persisted synthetic device checkpoint."""

from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from backend.app import Settings, create_app
from backend.business import encoded, ident
from backend.knowledge import transaction
from contracts.devices import DeviceCommand
from simulator.environment import apply_device_command
from simulator.devices import operate_devices
from simulator.world import FACILITY, initial_world, utc_now


ORIGIN = "http://testserver"


@pytest.fixture
def rig(tmp_path):
    app = create_app(Settings(database=tmp_path / "devices.sqlite3", test_control=True,
                              origins=(ORIGIN,), background_ticks=False))
    with TestClient(app) as client:
        runtime = app.state.runtime

        async def setup():
            runtime.world = initial_world(3, "s3-closing-v1")
            runtime.store.commit(runtime.world, runtime.event(runtime.world))
            return runtime.world["run_id"]

        run_id = client.portal.call(setup)
        response = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                               json={"username": "demo-owner", "password": "parking-demo-only"})
        assert response.status_code == 200
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}
        yield client, runtime, run_id, headers


def seed(rig, *, command_id=None, zone="announcement-a", played=False):
    client, runtime, run_id, _ = rig

    async def insert():
        candidate = deepcopy(runtime.world)
        eid = ident("execution")
        applied = apply_device_command(candidate, DeviceCommand(action="broadcast",
            operation_id=eid, zone_id=zone, message_id="closing_notice"), now_utc=utc_now())
        assert applied.outcome == "accepted"
        with transaction(runtime.store.db):
            cid = command_id or ident("command")
            if command_id is None:
                runtime.business.insert("commands", command_id=cid, facility_id=FACILITY,
                    run_id=run_id, requester_id="demo-owner", request_text="영업 종료",
                    purpose="operational_goal", target_vehicle_id=None,
                    aggregate_status="running", normalized_goal_json=encoded({"kind": "closing", "confirmed": True}))
            runtime.business.insert("executions", execution_id=eid, facility_id=FACILITY,
                run_id=run_id, plan_id=None, command_id=cid, incident_id=None,
                tool_name="play_announcement", target_ref=zone, requester_ref="demo-owner",
                idempotency_key=eid, payload_hash="0" * 64, payload_json=encoded({"zone_id": zone}),
                status="accepted", based_on_state_version=runtime.world["state_version"],
                policy_version=2, mode="synthetic_demo",
                result_json=encoded({"operation_id": eid, "receipt": "accepted"}))
            runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
        runtime.world = candidate
        if played:
            next_world = deepcopy(runtime.world)
            runtime.advance_candidate(next_world)
            with transaction(runtime.store.db):
                runtime.store.commit(next_world, runtime.event(next_world, "run.updated"))
            runtime.world = next_world
        return cid, eid

    return client.portal.call(insert)


def tick(rig):
    client, runtime, _, _ = rig

    async def advance():
        candidate = deepcopy(runtime.world)
        runtime.advance_candidate(candidate)
        with transaction(runtime.store.db):
            runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
        runtime.world = candidate
        runtime.devices.reconcile()

    client.portal.call(advance)


@pytest.mark.parametrize("scope", ["execution", "command"])
def test_pending_http_cancel_prevents_next_tick_playback(rig, scope):
    client, runtime, _, headers = rig
    cid, eid = seed(rig)
    path = f"/api/v1/executions/{eid}/cancel" if scope == "execution" else f"/api/v1/commands/{cid}/cancel"
    response = client.post(path, json={"expected_resource_version": 1},
                           headers=headers | {"Idempotency-Key": "cancel-pending"})
    assert response.status_code == 200, response.json()
    assert client.post(path, json={"expected_resource_version": 1},
                       headers=headers | {"Idempotency-Key": "cancel-pending"}).json() == response.json()
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "cancelled"
    assert runtime.world["device_state"]["broadcasts"][0]["simulated_playback"] == "cancelled"
    tick(rig)
    assert runtime.world["device_state"]["broadcasts"][0]["simulated_playback"] == "cancelled"
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "cancelled"
    assert client.post(path, json={"expected_resource_version": 1},
                       headers=headers | {"Idempotency-Key": "stale"}).status_code == 409


@pytest.mark.parametrize("scope", ["execution", "command"])
def test_played_http_cancel_keeps_confirmed_result(rig, scope):
    client, runtime, _, headers = rig
    cid, eid = seed(rig, played=True)
    path = f"/api/v1/executions/{eid}/cancel" if scope == "execution" else f"/api/v1/commands/{cid}/cancel"
    response = client.post(path, json={"expected_resource_version": 1},
                           headers=headers | {"Idempotency-Key": "cancel-played"})
    assert response.status_code == 200, response.json()
    if scope == "command":
        assert response.json()["aggregate_status"] == "succeeded"
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "succeeded"
    assert runtime.world["device_state"]["broadcasts"][0]["simulated_playback"] == "played"
    tick(rig)
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "succeeded"


def test_device_cancel_is_idempotent_and_late_playback_is_held(rig):
    _, runtime, _, _ = rig
    _, eid = seed(rig)
    state = runtime.world["device_state"]
    from contracts.devices import DeviceState
    command = DeviceCommand(action="cancel_broadcast", operation_id="cancel-1",
                            broadcast_operation_id=eid)
    first = operate_devices(DeviceState.model_validate(state), command,
                            now_utc=utc_now(), sim_time_ms=runtime.world["sim_time_ms"])
    repeated = operate_devices(first.state, command,
                               now_utc=utc_now(), sim_time_ms=runtime.world["sim_time_ms"])
    late = operate_devices(first.state, DeviceCommand(action="broadcast_feedback",
        operation_id="late-playback", broadcast_operation_id=eid,
        channel="simulated_playback", feedback="played"),
        now_utc=utc_now(), sim_time_ms=runtime.world["sim_time_ms"])
    assert first.outcome == repeated.outcome == "accepted"
    assert repeated.state == first.state
    assert late.outcome == "held"
    assert late.state.broadcasts[0].simulated_playback == "cancelled"


@pytest.mark.parametrize("scope", ["execution", "command"])
def test_checkpoint_commit_failure_rolls_back_execution_and_device(rig, monkeypatch, scope):
    client, runtime, _, headers = rig
    cid, eid = seed(rig)
    path = f"/api/v1/executions/{eid}/cancel" if scope == "execution" else f"/api/v1/commands/{cid}/cancel"
    original = runtime.store.commit

    def fail_commit(*args, **kwargs):
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(runtime.store, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        client.post(path,
            json={"expected_resource_version": 1},
            headers=headers | {"Idempotency-Key": "rollback"})
    monkeypatch.setattr(runtime.store, "commit", original)
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "accepted"
    assert runtime.world["device_state"]["broadcasts"][0]["simulated_playback"] == "pending"
    retry = client.post(path,
        json={"expected_resource_version": 1},
        headers=headers | {"Idempotency-Key": "rollback"})
    assert retry.status_code == 200
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "cancelled"


@pytest.mark.parametrize("scope", ["execution", "command"])
def test_uncertain_playback_cannot_be_claimed_cancelled(rig, scope):
    client, runtime, _, headers = rig
    cid, eid = seed(rig)

    async def set_unknown():
        candidate = deepcopy(runtime.world)
        applied = apply_device_command(candidate, DeviceCommand(action="broadcast_feedback",
            operation_id="feedback-unknown", broadcast_operation_id=eid,
            channel="simulated_playback", feedback="unknown"), now_utc=utc_now())
        assert applied.outcome == "unknown"
        with transaction(runtime.store.db):
            runtime.store.commit(candidate, runtime.event(candidate, "run.updated"))
        runtime.world = candidate

    client.portal.call(set_unknown)
    path = f"/api/v1/executions/{eid}/cancel" if scope == "execution" else f"/api/v1/commands/{cid}/cancel"
    response = client.post(path, json={"expected_resource_version": 1},
                           headers=headers | {"Idempotency-Key": "cancel-unknown"})
    assert response.status_code == 200, response.json()
    assert client.get(f"/api/v1/executions/{eid}").json()["status"] == "unknown"
    assert runtime.world["device_state"]["broadcasts"][0]["simulated_playback"] == "unknown"
