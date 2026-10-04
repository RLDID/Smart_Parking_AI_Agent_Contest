"""Private vehicle views: isolated SQLite, ASGI requests, no providers/ports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend.app import Settings, create_app
from backend.auth import ApiError
from backend.vehicle_views import install_vehicle_view_routes
from contracts.vehicle_views import OwnParkingMap, VehicleLocations

FACILITY = "fac-demo-01"
ORIGIN = "http://testserver"
LOCATIONS = "/api/v1/me/vehicle-locations"


@pytest.fixture
def rig(tmp_path):
    app = create_app(Settings(database=tmp_path / "vehicle-views.sqlite3",
                              test_control=True, origins=(ORIGIN,), background_ticks=False))

    def authenticate(request):
        return app.state.auth.require(request.cookies.get("parking_session"))

    def facility_check(facility_id):
        if facility_id != FACILITY:
            raise ApiError(404, "NOT_FOUND", "시설을 찾을 수 없습니다.")

    # Remains usable after the main chat installs the same routes in app.py.
    if not any(getattr(route, "path", None) == LOCATIONS for route in app.routes):
        install_vehicle_view_routes(app, authenticate, facility_check, None)
    with TestClient(app) as client:
        def login(username):
            response = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                                   json={"username": username, "password": "parking-demo-only"})
            assert response.status_code == 200
            return client.get("/api/v1/me").json()["csrf_token"]

        csrf = login("demo-operator")
        created = client.post("/api/v1/test/runs",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "vehicle-view-create"},
            json={"facility_id": FACILITY, "fixture_ref": "s1a-foundation-v1",
                  "config_ref": "foundation-v1", "seed": 17})
        assert created.status_code == 201
        login("demo-driver")
        runtime = app.state.runtime
        yield SimpleNamespace(client=client, app=app, runtime=runtime,
                              run=created.json()["run_id"], login=login)


def configure(rig, *, end=2400, x=9.5, uncertainty=0, change=None):
    """Independent public measurements: no simulator actors/expected fixtures."""
    start = datetime.now(timezone.utc) - timedelta(milliseconds=end + 2000)
    frames = []
    for index, tick in enumerate(range(0, end + 1, 200), 1):
        stamp = (start + timedelta(milliseconds=tick)).isoformat().replace("+00:00", "Z")
        target = {"object_id": "obj-car-02", "object_type": "vehicle",
                  "position": {"x": x, "y": 26.5}, "size": {"length_m": 4.6, "width_m": 1.8},
                  "heading_deg": 90, "quality": {"visibility": "visible",
                    "uncertainty_m": uncertainty, "missing_fields": []}}
        if change:
            change(target, tick)
        frames.append({"facility_id": FACILITY, "run_id": rig.run, "map_version": "map-01-draft",
                       "observation_id": f"own-measurement-{index}", "state_version": index,
                       "sim_time_ms": tick, "observed_at": stamp, "received_at": stamp,
                       "coverage": "complete", "devices": [], "object_events": [],
                       "objects": [target, {**deepcopy(target), "object_id": "obj-car-01",
                                             "position": {"x": 37, "y": 3}}]})

    async def update():
        async with rig.runtime.lock:
            rig.runtime.world.update(observation=deepcopy(frames[-1]), observation_history=frames,
                sim_time_ms=end, state_version=frames[-1]["state_version"],
                run_status="paused", recovery_required=False)
    rig.client.portal.call(update)
    return frames


def query(rig):
    response = rig.client.get(LOCATIONS, params={"facility_id": FACILITY, "run_id": rig.run})
    assert response.status_code == 200, response.text
    return VehicleLocations.model_validate(response.json())


def alter(rig, operation):
    async def update():
        async with rig.runtime.lock:
            operation(rig.runtime)
    rig.client.portal.call(update)


def test_unique_stationary_containment_is_observation_not_assignment(rig):
    configure(rig)
    view = query(rig)
    assert len(view.vehicles) == 1
    location = view.vehicles[0]
    assert location.registered_vehicle_id == "veh-demo-02"
    assert location.object_id == "obj-car-02"
    assert location.observed_bay_id == "B01"
    assert location.location_status == "observed_bay"
    assert location.stationary_duration_ms == 2400
    assert location.bay_semantics == "observed_not_assigned"
    assert len(location.evidence_observation_ids) >= 11
    assert "obj-car-01" not in view.model_dump_json()
    assert view.state_version == view.applied_state_version
    assert view.settings_version == "sim0-operation-test-v1"
    assert rig.runtime.autonomous.enabled is None
    assert rig.runtime.queries.live_models is None


@pytest.mark.parametrize("x,uncertainty", [(11, 0), (10.15, 0), (10.05, .1)])
def test_crossing_bays_or_tolerated_intrusion_never_confirms(rig, x, uncertainty):
    configure(rig, x=x, uncertainty=uncertainty)
    location = query(rig).vehicles[0]
    assert location.location_status == "observed_position"
    assert location.observed_bay_id is None
    assert location.location_reason == "outside_or_intruding_bay"


@pytest.mark.parametrize("case,reason", [
    ("short", "insufficient_history"), ("moving", "moving"),
    ("uncertain", "uncertain_stationarity"), ("gap", "insufficient_history"),
    ("duplicate", "observation_insufficient"),
])
def test_insufficient_motion_evidence_never_confirms(rig, case, reason):
    configure(rig, end=400 if case == "short" else 2400,
              uncertainty=.06 if case == "uncertain" else 0,
              change=(lambda obj, t: obj["position"].update(x=9.2 if t < 2400 else 9.5))
              if case == "moving" else None)
    if case == "gap":
        alter(rig, lambda r: r.world.update(observation_history=r.world["observation_history"][-2:-1]
                                            + r.world["observation_history"][-1:]))
        # Leave only a sample 1000ms before the current one.
        alter(rig, lambda r: r.world["observation_history"][0].update(sim_time_ms=1400))
    if case == "duplicate":
        alter(rig, lambda r: r.world["observation_history"][0].update(
            observation_id=r.world["observation"]["observation_id"]))
    location = query(rig).vehicles[0]
    assert location.observed_bay_id is None
    assert location.location_reason == reason


@pytest.mark.parametrize("case", ["unavailable", "partial", "occluded", "missing_size",
                                 "missing_target", "stale", "wall_stale", "recovery"])
def test_unreliable_current_measurement_does_not_publish_current_position(rig, case):
    configure(rig)
    def degrade(runtime):
        world = runtime.world
        frame = world["observation"]
        if case in ("unavailable", "partial"):
            frame["coverage"] = case
        elif case == "occluded":
            frame["objects"][0]["quality"]["visibility"] = "occluded"
        elif case == "missing_size":
            frame["objects"][0]["size"] = None
        elif case == "missing_target":
            frame["objects"].pop(0)
        elif case == "stale":
            world["sim_time_ms"] += 1200
        elif case == "wall_stale":
            world["run_status"] = "running"
        elif case == "recovery":
            world["recovery_required"] = True
        world["observation_history"][-1] = deepcopy(frame)
    alter(rig, degrade)
    location = query(rig).vehicles[0]
    assert location.location_status == "unknown"
    assert location.position is None and location.observed_bay_id is None


def test_history_before_ownership_cannot_prove_parking_hold(rig):
    frames = configure(rig)
    # Only the last 1000ms belong to this user, shorter than the 2000ms hold.
    alter(rig, lambda r: r.store.db.execute(
        "UPDATE vehicle_users SET valid_from=? WHERE registered_vehicle_id='veh-demo-02'",
        (frames[-6]["observed_at"],)))
    location = query(rig).vehicles[0]
    assert location.object_id == "obj-car-02"
    assert location.observed_bay_id is None
    assert location.location_reason == "insufficient_history"


def test_new_owner_cannot_read_observation_before_grant(rig):
    configure(rig)
    assigned = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    alter(rig, lambda r: r.store.db.execute(
        "UPDATE vehicle_users SET valid_from=? WHERE registered_vehicle_id='veh-demo-02'", (assigned,)))
    location = query(rig).vehicles[0]
    assert location.registered_vehicle_id == "veh-demo-02"
    assert location.object_id is None and location.quality is None
    assert location.location_reason == "no_verified_relationship"


@pytest.mark.parametrize("change", ["ended", "uncertain", "future", "inactive", "unlink"])
def test_current_permission_loss_hides_object_and_location(rig, change):
    configure(rig)
    def update(runtime):
        db = runtime.store.db
        if change == "ended":
            db.execute("UPDATE object_mappings SET valid_to_sim_time_ms=2000 WHERE registered_vehicle_id='veh-demo-02'")
        elif change == "uncertain":
            db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE registered_vehicle_id='veh-demo-02'")
        elif change == "future":
            db.execute("UPDATE object_mappings SET valid_from_sim_time_ms=2600 WHERE registered_vehicle_id='veh-demo-02'")
        elif change == "inactive":
            db.execute("UPDATE vehicles SET active=0 WHERE registered_vehicle_id='veh-demo-02'")
        else:
            db.execute("UPDATE vehicle_users SET valid_until='2000-01-01T00:00:00Z' WHERE registered_vehicle_id='veh-demo-02'")
    alter(rig, update)
    locations = query(rig).vehicles
    if change in ("inactive", "unlink"):
        assert locations == []
    else:
        assert locations[0].object_id is None
        assert locations[0].position is None


def test_owner_and_other_driver_get_only_own_registered_vehicles(rig):
    configure(rig)
    rig.login("demo-owner")
    assert query(rig).vehicles == []
    rig.login("demo-driver-2")
    locations = query(rig).vehicles
    assert {v.registered_vehicle_id for v in locations} == {"veh-demo-01"}
    assert "obj-car-02" not in locations[0].model_dump_json()


def test_map_is_static_sanitized_and_existing_driver_map_remains_forbidden(rig):
    configure(rig)
    response = rig.client.get("/api/v1/me/parking-map", params={"facility_id": FACILITY})
    assert response.status_code == 200
    view = OwnParkingMap.model_validate(response.json())
    assert view.view_scope == "static_geometry"
    assert {bay.zone_id for bay in view.parking_bays} == {f"B{i:02}" for i in range(1, 7)}
    assert set(response.json()) == {"view_version", "view_scope", "facility_id", "map_version",
                                   "coordinate_system", "bounds", "zones", "parking_bays"}
    assert all(set(zone) == {"zone_id", "type", "polygon"} for zone in response.json()["zones"])
    assert not any(word in response.text for word in ("object_id", "gate-in-01", "seed", "incident_id", "route_policy"))
    assert rig.client.get(f"/api/v1/facilities/{FACILITY}/map").status_code == 403
    assert rig.client.get("/api/v1/me/parking-map", params={"facility_id": FACILITY,
                           "map_version": "missing"}).status_code == 404
    assert rig.client.get("/api/v1/me/parking-map", params={"facility_id": "other"}).status_code == 404
    assert rig.client.get(LOCATIONS, params={"facility_id": FACILITY, "run_id": "old-run"}).status_code == 404
    assert rig.client.get(LOCATIONS, params={"facility_id": FACILITY}).status_code == 422


def test_revoked_session_and_storage_failure(rig):
    configure(rig)
    alter(rig, lambda r: setattr(r, "failure", "test-storage-failure"))
    assert rig.client.get(LOCATIONS, params={"facility_id": FACILITY, "run_id": rig.run}).status_code == 503
    alter(rig, lambda r: r.store.db.execute(
        "UPDATE memberships SET revoked_at='2026-01-01T00:00:00Z' WHERE user_id='demo-driver'"))
    assert rig.client.get(LOCATIONS, params={"facility_id": FACILITY, "run_id": rig.run}).status_code == 401
    assert rig.client.get("/api/v1/me/parking-map", params={"facility_id": FACILITY}).status_code == 401


def test_scope_change_during_projection_rejects_response(rig, monkeypatch):
    configure(rig)
    registry = rig.runtime.store.registry
    original = registry.scope_stamp
    calls = []
    def changed(username):
        calls.append(username)
        return original(username) + ("changed" if len(calls) > 1 else "")
    monkeypatch.setattr(registry, "scope_stamp", changed)
    response = rig.client.get(LOCATIONS, params={"facility_id": FACILITY, "run_id": rig.run})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCESS_CHANGED"
