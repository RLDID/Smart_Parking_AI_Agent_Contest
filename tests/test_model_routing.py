"""Routing uses injected clients and the existing shared ledger; no paid calls."""
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from agent.live import LiveConfiguration, LiveModels
from agent.providers import ProviderError
from contracts.agent_loop import LiveAgentQuery
from contracts.budget import TokenQuote
from test_business_api import api_rig, login
from test_live_agent import FakeClient, configuration, query


def install_routes(rig, tmp_path, *, primary_ready=True, hook=None):
    clients = {name: FakeClient() for name in ("openai", "gemini")}
    clients["openai"].ready = primary_ready
    clients["openai"].hook = hook
    models = LiveModels(configuration(), tmp_path / "route-ledger.sqlite3",
                        client_factory=lambda provider, *args: clients[provider])
    async def setup():
        rig.runtime.world["run_status"] = "paused"
        rig.runtime.queries.live_models = models
    rig.client.portal.call(setup)
    return models, clients


def test_auto_defaults_primary_and_cache_survives_key_unavailability(api_rig, tmp_path):
    _, clients = install_routes(api_rig, tmp_path)
    headers = login(api_rig.client, "demo-operator")
    result = query(api_rig, headers, provider="auto").json()
    assert result["status"] == "completed" and result["provider"] == "openai"
    assert result["routing_mode"] == "auto" and result["fallback_reason"] is None
    assert len(clients["openai"].calls) == 2 and not clients["gemini"].calls
    clients["openai"].ready = False
    assert query(api_rig, headers, provider="auto").json() == result
    assert not clients["gemini"].calls
    assert LiveAgentQuery(run_id=api_rig.run, goal="current_state").provider == "auto"


def test_missing_primary_uses_fallback_once_and_fixes_pending_route(api_rig, tmp_path):
    _, clients = install_routes(api_rig, tmp_path, primary_ready=False)
    async def inspect_pending(model_input):
        import json
        row = api_rig.runtime.store.db.execute("SELECT response_json FROM business_requests WHERE key='live'").fetchone()
        route = json.loads(row[0])["route"]
        assert route["provider"] == "gemini" and route["fallback_reason"] == "MODEL_KEY_UNAVAILABLE"
        clients["gemini"].hook = None
        return await clients["gemini"].complete(model_input)
    clients["gemini"].hook = inspect_pending
    headers = login(api_rig.client, "demo-operator")
    response = query(api_rig, headers, provider="auto")
    assert response.status_code == 200
    result = response.json()
    assert result["provider"] == "gemini" and result["fallback_reason"] == "MODEL_KEY_UNAVAILABLE"
    clients["openai"].ready = True
    assert query(api_rig, headers, provider="auto").json() == result
    assert not clients["openai"].calls


def test_explicit_primary_missing_never_switches(api_rig, tmp_path):
    _, clients = install_routes(api_rig, tmp_path, primary_ready=False)
    headers = login(api_rig.client, "demo-operator")
    response = query(api_rig, headers, provider="openai")
    assert response.status_code == 503 and response.json()["error"]["code"] == "MODEL_KEY_UNAVAILABLE"
    assert not clients["gemini"].calls


def test_both_keys_missing_do_not_admit_paid_request(api_rig, tmp_path):
    _, clients = install_routes(api_rig, tmp_path, primary_ready=False)
    clients["gemini"].ready = False
    headers = login(api_rig.client, "demo-operator")
    response = query(api_rig, headers, provider="auto")
    assert response.status_code == 503
    assert not clients["openai"].calls and not clients["gemini"].calls
    assert not api_rig.db.execute("SELECT 1 FROM business_requests WHERE key='live'").fetchone()


def test_missing_primary_configuration_can_select_prepared_fallback(api_rig, tmp_path):
    config = configuration().model_dump()
    config["providers"].pop("openai")
    client = FakeClient()
    models = LiveModels(config, tmp_path / "only-fallback.sqlite3", client_factory=lambda *args: client)
    assert models.select_route()["fallback_reason"] == "MODEL_NOT_CONFIGURED"
    assert models.select_route()["provider"] == "gemini"


def test_sent_failure_never_switches_but_new_paid_work_can_be_admitted(api_rig, tmp_path):
    async def fail(model_input):
        raise ProviderError("MODEL_RATE_LIMIT")
    _, clients = install_routes(api_rig, tmp_path, hook=fail)
    headers = login(api_rig.client, "demo-operator")
    result = query(api_rig, headers, provider="auto").json()
    assert result["reason_code"] == "MODEL_RATE_LIMIT" and result["usage_status"] == "unknown"
    assert not clients["gemini"].calls
    clients["openai"].ready = False
    blocked = query(api_rig, headers, provider="auto", key="new-key").json()
    assert blocked["status"] == "completed"
    assert len(clients["gemini"].calls) == 2
    assert len(clients["openai"].calls) == 1


def test_adopted_model_configuration_tracks_cost_without_forced_limits():
    config = LiveConfiguration.read(Path(__file__).resolve().parents[1] / "data/samples/live-read-defaults.json")
    assert config.providers["openai"].pricing.model == "gpt-6-luna"
    assert config.providers["gemini"].pricing.model == "gemini-3.8-flash"
    assert config.limits.total_krw is None and config.limits.daily_krw is None
    assert config.providers["gemini"].max_output_tokens == 2048
    assert config.providers["gemini"].timeout_seconds == 10


@pytest.mark.parametrize("change", (
    lambda raw: raw.pop("providers"),
    lambda raw: raw.pop("limits"),
    lambda raw: raw["providers"]["openai"]["pricing"].pop("output_krw_per_million"),
    lambda raw: raw["providers"]["openai"]["pricing"].update(input_krw_per_million="-1"),
    lambda raw: raw["providers"]["gemini"]["pricing"].update(provider="openai"),
    lambda raw: raw["providers"]["openai"].update(max_output_tokens=4097),
    lambda raw: raw["providers"]["openai"].update(timeout_seconds=11),
    lambda raw: raw["limits"].update(total_krw=100, daily_krw=101),
    lambda raw: raw["providers"]["gemini"]["pricing"].update(model="../../unknown"),
), ids=("missing-providers", "missing-limits", "missing-rate", "negative-rate", "provider-rate-mismatch", "output-limit",
        "timeout-limit", "combined-budget-limit", "invalid-model-identifier"))
def test_invalid_configuration_never_replaces_existing_ledger(tmp_path, change):
    path = tmp_path / "shared-ledger.sqlite3"
    configured = configuration()
    models = LiveModels(configured, path, client_factory=FakeClient)
    now = models.now()
    pricing = configured.providers["openai"].pricing
    settled = TokenQuote(request_key="existing-settled", input_tokens=100,
                         max_output_tokens=256, pricing=pricing)
    pending = TokenQuote(request_key="existing-pending", input_tokens=100,
                         max_output_tokens=256, pricing=pricing)
    models.ledger.reserve(settled, configured.limits, now)
    models.ledger.mark_dispatched(settled.request_key)
    models.ledger.settle(settled.request_key, 80, 20, now)
    models.ledger.reserve(pending, configured.limits, now)
    before = models.ledger.snapshot(configured.limits, now).model_dump()
    assert before["total_spent_krw"] > 0 and before["total_pending_krw"] > 0
    def stored_rows():
        with sqlite3.connect(path) as db:
            return (db.execute("SELECT request_key,state,reserved_krw,actual_krw FROM budget_requests ORDER BY request_key").fetchall(),
                    db.execute("SELECT total_krw,daily_krw FROM budget_policy").fetchall())
    original_rows = stored_rows()
    assert [row[1] for row in original_rows[0]] == ["reserved", "settled"]
    raw = configured.model_dump()
    change(raw)
    with pytest.raises(ValidationError):
        LiveModels(raw, path, client_factory=FakeClient)
    assert path.exists()
    reopened = LiveModels(configured, path, client_factory=FakeClient)
    assert reopened.ledger.snapshot(configured.limits, now).model_dump() == before
    assert stored_rows() == original_rows


def test_explicit_gemini_read_does_not_switch_when_unavailable(api_rig, tmp_path):
    _, clients = install_routes(api_rig, tmp_path)
    headers = login(api_rig.client, "demo-operator")
    result = query(api_rig, headers, provider="gemini", key="explicit-gemini").json()
    assert result["status"] == "completed"
    assert result["provider"] == "gemini" and result["routing_mode"] == "explicit"
    assert len(clients["gemini"].calls) == 2 and not clients["openai"].calls
    clients["gemini"].ready = False
    failed = query(api_rig, headers, provider="gemini", key="gemini-unavailable")
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "MODEL_KEY_UNAVAILABLE"
    assert not clients["openai"].calls
