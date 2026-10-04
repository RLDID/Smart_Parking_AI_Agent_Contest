"""Disposable ASGI/fake evaluation only; no credentials, shared DB or network."""
import importlib.util
import json
from pathlib import Path

import pytest

from agent.providers import ProviderError
from agent.live import LiveConfiguration


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_model_quality.py"
spec = importlib.util.spec_from_file_location("quality_evaluation", SCRIPT)
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def config():
    return LiveConfiguration.model_validate({"limits": {"total_krw": None, "daily_krw": 1000},
        "providers": {p: {"pricing": {"provider": p, "model": "fake-model",
            "input_krw_per_million": "1000", "output_krw_per_million": "1000"},
            "max_output_tokens": 256, "timeout_seconds": 8} for p in ("openai", "gemini")}})


def test_default_plan_pins_repeat_conditions_and_has_no_live_claim():
    report = quality.prepare()
    assert len(report["tasks"]) == 32
    assert all(sum(t["provider"] == p and t["scenario"] == s for t in report["tasks"]) == 3
               for p in ("openai", "gemini") for s in quality.SCENARIOS)
    assert report["condition"]["watcher_cycles"] == 3
    assert report["scope"]["paid_calls"] == 0
    assert not quality.summarize(report)["live_selected_matrix_passed"]


def test_prepare_cli_never_opens_application_credentials_or_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "ARTIFACT_ROOT", tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("prepare must not open an application or cost ledger")
    monkeypatch.setattr(quality, "create_app", forbidden)
    monkeypatch.setattr(quality, "LiveModels", forbidden)
    assert quality.main(["--output", str(tmp_path / "prepared")]) == 0
    assert json.loads((tmp_path / "prepared/summary.json").read_text(encoding="utf-8"))["mode"] == "mock"
    with pytest.raises(SystemExit):
        quality.main(["--output", str(tmp_path.parent / "outside")])


def test_fake_all_business_scenes_use_real_results_without_expected_input(tmp_path):
    report = quality.prepare("fake", providers=("gemini",), repeats=1, config=config())
    class AuditedFake(quality.PublicFakeClient):
        async def complete(self, model_input):
            encoded = json.dumps(model_input)
            assert all(f'"{key}"' not in encoded for key in (
                "expected", "fixture_ref", "seed", "future_path", "recovery_ticks", "repeats"))
            return await super().complete(model_input)
    quality.run_pending(report, tmp_path, scenarios=quality.SCENARIOS, fake_factory=lambda *_: AuditedFake())
    business = [t for t in report["tasks"] if t["scenario"] != "watcher"]
    assert [(t["scenario"], t["status"], t.get("reason")) for t in business] == [
        (s, "passed", None) for s in quality.SCENARIOS]
    assert [len(t["calls"]) for t in business] == [2, 2, 2, 1, 6]
    assert all(t["cleanup"]["active_jobs"] == 0 and not t["cleanup"]["socket_server_started"] for t in business)
    assert all(c["private_input_guard"] == "passed" for t in business for c in t["contexts"])
    assert all(t["cost"]["basis"] == "simulated" for t in business)
    assert report["scope"]["paid_calls"] == 0
    assert not report["summary"]["live_selected_matrix_passed"]
    assert (tmp_path / "gemini-s3-001/result.json").is_file()


def test_real_business_loop_watcher_three_cycles_and_idle_no_duplicates(tmp_path):
    # Use the actual adopted cooldown; no time acceleration or policy weakening.
    report = quality.prepare(providers=("openai",), scenarios=(), watcher_cycles=3)
    quality.run_pending(report, tmp_path)
    task = report["tasks"][0]
    assert task["status"] == "passed", task.get("reason", task.get("error_type"))
    assert task["watcher"]["business_loop_started"] and task["watcher"]["business_loop_stopped"]
    assert task["watcher"]["manual_watcher_tick_calls"] == 0
    assert task["watcher"]["completed_cycles"] == 3 and task["watcher"]["scheduler_ticks"] >= 100
    assert task["business_evidence"]["counts"]["executions"] == 3
    assert len(task["business_evidence"]["jobs"]) == 9
    assert len([s for s in task["steps"] if s["stage"] == "watcher_cycle"]) == 3
    assert all(s["before"] == s["after"] for s in task["steps"] if "idle_ticks" in s and "before" in s)
    assert task["cleanup"]["active_jobs"] == 0 and not task["cleanup"]["watcher_enabled"]


def test_failure_preserves_unknown_and_resume_never_replays_failed_or_started(tmp_path):
    report = quality.prepare("fake", providers=("gemini",), scenarios=("s1a", "s2"),
                             repeats=1, config=config())
    class Failed(quality.PublicFakeClient):
        calls = 0
        async def complete(self, _input):
            Failed.calls += 1
            raise ProviderError("MODEL_TIMEOUT")
    quality.run_pending(report, tmp_path, fake_factory=lambda *_: Failed())
    first = report["tasks"][0]
    assert first["status"] == "failed" and first["cost"]["new_unknown_count"] == 1
    assert Failed.calls == 1 and first["business_evidence"]["counts"]["notifications"] == 0
    original = json.loads((tmp_path / first["id"] / "result.json").read_text(encoding="utf-8"))
    # Simulate process interruption of a different already-started task.
    report["tasks"][1]["status"] = "started"
    quality.run_pending(report, tmp_path, scenarios=("s1a", "s2"), fake_factory=lambda *_: Failed())
    assert report["tasks"][1]["status"] == "interrupted"
    assert Failed.calls == 1
    assert json.loads((tmp_path / first["id"] / "result.json").read_text(encoding="utf-8")) == original
    assert report["budget_after"]["unknown_count"] == 1


def test_stop_and_evaluator_cost_guard_prevent_dispatch(tmp_path):
    report = quality.prepare("fake", providers=("openai",), scenarios=("s2",), repeats=1, config=config())
    stop = tmp_path / "STOP"
    stop.write_text("stop", encoding="utf-8")
    quality.run_pending(report, tmp_path, stop=stop)
    assert all(t["status"] == "pending" for t in report["tasks"])
    stop.unlink()
    quality.run_pending(report, tmp_path, budget_krw=1, max_items=1)
    task = report["tasks"][0]
    assert task["status"] == "stopped" and task["reason"] == "EVALUATION_COST_GUARD"
    assert task["calls"] == [] and task["business_evidence"]["counts"]["notifications"] == 0


def test_resume_rejects_source_or_shared_ledger_change(tmp_path):
    report = quality.prepare()
    report["source_digest"] = "changed"
    with pytest.raises(ValueError, match="Source changed"):
        quality.run_pending(report, tmp_path)
    live = quality.prepare("live", config=config())
    with pytest.raises(ValueError, match="Existing shared ledger"):
        quality.run_pending(live, tmp_path, ledger=tmp_path / "missing.sqlite3")
    existing = tmp_path / "existing.sqlite3"
    existing.write_text("not opened in this test", encoding="utf-8")
    live["ledger_path"] = "other"
    with pytest.raises(ValueError, match="cannot switch"):
        quality.run_pending(live, tmp_path, ledger=existing)
