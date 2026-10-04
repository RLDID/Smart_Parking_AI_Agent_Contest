"""Relationship API changes must revoke an already-open driver stream."""

import httpx
import pytest

from simulator.world import FACILITY
from test_foundation import RUN
from test_http_stream import next_event, server


def sign_in(client, address, username):
    response = client.post("/api/v1/auth/session", headers={"Origin": address},
                           json={"username": username, "password": "parking-demo-only"})
    assert response.status_code == 200
    return {"Origin": address, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}


def object_ids(event):
    return {obj["object_id"] for obj in event["payload"]["snapshot"]["objects"]}


@pytest.mark.parametrize("change", ("transfer", "unlink", "vehicle_disabled"))
def test_paused_driver_stream_revoked_by_relationship_api_and_old_cursor_cannot_restore_access(tmp_path, change):
    # server() binds an OS-assigned loopback port and closes/join its thread.
    with server(tmp_path) as address, httpx.Client(base_url=address, timeout=5) as operator, \
            httpx.Client(base_url=address, timeout=5) as driver:
        operator_headers = sign_in(operator, address, "demo-operator")
        created = operator.post("/api/v1/test/runs", json=RUN,
                                headers=operator_headers | {"Idempotency-Key": "create-run"})
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]
        base = f"/api/v1/facilities/{FACILITY}/relationships"
        if change == "unlink":
            # A reviewed person/customer link is history, not vehicle access.
            person = operator.put(base + "/person-objects/obj-person-01",
                json={"run_id": run_id, "expected_version": 0, "user_id": "demo-driver",
                      "status": "verified", "source": "reviewed", "reason": "합성 검토"},
                headers=operator_headers | {"Idempotency-Key": "person-link"})
            assert person.status_code == 200, person.text
        sign_in(driver, address, "demo-driver")
        path = f"/api/v1/facilities/{FACILITY}/events?run_id={run_id}"
        with driver.stream("GET", path) as stream:
            assert stream.status_code == 200
            lines = stream.iter_lines()
            first = next_event(lines)
            assert first["type"] == "state.snapshot"
            assert object_ids(first) == {"obj-car-02"}
            old_cursor = first["event_id"]

            stepped = operator.post(f"/api/v1/test/runs/{run_id}/control",
                json={"action": "step"},
                headers=operator_headers | {"Idempotency-Key": "paused-step"})
            assert stepped.status_code == 200, stepped.text
            changed = next_event(lines)
            assert changed["event_id"] != old_cursor
            assert object_ids(changed) == {"obj-car-02"}

            if change == "vehicle_disabled":
                target = base + "/vehicles/veh-demo-02"
                method = "PATCH"
                body = {"expected_version": 0, "active": False, "reason": "가상 차량 비활성"}
            else:
                target = base + "/vehicles/veh-demo-02/customer"
                method = "PUT"
                body = {"expected_version": 0,
                        "user_id": "demo-driver-2" if change == "transfer" else None,
                        "reason": "가상 차주 변경"}
            update = operator.request(method, target, json=body,
                headers=operator_headers | {"Idempotency-Key": "change-relationship"})
            assert update.status_code == 200, update.text

            # The run remains paused, so this is the stream's scope poll rather
            # than a new simulator event waking the connection.
            assert next(line for line in lines if line.startswith("event: ")) == "event: access.revoked"
            assert next_event(lines) == {"reason": "access_changed_or_unavailable"}
            assert next_event(lines) is None

        state = driver.get(f"/api/v1/facilities/{FACILITY}/state?run_id={run_id}")
        assert state.status_code == 200, state.text
        assert state.json()["snapshot"]["objects"] == []
        assert state.json()["registered_vehicle_ids"] == []
        vehicles = driver.get(f"/api/v1/me/vehicles?facility_id={FACILITY}")
        assert vehicles.status_code == 200 and vehicles.json()["vehicles"] == []

        # A valid old cursor may replay a historical event, but only after
        # applying the current server-side scope. It cannot restore car B.
        with driver.stream("GET", path, headers={"Last-Event-ID": old_cursor}) as replay:
            assert replay.status_code == 200
            event = next_event(replay.iter_lines())
            assert event["event_id"] == changed["event_id"]
            assert object_ids(event) == set()
