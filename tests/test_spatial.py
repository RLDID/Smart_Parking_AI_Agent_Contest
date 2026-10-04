"""Independent geometry/clock expectations, plus the actual HTTP and checkpoint path."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import pytest

from backend.app import Settings, create_app
from backend.auth import Session
from backend.runtime import Runtime
from simulator.spatial import POLICY, analyze_spatial_context
from simulator.world import (MAP, advance, digest, initial_world, publish_observation,
                             set_observation_mode)

EPOCH = datetime(2026, 9, 30, tzinfo=timezone.utc)


def frame(t, *, x=4, y=20, heading=90, objects=True):
    at = (EPOCH + timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z")
    return {"facility_id": "fac-demo-01", "run_id": "run-unit", "observation_id": f"obs-{t}",
            "map_version": "map-01-draft", "state_version": t//100, "sim_time_ms": t,
            "observed_at": at, "received_at": at, "coverage": "complete", "devices": [],
            "objects": [{"object_id": "car-test", "object_type": "vehicle", "position": {"x": x, "y": y},
                         "size": {"length_m": 4.6, "width_m": 1.8}, "heading_deg": heading,
                         "quality": {"visibility": "visible", "uncertainty_m": 0, "missing_fields": []}}] if objects else []}


def analyze(history, **kwargs):
    latest = history[-1]
    arguments = dict(current_sim_time_ms=latest["sim_time_ms"], run_status="paused",
                     now=EPOCH+timedelta(milliseconds=latest["sim_time_ms"]))
    arguments.update(kwargs)
    return analyze_spatial_context(MAP, history, **arguments)


def test_full_body_blocks_even_when_center_is_outside_and_allows_real_gap():
    blocked = analyze([frame(0, y=22.6)])  # Center outside; body reaches y20.3.
    assert blocked.metrics.available_clearance_m == pytest.approx(2.3)
    assert blocked.metrics.passage == "blocked"
    clear = analyze([frame(0, y=21.9, heading=0)])
    assert clear.metrics.passage == "clear"
    assert clear.metrics.available_clearance_m == pytest.approx(3)
    assert clear.metrics.occupied_object_ids == ["car-test"]


@pytest.mark.parametrize("free,expected", [(2.39, "blocked"), (2.4, "blocked"), (2.41, "clear")])
def test_positive_side_clearance_boundary(free, expected):
    result = analyze([frame(0, y=18+free+.9, heading=0)])
    assert result.metrics.required_clearance_m == pytest.approx(2.4)
    assert result.metrics.passage == expected


def test_union_of_multiple_obstacles_is_not_a_centerpoint_test():
    obs = frame(0, y=18, heading=0)
    second = deepcopy(obs["objects"][0])
    second["object_id"] = "other"
    second["position"]["y"] = 22
    obs["objects"].append(second)
    result = analyze([obs])
    assert result.metrics.available_clearance_m == pytest.approx(2.2)
    assert result.metrics.passage == "blocked"


def test_full_body_exit_sweep_includes_central_approach():
    obs = frame(0, x=11)
    assert analyze([obs]).metrics.passage == "blocked"
    # A car beyond the entire candidate body's sweep is not an obstacle to it.
    assert analyze([frame(0, x=16)]).metrics.passage == "clear"


def test_stationary_4800_5000_and_slow_cumulative_motion():
    history = [frame(t) for t in range(0, 5001, 200)]
    assert not analyze(history[:-1]).metrics.objects[0].stationary_candidate
    result = analyze(history)
    assert result.metrics.objects[0].stop_duration_ms == 5000
    assert result.metrics.objects[0].stationary_candidate
    assert result.metrics.blocked_duration_ms == 5000
    creeping = [frame(t, y=20+t/10000) for t in range(0, 5001, 200)]
    assert analyze(creeping).metrics.objects[0].stop_duration_ms <= 1000
    assert not analyze(creeping).metrics.objects[0].stationary_candidate


@pytest.mark.parametrize("fault", ["partial", "unavailable", "occluded", "missing_geometry", "uncertain", "duplicate"])
def test_uncertain_latest_never_reports_clearance_or_stop(fault):
    history = [frame(t) for t in range(0, 5001, 200)]
    obs = history[-1]
    if fault in ("partial", "unavailable"):
        obs.update(coverage=fault, objects=[])
    elif fault == "occluded":
        obs["objects"][0]["quality"]["visibility"] = "occluded"
    elif fault == "missing_geometry":
        obs["objects"][0]["position"] = None
    elif fault == "uncertain":
        obs["objects"][0]["quality"]["uncertainty_m"] = .2
    else:
        obs["objects"].append(deepcopy(obs["objects"][0]))
    result = analyze(history)
    assert result.support_status == "insufficient_data"
    assert result.metrics.passage == "unknown"
    assert result.metrics.clearance_sustained is None
    assert result.metrics.objects == []


def test_clearance_hold_boundary_and_missing_sample_restart():
    history = [frame(0)] + [frame(t, objects=False) for t in range(200, 3400, 200)]
    assert analyze(history[:-1]).metrics.clear_duration_ms == 2800
    assert not analyze(history[:-1]).metrics.clearance_sustained
    assert analyze(history).metrics.clearance_sustained
    history[8]["coverage"] = "partial"
    assert analyze(history).metrics.clear_duration_ms == 1400
    assert not analyze(history).metrics.clearance_sustained
    history[8]["coverage"] = "complete"
    del history[8]  # A missing 200ms sample breaks continuity, even if both endpoints are clear.
    assert analyze(history).metrics.clear_duration_ms == 1400
    history[-1] = frame(3200)
    assert analyze(history).metrics.clear_duration_ms == 0


def test_old_observations_sim_and_live_wall_age_but_pause_does_not_accumulate():
    history = [frame(t) for t in range(0, 3200, 200)]
    assert analyze(history, current_sim_time_ms=4000).support_status == "supported"
    assert analyze(history, current_sim_time_ms=4001).quality.freshness == "stale"
    assert analyze(history, run_status="running", now=EPOCH+timedelta(milliseconds=4001)).quality.freshness == "stale"
    paused = analyze(history, now=EPOCH+timedelta(days=1))
    assert paused.support_status == "supported"
    assert paused.metrics.objects[0].stop_duration_ms == 3000
    assert not paused.metrics.objects[0].stationary_candidate
    assert analyze(history, recovery_required=True).support_status == "insufficient_data"


@pytest.mark.parametrize("defect", ["order", "scope", "map", "future", "conflicting_id", "old_duplicate"])
def test_invalid_history_does_not_synthesize_temporal_features(defect):
    history = [frame(t) for t in range(0, 1000, 200)]
    if defect == "order":
        history[2], history[3] = history[3], history[2]
    elif defect == "scope":
        history[2]["run_id"] = "other-run"
    elif defect == "map":
        history[2]["map_version"] = "other-map"
    elif defect == "future":
        history.insert(1, frame(2000))
    elif defect == "old_duplicate":
        history.append(deepcopy(history[0]))
    else:
        duplicate = deepcopy(history[-1])
        duplicate["objects"][0]["position"]["x"] = 8
        history.append(duplicate)
    assert analyze(history).support_status == "insufficient_data"


def test_retransmitted_snapshot_does_not_extend_time_and_unsupported_zone_is_explicit():
    single = frame(0)
    result = analyze([single, deepcopy(single)])
    assert result.observation_ids == ["obs-0"]
    assert result.metrics.objects[0].stop_duration_ms == 0
    assert analyze([single], zone_id="aisle-central").support_status == "unsupported_geometry"
    other_map = deepcopy(MAP)
    other_map["zones"][0]["polygon"][0]["y"] = 17
    assert analyze_spatial_context(other_map, [single], current_sim_time_ms=0,
                                   run_status="paused", now=EPOCH).support_status == "unsupported_geometry"


@pytest.mark.parametrize("mode", ["missing_vehicle", "occluded_vehicle", "unavailable", "delayed"])
def test_observation_fault_does_not_move_or_delete_internal_vehicle(mode):
    world = initial_world(1)
    before = deepcopy(world["actors"])
    set_observation_mode(world, mode)
    for _ in range(14):
        advance(world)
    assert world["actors"] == before
    snapshot = world["observation"]
    result = analyze_spatial_context(MAP, world["observation_history"],
                                     current_sim_time_ms=world["sim_time_ms"], run_status="paused")
    assert result.support_status == "insufficient_data"
    assert result.metrics.clearance_sustained is None
    assert snapshot["object_events"] == []
    if mode == "delayed":
        assert snapshot["sim_time_ms"] == 200
        assert world["sim_time_ms"]-snapshot["sim_time_ms"] == 1200
        assert len(world["observation_queue"]) == 6
    elif mode == "occluded_vehicle":
        obj = next(o for o in snapshot["objects"] if o["object_id"] == "obj-car-02")
        assert obj["position"] is None and obj["quality"]["visibility"] == "occluded"
    set_observation_mode(world, "normal")
    advance(world); advance(world)
    fresh = analyze_spatial_context(MAP, world["observation_history"],
                                    current_sim_time_ms=world["sim_time_ms"], run_status="paused")
    assert fresh.support_status == "supported"
    assert all(obj.stop_duration_ms == 0 for obj in fresh.metrics.objects)


def test_history_is_bounded_and_late_delivery_cannot_overwrite_current():
    world = initial_world(1)
    first = deepcopy(world["observation"])
    for _ in range(160):
        advance(world)
    assert len(world["observation_history"]) == POLICY["history_limit"]
    latest = deepcopy(world["observation"])
    publish_observation(world, first)
    assert world["observation"] == latest


def test_initial_observed_blockage_then_continuous_space_recovery():
    world = initial_world(1)
    for _ in range(50):
        advance(world)
    result = analyze_spatial_context(MAP, world["observation_history"], current_sim_time_ms=5000, run_status="paused")
    assert result.metrics.passage == "blocked"
    assert next(o for o in result.metrics.objects if o.object_id == "obj-car-02").stationary_candidate
    assert all(not o.stationary_candidate for o in result.metrics.objects if o.object_id != "obj-car-02")
    world["move_requested"] = True
    results = []
    for _ in range(70):
        advance(world)
        if world["sim_time_ms"] % 200 == 0:
            results.append(analyze_spatial_context(MAP, world["observation_history"],
                           current_sim_time_ms=world["sim_time_ms"], run_status="paused"))
    first_clear = next(r for r in results if r.metrics.passage == "clear")
    assert first_clear.metrics.clear_duration_ms == 0
    first_held = next(r for r in results if r.metrics.clearance_sustained)
    assert first_held.evaluated_at_sim_time_ms-first_clear.evaluated_at_sim_time_ms == 3000
    encoded = json.dumps(first_held.model_dump())
    for forbidden in ("actor_id", "move_requested", "pending_events", "scenario", "incident_id", "resolved"):
        assert forbidden not in encoded


def test_restart_preserves_world_and_requests_but_resets_observation_holds(tmp_path):
    async def exercise():
        path = tmp_path / "upgrade.sqlite3"
        runtime = Runtime(path)
        world = initial_world(1)
        for _ in range(50):
            advance(world)
        for key in ("observation_history", "observation_queue", "observation_mode", "observation_policy_digest"):
            world.pop(key, None)  # Actual v1 foundation checkpoint shape.
        runtime.store.commit(world, runtime.event(world), ("demo-operator", "old-key", "old-hash", "{}"))
        runtime.store.close()
        restored = Runtime(path)
        try:
            assert restored.world["sim_time_ms"] == 5000 and restored.world["recovery_required"]
            assert restored.world["observation_history"] == []
            assert restored.store.previous_request("demo-operator", "old-key")[0] == "old-hash"
            session = Session("demo-operator", "test_operator", "unused", 999999999)
            for key in ("one", "two"):
                await restored.mutate(session, key, "control", {"action": "step"}, restored.world["run_id"])
            result = analyze_spatial_context(MAP, restored.world["observation_history"],
                          current_sim_time_ms=5200, run_status="paused")
            assert all(item.stop_duration_ms == 0 for item in result.metrics.objects)
        finally:
            restored.store.close()
    asyncio.run(exercise())


def test_spatial_http_authorization_fault_idempotency_and_previous_request_hash(tmp_path):
    origin = "http://testserver"
    app = create_app(Settings(database=tmp_path/"http.sqlite3", test_control=True,
                              origins=(origin,), background_ticks=False))
    with TestClient(app) as client:
        path = "/api/v1/facilities/fac-demo-01/spatial-analysis"
        assert client.get(path, params={"run_id": "none"}).status_code == 401
        def login(name):
            assert client.post("/api/v1/auth/session", headers={"Origin": origin},
                               json={"username": name, "password": "parking-demo-only"}).status_code == 200
            return {"Origin": origin, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}
        headers = login("demo-operator")
        created = client.post("/api/v1/test/runs", headers={**headers, "Idempotency-Key": "create"},
                              json={"facility_id": "fac-demo-01", "fixture_ref": "s1a-foundation-v1",
                                    "config_ref": "foundation-v1", "seed": 1}).json()
        run_id = created["run_id"]
        control = f"/api/v1/test/runs/{run_id}/control"
        assert client.get(path, params={"run_id": run_id}).json()["metrics"]["passage"] == "blocked"
        args = {"action": "step", "action_params": {"request_vehicle_move": "obj-car-02"}}
        # Persist a legacy normalized hash; the upgraded API must return its response.
        def legacy_request():
            runtime = app.state.runtime
            value = digest({"operation": "control", "run_id": run_id, "args": args})
            runtime.store.commit(runtime.world, None, ("demo-operator", "legacy", value, json.dumps(created)))
        client.portal.call(legacy_request)
        assert client.post(control, json=args, headers={**headers, "Idempotency-Key": "legacy"}).json() == created
        body = {"action": "step", "action_params": {"observation_mode": "missing_vehicle"}}
        mutation = {**headers, "Idempotency-Key": "fault"}
        first = client.post(control, json=body, headers=mutation)
        assert first.status_code == 200
        assert client.post(control, json=body, headers=mutation).json() == first.json()
        assert client.post(control, json={"action": "step"}, headers={**headers, "Idempotency-Key": "next"}).status_code == 200
        assert client.get(path, params={"run_id": run_id}).json()["support_status"] == "insufficient_data"
        assert client.get(path, params={"run_id": "foreign"}).status_code == 404
        assert client.get(path.replace("fac-demo-01", "foreign"), params={"run_id": run_id}).status_code == 404
        assert client.post(control, json=body, headers={"Origin": origin, "Idempotency-Key": "csrf"}).status_code == 403
        owner = login("demo-owner")
        assert client.get(path, params={"run_id": run_id}).status_code == 200
        assert client.post(control, json=body, headers={**owner, "Idempotency-Key": "owner"}).status_code == 403
        login("demo-driver")
        assert client.get(path, params={"run_id": run_id}).status_code == 403


def test_restarted_http_waits_for_first_new_observation_after_operator_start(tmp_path):
    path = tmp_path/"boundary.sqlite3"
    runtime = Runtime(path)
    world = initial_world(1)
    runtime.store.commit(world, runtime.event(world))
    runtime.store.close()
    origin = "http://testserver"
    app = create_app(Settings(database=path, test_control=True, origins=(origin,), background_ticks=False))
    with TestClient(app) as client:
        client.post("/api/v1/auth/session", headers={"Origin": origin},
                    json={"username": "demo-operator", "password": "parking-demo-only"})
        headers = {"Origin": origin, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}
        url = f"/api/v1/test/runs/{world['run_id']}/control"
        analysis_url = f"/api/v1/facilities/fac-demo-01/spatial-analysis?run_id={world['run_id']}"
        for index, action in enumerate(["start", "pause", "step", "step"]):
            assert client.post(url, json={"action": action}, headers={**headers, "Idempotency-Key": str(index)}).status_code == 200
            result = client.get(analysis_url).json()
            assert result["support_status"] == ("supported" if index == 3 else "insufficient_data")
            if index < 3:
                assert "awaiting_post_boundary_observation" in result["quality"]["reasons"]
