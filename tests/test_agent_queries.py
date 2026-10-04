"""Real read tools behind the mock-only HTTP/job boundary; synthetic DBs."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from agent.loop import MockReadAdapter
from backend.app import Settings, create_app
from backend.auth import ApiError
from contracts.agent_loop import AgentQuery
from test_business import rig
from test_business_api import api_rig, login, ORIGIN


def query(client, headers, run, goal="current_state", key="query", **fields):
    return client.post("/api/v1/test/agent/queries", json={"run_id": run, "goal": goal, **fields},
        headers=headers | {"Idempotency-Key": key})


def test_owner_read_loop_uses_actual_tools_and_cannot_change_world(api_rig):
    headers = login(api_rig.client, "demo-owner")
    before = deepcopy(api_rig.runtime.world)
    result = query(api_rig.client, headers, api_rig.run)
    assert result.status_code == 200
    body = result.json()
    assert body["mode"] == "mock" and body["cost_actual_usd"] == 0
    assert body["status"] == "completed" and body["model_calls"] == 2
    assert {item["name"] for item in body["tool_results"]} == {"get_parking_state", "analyze_spatial_context"}
    assert query(api_rig.client, headers, api_rig.run).json() == body
    assert api_rig.runtime.world == before
    for table in ("incidents", "plans", "notifications", "executions"):
        assert api_rig.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_driver_actual_projection_and_documents_are_only_own_scope(api_rig):
    headers = login(api_rig.client, "demo-driver")
    result = query(api_rig.client, headers, api_rig.run, "my_vehicle").json()
    state = next(item["result"] for item in result["tool_results"] if item["name"] == "get_parking_state")
    assert state["view_scope"] == "own_vehicles"
    assert [obj["object_id"] for obj in state["snapshot"]["objects"]] == ["obj-car-02"]
    regulated = query(api_rig.client, headers, api_rig.run, "regulation", "rules", query="이동 요청 응답").json()
    for item in regulated["tool_results"]:
        if item["name"] == "search_operating_knowledge":
            assert item["result"]["status"] == "matched" and item["result"]["references"]
            assert all(ref["document_id"] == "manual-user-guidance" for ref in item["result"]["references"])
    assert "manual-parking-order" not in str(regulated)


def test_http_authority_csrf_idempotency_and_dev_flag(api_rig, tmp_path):
    client = api_rig.client
    assert query(client, {}, api_rig.run).status_code == 401
    headers = login(client, "demo-owner")
    assert query(client, {"Origin": ORIGIN}, api_rig.run).status_code == 403
    assert query(client, headers, api_rig.run, role="test_operator").status_code == 422
    assert query(client, headers, api_rig.run).status_code == 200
    assert query(client, headers, api_rig.run, "my_vehicle").status_code == 409
    assert query(client, headers, api_rig.run, "regulation", "blank", query="   ").status_code == 422
    app = create_app(Settings(database=tmp_path / "off.sqlite3", origins=(ORIGIN,), background_ticks=False))
    with TestClient(app) as off:
        assert query(off, login(off, "demo-owner"), "run-none").json()["error"]["code"] == "TEST_CONTROL_DISABLED"


def test_withdrawn_citation_or_changed_scope_cannot_replay_private_answer(api_rig):
    headers = login(api_rig.client, "demo-owner")
    first = query(api_rig.client, headers, api_rig.run, "regulation", "old-rules", query="통로 차단 이동 요청").json()
    found = first["tool_results"][0]["result"]
    assert found["status"] == "matched" and found["references"]
    async def withdraw():
        api_rig.runtime.knowledge.document_access("fac-demo-01", "manual-parking-order", "v1", status="withdrawn")
    api_rig.client.portal.call(withdraw)
    repeated = query(api_rig.client, headers, api_rig.run, "regulation", "old-rules", query="통로 차단 이동 요청")
    assert repeated.status_code == 409 and "references" not in str(repeated.json())


def test_wall_expired_citation_cannot_replay_without_any_document_update(api_rig):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [now.isoformat().replace("+00:00", "Z")]
    async def expires_later():
        api_rig.runtime.knowledge.clock = lambda: clock[0]
        api_rig.runtime.knowledge.document_access("fac-demo-01", "manual-parking-order", "v1",
            retired_at=(now + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"))
    api_rig.client.portal.call(expires_later)
    headers = login(api_rig.client, "demo-owner")
    first = query(api_rig.client, headers, api_rig.run, "regulation", "expiry", query="통로 차단 이동 요청")
    assert first.status_code == 200 and first.json()["tool_results"][0]["result"]["references"]
    # Time alone changes eligibility; the database row/stamp is unchanged.
    clock[0] = (now + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    repeated = query(api_rig.client, headers, api_rig.run, "regulation", "expiry", query="통로 차단 이동 요청")
    assert repeated.status_code == 409 and "references" not in str(repeated.json())


def test_model_wait_keeps_ticks_and_changed_world_rejects_late_result(rig):
    async def check():
        r = rig.runtime
        started = asyncio.Event()
        class Delayed(MockReadAdapter):
            async def next_turn(self, model_input):
                started.set()
                await asyncio.sleep(.35)
                return await super().next_turn(model_input)
        r.queries.adapter_factory = Delayed
        r.world["run_status"] = "running"
        initial = r.world["sim_time_ms"]
        tick = asyncio.create_task(r.loop())
        try:
            pending = asyncio.create_task(r.queries.execute(rig.sessions["demo-operator"],
                AgentQuery(run_id=rig.run, goal="current_state"), "tick-wait", lambda: None))
            await started.wait()
            with pytest.raises(ApiError) as error:
                await pending
            assert error.value.code == "QUERY_CONTEXT_CHANGED"
            assert r.world["sim_time_ms"] >= initial + 200
            assert not r.queries.active
        finally:
            tick.cancel()
            with suppress(asyncio.CancelledError):
                await tick
            await r.queries.close()
    asyncio.run(check())


def test_duplicate_job_merges_queue_limit_and_revoked_session_rejects_all(rig):
    async def check():
        r = rig.runtime
        waiting, proceed = asyncio.Event(), asyncio.Event()
        class Waiting(MockReadAdapter):
            async def next_turn(self, model_input):
                waiting.set()
                await proceed.wait()
                return await super().next_turn(model_input)
        r.queries.adapter_factory = Waiting
        valid = [True]
        def authenticated():
            if not valid[0]:
                raise ApiError(401, "UNAUTHENTICATED", "revoked")
        session = rig.sessions["demo-operator"]
        body = AgentQuery(run_id=rig.run, goal="current_state")
        first = asyncio.create_task(r.queries.execute(session, body, "same", authenticated))
        await waiting.wait()
        duplicate = asyncio.create_task(r.queries.execute(session, body, "same", authenticated))
        await asyncio.sleep(0)
        assert len(r.queries.active) == 1
        with pytest.raises(ApiError) as limited:
            await r.queries.execute(session, body, "different", authenticated)
        assert limited.value.code == "AGENT_QUEUE_LIMIT"
        valid[0] = False
        proceed.set()
        outcomes = await asyncio.gather(first, duplicate, return_exceptions=True)
        assert all(isinstance(outcome, ApiError) and outcome.status == 401 for outcome in outcomes)
        assert not r.queries.active
        assert rig.db.execute("SELECT count(*) FROM business_requests WHERE key='same'").fetchone()[0] == 0
        await r.queries.close()
    asyncio.run(check())
