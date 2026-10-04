"""Current evidence for live recommendations, without identities or future state."""
import asyncio
import json

import pytest

from backend.auth import Auth
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from simulator.world import advance, initial_world


@pytest.mark.parametrize("scenario,fixture", [
    ("s1a", "s1a-foundation-v1"),
    ("s1b", "s1b-blocked-v1"),
    ("s1c", "s1c-overlap-v1"),
])
def test_contact_evidence_is_current_minimal_and_read_only(tmp_path, scenario, fixture):
    runtime = Runtime(tmp_path / "context.sqlite3")
    try:
        auth = Auth(runtime.store)
        _, operator = auth.login("demo-operator", "parking-demo-only", "test")
        runtime.world = initial_world(42, fixture)
        for _ in range(60):
            advance(runtime.world)
        runtime.store.commit(runtime.world, Runtime.event(runtime.world))
        run = runtime.world["run_id"]
        body = AutonomousControl(run_id=run, action="process", scenario=scenario)

        def snapshot():
            return runtime.autonomous._snapshot(operator, body, runtime.read_task(operator, run))

        context = snapshot()
        assert context["recipient_check"] == {"object_id": "obj-car-02", "mapping_status": "verified"}
        assert "demo-driver" not in json.dumps(context)
        assert context["analysis"]["support_status"] == "supported"
        runtime.store.db.execute("UPDATE vehicle_users SET valid_until='2000-01-01T00:00:00Z'")
        runtime.store.db.commit()
        assert snapshot()["recipient_check"]["mapping_status"] == "unverified"
        for table in ("incidents", "plans", "notifications", "executions"):
            assert runtime.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        runtime.store.close()


@pytest.mark.parametrize("scenario,fixture,impact", [
    ("s1b", "s1b-blocked-v1", "exit_blocked"),
    ("s1c", "s1c-overlap-v1", "bay_intrusion"),
])
def test_existing_incident_keeps_latest_spatial_analysis(tmp_path, scenario, fixture, impact):
    async def exercise():
        runtime = Runtime(tmp_path / "followup.sqlite3")
        try:
            auth = Auth(runtime.store)
            token, operator = auth.login("demo-operator", "parking-demo-only", "test")
            runtime.world = initial_world(42, fixture)
            for _ in range(60):
                advance(runtime.world)
            runtime.store.commit(runtime.world, Runtime.event(runtime.world))
            run = runtime.world["run_id"]
            body = AutonomousControl(run_id=run, action="process", scenario=scenario)
            result = await runtime.autonomous.control(operator, body, "first", lambda: auth.require(token))
            assert result["status"] == "accepted"
            context = runtime.autonomous._snapshot(operator, body, runtime.read_task(operator, run))
            assert context["incident"]["incident_id"] == result["incident_id"]
            latest = runtime.business.impact_assessment(impact, "obj-car-02", "B01")
            # Wall-clock age changes between reads; compare observation evidence.
            for key in ("support_status", "observation_ids", "violation_candidate", "clearance_sustained"):
                assert context["analysis"][key] == latest[key]
            assert context["analysis"]["violation_candidate"]
            assert not context["analysis"]["clearance_sustained"]
        finally:
            runtime.store.close()
    asyncio.run(exercise())
