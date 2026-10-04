"""Paid-path admission and HTTP tests use injected clients, never credentials."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.live import LiveConfiguration, LiveModels
from backend.auth import ApiError
from contracts.agent_loop import LiveAgentQuery
from test_business_api import api_rig, login


def configuration(total=500, timeout=8):
    return LiveConfiguration.model_validate({"limits": {"total_krw": total, "daily_krw": total},
        "providers": {name: {"pricing": {"provider": name, "model": "test-model",
            "input_krw_per_million": "1000", "output_krw_per_million": "1000"},
            "max_output_tokens": 256, "timeout_seconds": timeout} for name in ("openai", "gemini")}})


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.ready = True
        self.hook = None

    def credentials_ready(self):
        return self.ready

    def input_token_bound(self, model_input):
        return 2048

    async def complete(self, model_input):
        self.calls.append(deepcopy(model_input))
        if self.hook:
            return await self.hook(model_input)
        if model_input["tool_results"]:
            turn = {"finish": {"status": "completed", "reason_code": "READ_COMPLETED", "answer": "관측을 조회했습니다."}}
        else:
            goal = model_input["request"]["goal"]
            names = ({"get_parking_state", "analyze_spatial_context"} if goal == "current_state" else
                {"get_parking_state", "get_my_vehicles"} if goal == "my_vehicle" else {"search_operating_knowledge"})
            names &= set(model_input["allowed_tools"])
            turn = {"tool_calls": [{"call_id": f"call-{len(self.calls)}-{index}", "name": name,
                "arguments": {"query": model_input["request"]["query"]} if name == "search_operating_knowledge" else {}}
                for index, name in enumerate(sorted(names))]}
        return SimpleNamespace(turn=turn, input_tokens=40, output_tokens=10, error_code=None)


def install(api_rig, tmp_path, *, total=500, timeout=8):
    client = FakeClient()
    models = LiveModels(configuration(total, timeout), tmp_path / "ledger.sqlite3",
        client_factory=lambda *args: client)
    async def configure():
        api_rig.runtime.world["run_status"] = "paused"
        api_rig.runtime.queries.live_models = models
    api_rig.client.portal.call(configure)
    return models, client


def query(rig, headers, provider="openai", goal="current_state", key="live", **fields):
    return rig.client.post("/api/v1/test/agent/live-queries", json={"run_id": rig.run,
        "goal": goal, "provider": provider, **fields}, headers=headers | {"Idempotency-Key": key})


def test_real_mode_actual_reads_answer_shared_cost_and_no_business_mutation(api_rig, tmp_path):
    models, fake = install(api_rig, tmp_path)
    headers = login(api_rig.client, "demo-owner")
    before = deepcopy(api_rig.runtime.world)
    first = query(api_rig, headers)
    assert first.status_code == 200
    result = first.json()
    assert result["mode"] == "live" and result["provider"] == "openai"
    assert result["status"] == "completed" and result["answer"]
    assert result["cost_actual_usd"] is None and result["cost_estimated_krw"] == 2
    assert result["usage_status"] == "known" and result["input_tokens"] == 80
    assert len(fake.calls) == 2 and all("provider" not in item["request"] for item in fake.calls)
    assert query(api_rig, headers).json() == result and len(fake.calls) == 2
    assert query(api_rig, headers, "gemini", key="gemini").json()["status"] == "completed"
    assert models.ledger.snapshot(models.configuration.limits, models.now()).total_spent_krw == 4
    assert api_rig.runtime.world == before
    for table in ("incidents", "plans", "notifications", "executions"):
        assert api_rig.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_driver_projection_and_http_authority(api_rig, tmp_path):
    _, fake = install(api_rig, tmp_path)
    assert query(api_rig, {}).status_code == 401
    headers = login(api_rig.client, "demo-driver")
    assert query(api_rig, {"Origin": headers["Origin"]}).status_code == 403
    assert query(api_rig, headers, model="expensive").status_code == 422
    result = query(api_rig, headers, goal="my_vehicle").json()
    assert result["status"] == "completed"
    state = next(item["result"] for item in result["tool_results"] if item["name"] == "get_parking_state")
    assert [obj["object_id"] for obj in state["snapshot"]["objects"]] == ["obj-car-02"]
    assert "obj-car-01" not in str(fake.calls)


def test_opt_in_missing_keys_pause_and_budget_block_before_dispatch(api_rig, tmp_path):
    headers = login(api_rig.client, "demo-owner")
    assert query(api_rig, headers).json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
    models, fake = install(api_rig, tmp_path, total=1)
    fake.ready = False
    assert query(api_rig, headers).json()["error"]["code"] == "MODEL_KEY_UNAVAILABLE"
    fake.ready = True
    response = query(api_rig, headers)
    assert response.json()["reason_code"] == "BUDGET_LIMIT" and not fake.calls
    assert response.json()["usage_status"] == "not_sent"
    async def start():
        api_rig.runtime.world["run_status"] = "running"
    api_rig.client.portal.call(start)
    assert query(api_rig, headers, key="running").json()["error"]["code"] == "LIVE_REQUIRES_PAUSED"


@pytest.mark.parametrize("failure", ["usage", "transport", "invalid"])
def test_failure_retains_or_settles_cost_without_exposing_errors(api_rig, tmp_path, failure):
    models, fake = install(api_rig, tmp_path)
    async def fail(_):
        if failure == "transport":
            raise RuntimeError("PRIVATE-CANARY")
        return SimpleNamespace(turn=None, input_tokens=40 if failure == "invalid" else None,
            output_tokens=10 if failure == "invalid" else None, error_code="MODEL_INVALID")
    fake.hook = fail
    headers = login(api_rig.client, "demo-owner")
    response = query(api_rig, headers)
    assert response.status_code == 200 and response.json()["status"] == "needs_review"
    assert "PRIVATE-CANARY" not in response.text
    budget = models.ledger.snapshot(models.configuration.limits, models.now())
    if failure == "invalid":
        assert budget.total_spent_krw == 1 and budget.total_pending_krw == 0
    else:
        assert budget.unknown_count == 1 and budget.total_pending_krw > 0
        assert response.json()["usage_status"] == "unknown"
        fake.hook = None
        assert query(api_rig, headers, provider="gemini", key="other").json()["status"] == "completed"
        assert models.public_status()["budget"]["unknown_count"] == 1
    assert query(api_rig, headers).json() == response.json()
    assert len(fake.calls) == (1 if failure == "invalid" else 3)


def test_state_change_after_charge_cannot_reissue_same_key(api_rig, tmp_path):
    models, fake = install(api_rig, tmp_path)
    async def change(_):
        api_rig.runtime.world["state_version"] += 1
        return SimpleNamespace(turn={"finish": {"status": "completed", "reason_code": "READ_COMPLETED"}},
            input_tokens=40, output_tokens=10, error_code=None)
    fake.hook = change
    headers = login(api_rig.client, "demo-owner")
    assert query(api_rig, headers).json()["error"]["code"] == "QUERY_CONTEXT_CHANGED"
    assert query(api_rig, headers).json()["error"]["code"] == "QUERY_CONTEXT_CHANGED"
    assert len(fake.calls) == 1
    assert models.ledger.snapshot(models.configuration.limits, models.now()).total_spent_krw == 1


def test_timeout_unknown_and_restart_pending_admission(api_rig, tmp_path):
    models, fake = install(api_rig, tmp_path, timeout=.01)
    async def slow(_):
        await asyncio.sleep(10)
    fake.hook = slow
    headers = login(api_rig.client, "demo-owner")
    response = query(api_rig, headers)
    assert response.json()["reason_code"] == "MODEL_TIMEOUT"
    assert response.json()["usage_status"] == "unknown" and response.json()["cost_pending_krw"] > 0
    assert models.ledger.snapshot(models.configuration.limits, models.now()).unknown_count == 1
    # Simulate a crash after admission but before a response was persisted.
    async def unfinished():
        from backend.business import encoded
        api_rig.runtime.store.db.execute("UPDATE business_requests SET response_json=? WHERE key='live'",
            (encoded({"context_stamp": api_rig.runtime.queries.stamp(api_rig.sessions["demo-owner"],
                LiveAgentQuery(run_id=api_rig.run, goal="current_state", provider="openai"), lambda: None), "pending": True}),))
    api_rig.client.portal.call(unfinished)
    assert query(api_rig, headers).json()["error"]["code"] == "QUERY_RECONCILIATION_REQUIRED"
    assert len(fake.calls) == 1


def test_dispatched_recovery_persists_unknown_and_limit_policy(tmp_path):
    from contracts.budget import TokenQuote
    models = LiveModels(configuration(), tmp_path / "ledger.sqlite3", client_factory=FakeClient)
    quote = TokenQuote(request_key="interrupted", input_tokens=100, max_output_tokens=256,
        pricing=models.configuration.providers["openai"].pricing)
    models.ledger.reserve(quote, models.configuration.limits, models.now())
    models.ledger.mark_dispatched("interrupted")
    reopened = LiveModels(configuration(), tmp_path / "ledger.sqlite3", client_factory=FakeClient)
    assert reopened.ledger.snapshot(reopened.configuration.limits, reopened.now()).unknown_count == 1
    with pytest.raises(ValueError):
        LiveModels(configuration(501), tmp_path / "ledger.sqlite3", client_factory=FakeClient)


def test_configuration_allows_explicit_budget_but_rejects_model_paths():
    assert configuration(100001).limits.daily_krw == 100001
    raw = configuration().model_dump()
    raw["providers"]["openai"]["pricing"]["model"] = "../../other"
    with pytest.raises(ValidationError):
        LiveConfiguration.model_validate(raw)


@pytest.mark.parametrize("known", [True, False])
def test_output_limit_never_executes_and_settles_only_known_usage(tmp_path, known):
    from agent.loop import ModelFailure
    client = FakeClient()
    async def limited(_):
        return SimpleNamespace(turn=None, input_tokens=40 if known else None,
            output_tokens=256 if known else None, error_code="MODEL_OUTPUT_LIMIT")
    async def check():
        pass
    client.hook = limited
    models = LiveModels(configuration(), tmp_path / "limited.sqlite3", client_factory=lambda *_: client)
    adapter = models.adapter("gemini", check)
    with pytest.raises(ModelFailure, match="MODEL_OUTPUT_LIMIT" if known else "MODEL_USAGE_UNKNOWN"):
        asyncio.run(adapter.next_turn({"request": {}, "allowed_tools": [], "tool_results": []}))
    snap = models.public_status()["budget"]
    assert len(client.calls) == 1
    assert snap["unknown_count"] == (0 if known else 1)
    assert adapter.result_metadata()["usage_status"] == ("known" if known else "unknown")
