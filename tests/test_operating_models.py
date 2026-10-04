"""Operational recommendations share billing controls and have no write authority."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.live import LiveModels
from agent.loop import ModelFailure
from agent.operating_models import LiveOperationsAdapter
from agent.providers import ProviderClient, ProviderError
from test_live_agent import configuration


DECISION = {"scenario": "s1a", "action": "notify", "target_ref": "obj-car-02",
            "reason_code": "PARKING_BLOCKED", "rationale": "공개 관측에서 지속 차단을 확인했습니다."}


class DecisionClient:
    def __init__(self):
        self.calls = []
        self.error = False
        self.answer = json.dumps(DECISION, ensure_ascii=False)

    def credentials_ready(self):
        return True

    def input_token_bound(self, data):
        return 4096

    async def complete(self, data):
        self.calls.append(data)
        if self.error:
            raise ProviderError("MODEL_NETWORK_ERROR")
        return SimpleNamespace(turn={"finish": {"status": "completed", "answer": self.answer}},
                               input_tokens=50, output_tokens=20, error_code=None)


def test_bounded_decisions_and_pre_dispatch_rechecks(tmp_path):
    client = DecisionClient()
    checks = []
    async def check():
        checks.append(True)
    models = LiveModels(configuration(), tmp_path / "decisions.sqlite3", client_factory=lambda *args: client)
    adapter = LiveOperationsAdapter(models, check)
    async def run():
        for _ in range(4):
            assert await adapter.decide({"observation": {"object_id": "obj-car-02"}}) == DECISION
        with pytest.raises(ModelFailure, match="MODEL_CALL_LIMIT"):
            await adapter.decide({})
    asyncio.run(run())
    assert len(checks) == len(client.calls) == 4
    assert client.calls[0]["allowed_tools"] == []
    assert adapter.result_metadata()["usage_status"] == "known"


def test_no_secret_or_future_context_is_dispatched(tmp_path):
    client = DecisionClient()
    async def check():
        pass
    models = LiveModels(configuration(), tmp_path / "private.sqlite3", client_factory=lambda *args: client)
    adapter = LiveOperationsAdapter(models, check)
    for key in ("actors", "fixture_ref", "seed", "password", "api_key"):
        with pytest.raises(ModelFailure, match="MODEL_CONTEXT_REJECTED"):
            asyncio.run(adapter.decide({"observation": {key: "PRIVATE-CANARY"}}))
    assert not client.calls


def test_model_receives_current_facts_without_scenario_label(tmp_path):
    client = DecisionClient()
    client.answer = json.dumps({key: value for key, value in DECISION.items() if key != "scenario"})
    async def check():
        pass
    models = LiveModels(configuration(), tmp_path / "labels.sqlite3", client_factory=lambda *args: client)
    result = asyncio.run(LiveOperationsAdapter(models, check).decide(
        {"scenario": "s1a", "observation": {"object_id": "obj-car-02"}}))
    assert result == DECISION
    assert "scenario" not in client.calls[0]["request"]["context"]


def test_cyclic_and_oversized_context_never_reaches_provider(tmp_path):
    client = DecisionClient()
    async def check():
        pass
    models = LiveModels(configuration(), tmp_path / "bounded.sqlite3", client_factory=lambda *args: client)
    adapter = LiveOperationsAdapter(models, check)
    cycle = {}
    cycle["self"] = cycle
    for value in (cycle, {"text": "x" * 60001}):
        with pytest.raises(ModelFailure, match="MODEL_CONTEXT_REJECTED"):
            asyncio.run(adapter.decide(value))
    assert not client.calls


def test_sent_failure_and_bad_json_keep_correct_billing_state(tmp_path):
    client = DecisionClient()
    async def check():
        pass
    models = LiveModels(configuration(), tmp_path / "sent.sqlite3", client_factory=lambda *args: client)
    adapter = LiveOperationsAdapter(models, check)
    client.answer = "not-json"
    with pytest.raises(ModelFailure, match="MODEL_INVALID"):
        asyncio.run(adapter.decide({}))
    assert adapter.result_metadata()["usage_status"] == "known"
    client.error = True
    with pytest.raises(ModelFailure, match="MODEL_NETWORK_ERROR"):
        asyncio.run(adapter.decide({}))
    assert adapter.result_metadata()["usage_status"] == "unknown"
    client.error = False
    client.answer = json.dumps(DECISION)
    assert asyncio.run(LiveOperationsAdapter(models, check).decide({})) == DECISION
    assert len(client.calls) == 3
    assert models.public_status()["budget"]["unknown_count"] == 1


def test_provider_operational_goal_exposes_only_recommendation_finish():
    for name, model in (("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")):
        client = ProviderClient(name, model, credential=lambda: "test-credential")
        _, payload, allowed = client._payload({"request": {"goal": "operations", "context": {}},
                                              "allowed_tools": [], "tool_results": []})
        assert allowed == set()
        if name == "openai":
            assert [tool["name"] for tool in payload["tools"]] == ["finish_read"]
            assert "action, " in payload["instructions"] and "scenario" not in payload["instructions"]
        else:
            assert payload["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == ["finish_read"]


def test_operational_auto_route_is_fixed_before_dispatch_and_never_retries_unknown(tmp_path):
    clients = {name: DecisionClient() for name in ("openai", "gemini")}
    clients["openai"].credentials_ready = lambda: False
    models = LiveModels(configuration(), tmp_path / "operational-route.sqlite3",
                        client_factory=lambda provider, *args: clients[provider])
    async def check():
        pass
    adapter = LiveOperationsAdapter(models, check)
    assert adapter.route == {"provider": "gemini", "model_ref": "test-model",
                             "routing_mode": "auto", "fallback_reason": "MODEL_KEY_UNAVAILABLE"}
    assert asyncio.run(adapter.decide({"scenario": "s1a"})) == DECISION
    assert len(clients["gemini"].calls) == 1 and not clients["openai"].calls

    clients["openai"].credentials_ready = lambda: True
    clients["openai"].error = True
    failing = LiveOperationsAdapter(models, check)
    assert failing.route["provider"] == "openai"
    with pytest.raises(ModelFailure, match="MODEL_NETWORK_ERROR"):
        asyncio.run(failing.decide({"scenario": "s1a"}))
    assert failing.result_metadata()["usage_status"] == "unknown"
    assert len(clients["openai"].calls) == 1 and len(clients["gemini"].calls) == 1

    reopened = LiveModels(configuration(), tmp_path / "operational-route.sqlite3",
                          client_factory=lambda provider, *args: clients[provider])
    assert reopened.ledger.snapshot(reopened.configuration.limits, reopened.now()).unknown_count == 1
    clients["openai"].credentials_ready = lambda: False
    blocked = LiveOperationsAdapter(reopened, check)
    assert blocked.route["provider"] == "gemini"
    assert asyncio.run(blocked.decide({"scenario": "s1a"})) == DECISION
    assert len(clients["gemini"].calls) == 2
    assert reopened.public_status()["budget"]["unknown_count"] == 1


def test_cancelled_operational_dispatch_keeps_reservation_and_allows_new_work(tmp_path):
    async def exercise():
        entered = asyncio.Event()
        class CancelClient(DecisionClient):
            async def complete(self, data):
                self.calls.append(data)
                entered.set()
                await asyncio.Event().wait()

        clients = {"openai": CancelClient(), "gemini": DecisionClient()}
        models = LiveModels(configuration(), tmp_path / "cancelled-work.sqlite3",
                            client_factory=lambda provider, *_: clients[provider])
        async def check():
            pass
        adapter = LiveOperationsAdapter(models, check)
        task = asyncio.create_task(adapter.decide({"scenario": "s1a"}))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.result_metadata()["usage_status"] == "unknown"
        assert models.ledger.snapshot(models.configuration.limits, models.now()).unknown_count == 1
        clients["openai"].credentials_ready = lambda: False
        second = LiveOperationsAdapter(models, check)
        assert second.route["provider"] == "gemini"
        assert await second.decide({"scenario": "s1a"}) == DECISION
        assert len(clients["openai"].calls) == len(clients["gemini"].calls) == 1
    asyncio.run(exercise())
