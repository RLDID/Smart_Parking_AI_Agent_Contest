"""Pure fake dispatch contract tests: no HTTP, credentials or SQLite ledger."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_rag_semantics as semantic
from agent.providers import ProviderClient, ProviderReply
from contracts.budget import BudgetReservation

COMMIT = "a" * 40


class FakeLedger:
    opened = []

    def __init__(self, path):
        self.opened.append(path)
        self.rows = {}
        self.quotes = {}

    def reserve(self, quote, limits, now):
        self.quotes[quote.request_key] = quote
        row = BudgetReservation(request_key=quote.request_key, provider=quote.pricing.provider,
            model=quote.pricing.model, day_kst="2026-10-04", state="reserved", reserved_krw=1)
        self.rows[row.request_key] = row
        return row

    def update(self, key, **fields):
        self.rows[key] = self.rows[key].model_copy(update=fields)
        return self.rows[key]

    def mark_dispatched(self, key):
        return self.update(key, state="dispatched")

    def mark_unknown(self, key):
        return self.update(key, state="unknown")

    def cancel_unstarted(self, key):
        return self.update(key, state="cancelled")

    def settle(self, key, inp, out, now):
        return self.update(key, state="settled", actual_krw=1,
                           actual_input_tokens=inp, actual_output_tokens=out)


class FakeClient(ProviderClient):
    calls = []
    behavior = "success"

    def __init__(self, provider, model, max_output_tokens, timeout_seconds):
        super().__init__(provider, model, max_output_tokens, timeout_seconds, credential=lambda: "synthetic")
        assert max_output_tokens == 1024

    async def complete(self, model_input):
        self.calls.append(deepcopy(model_input))
        if self.behavior == "timeout":
            await asyncio.sleep(1)
        if self.behavior == "unknown":
            return ProviderReply({"finish": {"status": "completed", "reason_code": "READ_COMPLETED", "answer": "実際の保持対象"}}, None, None)
        inp = self.input_token_bound(model_input) + (1 if self.behavior == "overquote" else 0)
        turn = {"finish": {"status": "completed", "reason_code": "READ_COMPLETED", "answer": "수신은 해결이 아닙니다."}}
        if self.behavior == "extra_tool":
            turn = {"tool_calls": [{"call_id": "repeat", "name": "search_operating_knowledge", "arguments": {"query": "조회"}}]}
        return ProviderReply(turn, inp, 20, "MODEL_OUTPUT_LIMIT" if self.behavior == "output_limit" else None)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "LIVE_ROOT", tmp_path)
    ledger = tmp_path / "existing-shared-ledger.sqlite3"
    ledger.write_text("Sentinel: fake never opens SQLite", encoding="utf-8")
    monkeypatch.setattr(semantic, "SHARED_LEDGER", ledger)
    source = {"git_commit": COMMIT, "tracked_dirty": False, "sha256": {"dispatch": "frozen"},
              "retrieval_source": {"scoped_dirty": False}}
    monkeypatch.setattr(semantic, "dispatch_provenance", lambda: deepcopy(source))
    config = semantic.read(semantic.rag.ROOT / "data/samples/live-read-defaults.json")
    for settings in config["providers"].values():
        settings["timeout_seconds"] = 0.03
    configuration = tmp_path / "configuration.json"
    configuration.write_text(json.dumps(config), encoding="utf-8")
    samples = []
    for variant, method in (("current", "keyword_rag"), ("current", "small_whole_document"), ("natural_conflict", "keyword_rag")):
        condition = {"run_id": "run-synthetic", "variant": variant}
        context = {"status": "context_ready" if method == "small_whole_document" else "matched",
                   "references": [{"reference_id": "ref-synthetic", "excerpt": "수신과 해결은 다르다."}]}
        transport = deepcopy(context)
        transport["status"] = "matched"
        for provider, model in semantic.LIVE_MODELS.items():
            model_input = {"request": {"run_id": "run-synthetic", "goal": "regulation", "query": "수신과 해결"},
                "allowed_tools": ["search_operating_knowledge"],
                "tool_results": [{"call_id": "prepared-read", "name": "search_operating_knowledge", "result": transport}]}
            client = ProviderClient(provider, model, 1024, 0.03, credential=lambda: "")
            rate = config["providers"][provider]["pricing"]
            from agent.live import ProviderConfiguration
            upper = semantic.estimate(ProviderConfiguration.model_validate(config["providers"][provider]), 20000)
            samples.append({"id": provider + "-" + variant + "-" + method,
                "provider": provider, "model_ref": model, "variant": variant, "method": method,
                "question": "수신과 해결", "condition": condition,
                "condition_fingerprint": semantic.rag.sha(semantic.rag.encoded(condition)),
                "raw_context": context, "references": transport["references"], "model_input": model_input,
                "input_token_bound": client.input_token_bound(model_input), "max_input_tokens": 20000,
                "max_output_tokens": 1024, "quote_upper_krw": upper, "dispatch_preflight": "eligible"})
    plan = {"version": "contest-live-rag-preparation-v2", "dispatch_source": deepcopy(source),
        "ledger_path": str(ledger.resolve()), "ledger_file_identity": semantic.existing_ledger_binding(ledger)[1],
        "configuration": config, "configuration_sha256": hashlib.sha256(configuration.read_bytes()).hexdigest(),
        "samples": samples, "provider_calls": 0, "execution_status": "prepared_only",
        "max_calls": 6, "max_input_tokens": 20000, "max_output_tokens": 1024,
        "batch_estimate_upper_krw": sum(s["quote_upper_krw"] for s in samples)}
    directory = tmp_path / "session"
    directory.mkdir()
    prepared = directory / "prepared.json"
    output = directory / "evidence.json"
    FakeClient.calls = []
    FakeClient.behavior = "success"
    FakeLedger.opened = []
    # A missed fake injection must fail before any network activity.
    async def forbidden(*args, **kwargs):
        raise AssertionError("Real provider dispatch is forbidden")
    monkeypatch.setattr(ProviderClient, "complete", forbidden)
    def run(change=None, **kwargs):
        if change:
            change(plan)
        prepared.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return asyncio.run(semantic.collect_live_samples(prepared, output, configuration=configuration,
            target_commit=kwargs.pop("target_commit", COMMIT),
            packet_sha256=kwargs.pop("packet_sha256", hashlib.sha256(prepared.read_bytes()).hexdigest()),
            ledger_path=kwargs.pop("ledger_path", ledger), _client_factory=FakeClient,
            _ledger_factory=FakeLedger, **kwargs))
    return run, plan, source, output, ledger


def test_six_fake_calls_keep_real_response_refs_and_caps(session):
    run, plan, source, output, ledger = session
    result = run()
    assert result["execution_status"] == "completed"
    assert result["provider_calls"] == len(FakeClient.calls) == 6
    assert result["conditions"]["batch_estimate_upper_krw"] == 159
    assert result["conditions"]["retry"] is False and result["conditions"]["fallback"] is False
    assert result["not_dispatched"] == []
    assert all(r["answer"] and r["references"] and r["model"]["usage_status"] == "known" for r in result["rows"])
    assert all(set(c) == {"request", "allowed_tools", "tool_results"} for c in FakeClient.calls)
    assert all(r["reservations"][0]["state"] == "settled" for r in result["rows"])
    assert ledger.read_text() == "Sentinel: fake never opens SQLite"
    assert len(list((output.parent / "live-raw").glob("*.json"))) == 6
    with pytest.raises(ValueError, match="exists"):
        run()
    assert len(FakeClient.calls) == 6


@pytest.mark.parametrize("behavior,reason,state", [
    ("timeout", "MODEL_TIMEOUT", "unknown"),
    ("unknown", "MODEL_USAGE_UNKNOWN", "unknown"),
    ("overquote", "MODEL_USAGE_EXCEEDS_QUOTE", "settled"),
    ("extra_tool", "EVALUATION_EXTRA_TOOL_REQUEST", "settled"),
    ("output_limit", "MODEL_OUTPUT_LIMIT", "settled"),
])
def test_failure_stops_every_remaining_dispatch_and_preserves_receipt(session, behavior, reason, state):
    run, plan, source, output, ledger = session
    FakeClient.behavior = behavior
    result = run()
    assert result["execution_status"] == "stopped" and result["stop_reason"] == reason
    assert len(FakeClient.calls) == result["provider_calls"] == 1
    assert len(result["not_dispatched"]) == 5
    assert result["rows"][0]["reservations"][0]["state"] == state
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["rows"][0]["status"] == "failed"
    if behavior != "timeout":
        assert "provider_reply" in saved["rows"][0]
        assert saved["rows"][0]["provider_reply"]["turn"]
    assert len(list((output.parent / "live-raw").glob("*.json"))) == 1


@pytest.mark.parametrize("change", ["target", "dirty", "source", "hash", "quote", "input_cap", "extra_sample", "condition", "new_ledger"])
def test_all_preflight_failures_precede_ledger_and_dispatch(session, change):
    run, plan, source, output, ledger = session
    kwargs = {}
    mutate = None
    if change == "target":
        kwargs["target_commit"] = "b" * 40
    elif change == "dirty":
        source["tracked_dirty"] = True
    elif change == "source":
        source["sha256"]["dispatch"] = "changed"
    elif change == "hash":
        kwargs["packet_sha256"] = "0" * 64
    elif change == "quote":
        mutate = lambda p: p.update(batch_estimate_upper_krw=201)
    elif change == "input_cap":
        mutate = lambda p: p["samples"][0].update(input_token_bound=20001)
    elif change == "extra_sample":
        mutate = lambda p: p["samples"].append(deepcopy(p["samples"][0]))
    elif change == "condition":
        mutate = lambda p: p["samples"][0].update(condition_fingerprint="different")
    else:
        kwargs["ledger_path"] = ledger.parent / "replacement.sqlite3"
    with pytest.raises(ValueError):
        run(mutate, **kwargs)
    assert not FakeClient.calls and not FakeLedger.opened and not output.exists()


def test_source_change_after_admission_cancels_before_dispatch(session, monkeypatch):
    run, plan, source, output, ledger = session
    calls = []
    def provenance():
        calls.append(True)
        current = deepcopy(source)
        if len(calls) > 1:
            current["sha256"]["dispatch"] = "changed"
        return current
    monkeypatch.setattr(semantic, "dispatch_provenance", provenance)
    result = run()
    assert result["stop_reason"] == "EVALUATION_SOURCE_CHANGED"
    assert result["provider_calls"] == 0 and FakeClient.calls == []
    assert result["rows"][0]["reservations"][0]["state"] == "cancelled"


@pytest.mark.parametrize("path,allowed", [("scripts/compare_acceptance_rag.py", True),
    ("code/backend/knowledge.py", False), ("data/samples/rag/manifest-sim0.json", False),
    ("requirements.lock.txt", False)])
def test_dirty_retrieval_allows_only_pinned_evaluator(session, path, allowed):
    run, plan, source, output, ledger = session
    source["retrieval_source"].update(scoped_dirty=True, scoped_status=[" M " + path])
    plan["dispatch_source"] = deepcopy(source)
    if allowed:
        assert run()["provider_calls"] == 6
    else:
        with pytest.raises(ValueError, match="Retrieval product source"):
            run()
        assert FakeClient.calls == [] and FakeLedger.opened == []
        assert not output.exists()


def test_existing_other_ledger_rejected_before_open_and_dispatch(session):
    run, plan, source, output, ledger = session
    other=ledger.parent/'other-existing.sqlite3';other.write_text('Different existing file')
    with pytest.raises(ValueError,match='Ledger differs'):
        run(ledger_path=other)
    assert FakeLedger.opened==[] and FakeClient.calls==[] and not output.exists()


def test_replaced_same_path_ledger_rejected_before_dispatch(session):
    run, plan, source, output, ledger = session
    ledger.rename(ledger.with_suffix('.preserved'))
    ledger.write_text('Same pathname, different existing file')
    with pytest.raises(ValueError,match='Ledger differs'):
        run()
    assert FakeLedger.opened==[] and FakeClient.calls==[] and not output.exists()


def test_missing_ledger_is_not_created(tmp_path):
    missing=tmp_path/'missing.sqlite3'
    with pytest.raises(ValueError,match='existing shared ledger'):
        semantic.existing_ledger_binding(missing)
    assert not missing.exists()


def test_prepare_cli_forwards_selected_existing_ledger(tmp_path, monkeypatch):
    ledger=tmp_path/'selected.sqlite3';ledger.write_text('Existing fake ledger')
    called=[]
    async def prepare(output, configuration, *, ledger_path=None):
        called.append(semantic.existing_ledger_binding(ledger_path)[0])
        return {'samples':[], 'batch_estimate_upper_krw':0}
    monkeypatch.setattr(semantic,'prepare_live_samples',prepare)
    semantic.main(['prepare-live','--output',str(tmp_path/'prepared.json'),'--ledger-path',str(ledger)])
    assert called==[ledger.resolve()]
