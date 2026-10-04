import json
import sqlite3

import httpx
import pytest

from backend.runtime import Runtime
from backend.storage import Store
from contracts.models import DriverStateView
from simulator.world import FACILITY, initial_world, public_state
from test_foundation import client, login, create_run, control, RUN
from test_http_stream import server, next_event


def objects(state):
    return {o["object_id"] for o in state["snapshot"]["objects"]}


@pytest.mark.parametrize("username,vehicle,obj", [
    ("demo-driver", "veh-demo-02", "obj-car-02"),
    ("demo-driver-2", "veh-demo-01", "obj-car-01"),
])
def test_driver_http_scope_and_no_privilege_bypass(client, username, vehicle, obj):
    run = create_run(client, login(client))
    headers = login(client, username)
    vehicles = client.get(f"/api/v1/me/vehicles?facility_id={FACILITY}").json()["vehicles"]
    assert [v["registered_vehicle_id"] for v in vehicles] == [vehicle]
    state = client.get(f"/api/v1/facilities/{FACILITY}/state?run_id={run['run_id']}").json()
    assert DriverStateView.model_validate(state)
    assert objects(state) == {obj} and state["snapshot"]["devices"] == []
    assert state["registered_vehicle_ids"] == [vehicle]
    for suffix in ("map", f"spatial-analysis?run_id={run['run_id']}"):
        assert client.get(f"/api/v1/facilities/{FACILITY}/{suffix}").status_code == 403
    assert control(client, headers, run["run_id"], "step").status_code == 403
    assert client.get("/api/v1/me/vehicles?facility_id=other").status_code == 404


def test_owner_still_has_full_state_and_database_revocation_is_immediate(client):
    run = create_run(client, login(client))
    login(client, "demo-owner")
    path = f"/api/v1/facilities/{FACILITY}/state?run_id={run['run_id']}"
    assert len(objects(client.get(path).json())) == 3
    assert "view_scope" not in client.get(path).json()
    def revoke():
        with client.app.state.runtime.store.db as db:
            db.execute("UPDATE memberships SET revoked_at='2026-09-30T00:00:00Z' WHERE user_id='demo-owner'")
    client.portal.call(revoke)
    assert client.get(path).status_code == 401
    response = client.post("/api/v1/auth/session", headers={"Origin": "http://testserver"},
                           json={"username": "demo-owner", "password": "parking-demo-only"})
    assert response.status_code == 401


def test_migration_preserves_v1_rows_cursors_requests_and_never_reseeds(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    world = initial_world(9)
    event = Runtime.event(world)
    event["event_id"] = "evt-42"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE runs(run_id TEXT PRIMARY KEY,world_json TEXT NOT NULL);
            CREATE TABLE current_run(singleton INTEGER PRIMARY KEY,run_id TEXT REFERENCES runs);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT REFERENCES runs,payload TEXT NOT NULL);
            CREATE TABLE requests(requester TEXT,key TEXT,argument_hash TEXT,response TEXT,PRIMARY KEY(requester,key));
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO runs VALUES (?,?)", (world["run_id"], json.dumps(world)))
        db.execute("INSERT INTO current_run VALUES (1,?)", (world["run_id"],))
        db.execute("INSERT INTO events VALUES (42,?,?)", (world["run_id"], json.dumps(event)))
        db.execute("INSERT INTO requests VALUES ('demo-operator','old','hash','{\"old\":true}')")
    store = Store(path)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert store.load() == world and store.events() == [(42, event)]
        assert tuple(store.previous_request("demo-operator", "old")) == ("hash", '{"old":true}')
        assert len(store.registry.vehicles("demo-driver")) == 1
        with store.db:
            store.db.execute("UPDATE memberships SET revoked_at='2026-09-30T00:00:00Z' WHERE user_id='demo-driver'")
            store.db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'")
        store.commit(world, Runtime.event(world))
        assert store.events()[-1][0] == 43
    finally:
        store.close()
    store = Store(path)
    try:
        assert store.registry.role("demo-driver") is None
        assert store.db.execute("SELECT count(*) FROM users").fetchone()[0] == 4
        assert store.db.execute("SELECT count(*) FROM object_mappings").fetchone()[0] == 2
        assert store.db.execute("SELECT mapping_status FROM object_mappings WHERE object_id='obj-car-02'").fetchone()[0] == "uncertain"
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path/"registry.sqlite3")
    world = initial_world(1)
    store.commit(world, Runtime.event(world))
    try:
        yield store, world
    finally:
        store.close()


@pytest.mark.parametrize("change", [
    "UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'",
    "UPDATE object_mappings SET valid_to_sim_time_ms=100 WHERE object_id='obj-car-02'",
    "UPDATE object_mappings SET valid_from_sim_time_ms=100 WHERE object_id='obj-car-02'",
    "UPDATE vehicle_users SET valid_until='2020-01-01T00:00:00Z' WHERE user_id='demo-driver'",
    "UPDATE vehicle_users SET valid_from='2099-01-01T00:00:00Z' WHERE user_id='demo-driver'",
    "UPDATE vehicles SET active=0 WHERE registered_vehicle_id='veh-demo-02'",
])
def test_projection_requires_current_and_historical_grants(registry, change):
    store, world = registry
    with store.db:
        store.db.execute(change)
    projected = store.registry.project("demo-driver", public_state(world), current_sim=200)
    assert objects(projected) == set()
    assert len(objects(public_state(world))) == 3  # Stored event was not mutated.


def test_new_owner_does_not_receive_before_assignment_history_or_other_exit(registry):
    store, world = registry
    snapshot = public_state(world)
    snapshot["snapshot"]["observed_at"] = "2020-01-01T00:00:00Z"
    with store.db:
        store.db.execute("UPDATE vehicle_users SET valid_from='2021-01-01T00:00:00Z' WHERE user_id='demo-driver'")
    assert objects(store.registry.project("demo-driver", snapshot, 0)) == set()
    current = public_state(world)
    current["snapshot"]["object_events"] = [
        {"object_id": obj, "sim_time_ms": 0, "observed_at": world["observation"]["observed_at"]}
        for obj in ("obj-car-01", "obj-car-02", "obj-ped-01")]
    projected = store.registry.project("demo-driver", current, 0)
    assert [e["object_id"] for e in projected["snapshot"]["object_events"]] == ["obj-car-02"]


def test_mapping_fks_overlap_and_transaction_rollback(registry):
    store, world = registry
    row = list(store.db.execute("SELECT * FROM object_mappings WHERE object_id='obj-car-02'").fetchone())
    for index, value in ((0, "duplicate-range"), (1, "other-facility"), (2, "other-run"), (4, "missing-vehicle")):
        invalid = row.copy()
        invalid[0] = "new-id"
        invalid[index] = value
        with pytest.raises(sqlite3.IntegrityError), store.db:
            store.db.execute("INSERT INTO object_mappings VALUES (?,?,?,?,?,?,?,?,?)", invalid)
    with store.db:
        store.db.execute("UPDATE object_mappings SET valid_to_sim_time_ms=100 WHERE object_id='obj-car-02'")
        adjacent = row.copy()
        adjacent[0], adjacent[7] = "adjacent", 100
        store.db.execute("INSERT INTO object_mappings VALUES (?,?,?,?,?,?,?,?,?)", adjacent)
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE object_mappings SET valid_from_sim_time_ms=99 WHERE mapping_id='adjacent'")
    with store.db:
        store.db.execute("CREATE TRIGGER fail_mapping BEFORE INSERT ON object_mappings BEGIN SELECT RAISE(ABORT,'test'); END")
    new = initial_world(2)
    with pytest.raises(sqlite3.IntegrityError):
        store.commit(new, Runtime.event(new), ("demo-operator", "failed", "hash", "{}"))
    assert store.load()["run_id"] == world["run_id"]
    assert not store.previous_request("demo-operator", "failed")
    assert store.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_real_driver_sse_replay_and_paused_vehicle_revocation(tmp_path):
    with server(tmp_path) as address, httpx.Client(base_url=address, timeout=5) as operator, \
            httpx.Client(base_url=address, timeout=5) as driver:
        def sign_in(client, username):
            assert client.post("/api/v1/auth/session", headers={"Origin": address},
                               json={"username": username, "password": "parking-demo-only"}).status_code == 200
            return {"Origin": address, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}
        headers = sign_in(operator, "demo-operator")
        created = operator.post("/api/v1/test/runs", json=RUN,
                                headers={**headers, "Idempotency-Key": "create"}).json()
        run_id = created["run_id"]
        sign_in(driver, "demo-driver")
        path = f"/api/v1/facilities/{FACILITY}/events?run_id={run_id}"
        with driver.stream("GET", path) as stream:
            lines = stream.iter_lines()
            first = next_event(lines)
            assert objects(first["payload"]) == {"obj-car-02"}
            assert operator.post(f"/api/v1/test/runs/{run_id}/control", json={"action": "step"},
                                 headers={**headers, "Idempotency-Key": "step"}).status_code == 200
            change = next_event(lines)
            assert objects(change["payload"]) == {"obj-car-02"}
        with driver.stream("GET", path, headers={"Last-Event-ID": first["event_id"]}) as stream:
            assert objects(next_event(stream.iter_lines())["payload"]) == {"obj-car-02"}
        with driver.stream("GET", path, headers={"Last-Event-ID": "expired"}) as stream:
            lines = stream.iter_lines()
            assert next_event(lines)["type"] == "reset_required"
            assert objects(next_event(lines)["payload"]) == {"obj-car-02"}
            # External admin database change while paused: no simulator event to
            # wake the stream. Polling must still revoke and close it.
            with sqlite3.connect(tmp_path/"network.sqlite3") as db:
                db.execute("UPDATE vehicle_users SET valid_until='2020-01-01T00:00:00Z' WHERE user_id='demo-driver'")
            assert next_event(lines) == {"reason": "access_changed_or_unavailable"}
            assert next_event(lines) is None
        state = driver.get(f"/api/v1/facilities/{FACILITY}/state?run_id={run_id}").json()
        assert objects(state) == set() and state["registered_vehicle_ids"] == []
