"""Independent harness regressions: execution, false-pass prevention and cleanup."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("acceptance_harness", ROOT / "scripts/run_acceptance.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


@pytest.fixture
def specs():
    inputs, _ = harness.load_json(ROOT / "tests/scenarios/acceptance-inputs.json")
    expected, _ = harness.load_json(ROOT / "tests/expected/acceptance-cases.json")
    return inputs, expected


@pytest.fixture
def cli_resources():
    parent = ROOT / "Work_tree/artifacts/acceptance-194/harness-tests/tmp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="cli-regression-", dir=parent) as directory:
        yield Path(directory)


def direct_ids(inputs):
    return {item["id"] for item in inputs["direct"]}


def row_for(report, case_id):
    return next(row for row in report["cases"] if row["id"] == case_id)


def test_native_probes_repeat_real_world_and_keep_acceptance_unrun(specs, tmp_path, monkeypatch):
    import socket

    def network_forbidden(*args, **kwargs):
        raise AssertionError("native probes must not use network")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    inputs, expected = specs
    report, code = harness.evaluate(inputs, expected, selected=direct_ids(inputs),
                                    repeat=2, resource_root=tmp_path)
    total = sum(len(group["variants"]) for group in inputs["groups"])
    count = len(inputs["direct"])
    assert code == 0
    assert report["summary"]["registered"] == total
    assert report["summary"]["executable"] == report["summary"]["executed"] == count
    assert report["summary"]["passed"] == count
    assert report["summary"]["attempts_executed"] == count * 2
    assert report["summary"]["attempts_passed_partial"] == count * 2
    assert report["summary"]["not_run"] == total - count
    assert report["summary"]["acceptance_passed"] == 0
    assert report["summary"]["representative_s1_streak"] is None
    assert report["summary"]["forbidden_execution_rate"] is None
    assert not report["full_acceptance"] and report["actual_provider_calls"] == 0
    assert report["resources"]["all_scratch_cleaned"]
    assert list((tmp_path / "tmp").iterdir()) == []
    paths, run_ids = set(), set()
    for row in report["cases"]:
        assert row["decision_evaluation"] == row["linked_execution"] == "not_run"
        for attempt in row["attempts"]:
            paths.add(attempt["scratch_path"])
            run_ids.add(attempt["evidence"]["run_id"])
            assert attempt["evidence"]["tick_ms"] == 100
            assert attempt["evidence"]["actual_versions"]["map_version"] == "map-01-draft"
            assert attempt["runtime_version_comparison"] == "matched"
            assert all(attempt["evidence"]["actual_versions"][key] == "not_loaded"
                       for key in ("document", "index", "structured_policy"))
            assert attempt["scratch_cleaned"]
        if row["attempts"]:
            assert row["attempts"][0]["actual"] == row["attempts"][1]["actual"]
    assert len(paths) == len(run_ids) == count * 2
    assert row_for(report, "T03-crossing")["attempts"][0]["actual"]["contact"] is True
    assert row_for(report, "T03-offset")["attempts"][0]["actual"]["contact"] is False
    assert row_for(report, "T21-occluded")["attempts"][0]["actual"]["visibility"] == "occluded"


def test_all_registered_cases_preserve_unrun_and_linked_evidence(specs, tmp_path):
    inputs, expected = specs
    report, code = harness.evaluate(inputs, expected, resource_root=tmp_path)
    assert code == 3
    summary = report["summary"]
    assert summary["registered"] == summary["executed"] + summary["not_run"]
    assert summary["unsupported_selected"] == summary["not_run"]
    external = row_for(report, "V01-browser-reconnect")
    assert external["status"] == "unsupported" and not external["attempts"]
    linked = row_for(report, "T23-unknown-restart-no-duplicate")
    assert linked["linked_nodeids"] == inputs["linked_nodeids"][linked["id"]]
    assert linked["linked_execution"] == "not_run"
    assert report["r08_quality"].startswith("not_run")


@pytest.mark.parametrize("mutation", [
    lambda i, e: i.pop("schema_version"),
    lambda i, e: e.update(schema_version="acceptance-cases-v999"),
    lambda i, e: i.pop("suite_version"),
    lambda i, e: e.update(suite_version="other"),
    lambda i, e: i["versions"].pop("policy"),
    lambda i, e: i["versions"].pop("document"),
    lambda i, e: i["versions"].update(expected="unknown"),
    lambda i, e: i.update(mode="live"),
    lambda i, e: i.update(actual_provider_call_budget=1),
    lambda i, e: i["splits"]["calibration"].update(seed_base=i["splits"]["final"]["seed_base"]),
    lambda i, e: i["groups"].pop(),
    lambda i, e: i["groups"].append(deepcopy(i["groups"][0])),
    lambda i, e: e["criteria"].pop("T01"),
    lambda i, e: e["variant_expected"].pop("T01-blocked"),
    lambda i, e: e["direct_assertions"].pop("T01-blocked"),
    lambda i, e: e["direct_assertions"]["T01-blocked"].update(not_a_predicate=True),
    lambda i, e: e["direct_assertions"]["T03-crossing"].update(contact=1),
    lambda i, e: i["direct"][0].update(tick=-1),
    lambda i, e: i["direct"][0].update(seed=True),
    lambda i, e: i["direct"].append(deepcopy(i["direct"][0])),
], ids=["input-schema", "expected-schema", "missing-version", "mismatched-version",
        "policy-version", "document-version", "expected-version", "live", "provider-budget",
        "split-overlap", "missing-group", "duplicate-group", "missing-criteria", "missing-truth",
        "missing-assertion", "unknown-predicate", "bool-type", "negative-tick", "bool-seed", "duplicate-direct"])
def test_invalid_spec_rejected_before_any_execution(specs, tmp_path, mutation):
    inputs, expected = specs
    mutation(inputs, expected)

    def must_not_run(**kwargs):
        pytest.fail("invalid specification reached product runner")

    with pytest.raises(harness.SpecError):
        harness.evaluate(inputs, expected, resource_root=tmp_path, runner_fn=must_not_run)
    assert not (tmp_path / "tmp").exists()


@pytest.mark.parametrize("patch", [{"fixture": "not-supported"}, {"runner": "live-agent"},
                                   {"fixture": "s2-crossing-v1"}, {"injection": "unimplemented"}])
def test_frozen_direct_fixture_or_runner_tampering_is_invalid(specs, tmp_path, patch):
    inputs, expected = specs
    inputs["direct"][0].update(patch)

    def must_not_run(**kwargs):
        pytest.fail("unsupported controls reached product runner")

    with pytest.raises(harness.SpecError):
        harness.evaluate(inputs, expected, selected={"T01-blocked"},
                         resource_root=tmp_path, runner_fn=must_not_run)


@pytest.mark.parametrize("mutation", [
    lambda i: i["l3_cases"][0]["injection"].update(response="will_move"),
    lambda i: i["l3_cases"][0].update(tick=999),
    lambda i: i["l3_cases"][0].update(seed=99111),
    lambda i: (i["l3_cases"][0]["injection"].update(response="will_move"),
               i["l3_cases"][0].update(tick=999, seed=99111)),
    lambda i: i["direct"][0].update(seed=99112),
    lambda i: i["direct"][0].update(tick=999),
    lambda i: i["rag_comparison"]["questions"][0].update(query="different evaluation question"),
    lambda i: i["rag_comparison"]["questions"][0].update(seed=99113),
    lambda i: i["rag_comparison"].update(access_filter="changed reader condition"),
], ids=["response", "tick", "seed", "combined", "direct-seed", "direct-tick",
        "rag-question", "rag-seed", "rag-filter"])
def test_fixed_final_condition_tampering_requires_new_review(specs, tmp_path, mutation):
    inputs, expected = specs
    mutation(inputs)
    with pytest.raises(harness.SpecError, match="final input conditions changed"):
        harness.evaluate(inputs, expected, resource_root=tmp_path,
                         runner_fn=lambda **kw: pytest.fail("tampered inputs executed"))
    assert not (tmp_path / "tmp").exists()


def test_parsed_condition_digest_ignores_json_format_and_explanatory_annotations(specs):
    inputs, expected = specs
    inputs = json.loads(json.dumps(inputs, sort_keys=True, indent=4, ensure_ascii=True))
    inputs["splits"]["final"]["use"] = "clarified explanatory note"
    inputs["l3_cases"][0]["unit_function"] = "test_clarified_reference"
    harness.validate_specs(inputs, expected)


def test_expected_mutation_changes_verdict_not_product_inputs(specs, tmp_path, monkeypatch):
    from simulator import world
    inputs, expected = specs
    captured = []
    original = world.initial_world

    def capture(seed, fixture):
        captured.append((seed, fixture))
        return original(seed, fixture)

    monkeypatch.setattr(world, "initial_world", capture)
    expected["direct_assertions"]["T01-blocked"]["b_x"] = 999
    expected["variant_expected"]["T01-blocked"]["decision"] = "ORACLE_SENTINEL"
    report, code = harness.evaluate(inputs, expected, selected={"T01-blocked"}, resource_root=tmp_path)
    assert code == 1
    attempt = row_for(report, "T01-blocked")["attempts"][0]
    assert attempt["actual"]["b_x"] == 4.0
    assert attempt["mismatches"] == [{"field": "b_x", "expected": 999, "actual": 4.0}]
    assert captured == [(11001, "s1a-foundation-v1")]
    assert "ORACLE_SENTINEL" not in json.dumps(attempt["evidence"])
    assert attempt["scratch_cleaned"]


@pytest.mark.parametrize("exception,exit_code,status", [
    (RuntimeError("injected runner failure"), 1, "error"),
    (KeyboardInterrupt(), 130, "interrupted"),
])
def test_runner_failure_and_interrupt_preserve_evidence_denominators_and_cleanup(
        specs, tmp_path, exception, exit_code, status):
    inputs, expected = specs
    scratch_paths = []

    def fail(**kwargs):
        scratch = kwargs["scratch"]
        (scratch / "partially-written.txt").write_text("partial", encoding="utf-8")
        scratch_paths.append(scratch)
        raise exception

    report, code = harness.evaluate(inputs, expected, selected=direct_ids(inputs), repeat=3,
                                    resource_root=tmp_path, runner_fn=fail)
    assert code == exit_code
    assert row_for(report, inputs["direct"][0]["id"])["status"] == status
    summary = report["summary"]
    assert summary["passed"] == summary["acceptance_passed"] == 0
    assert summary["registered"] == summary["executed"] + summary["not_run"]
    assert summary["attempts_planned"] == summary["attempts_executed"] + summary["attempts_not_run"]
    if status == "interrupted":
        assert summary["executed"] == summary["attempts_interrupted"] == 1
        assert summary["attempts_executed"] == 1
    else:
        assert summary["attempts_failed"] == 3 * len(inputs["direct"])
    assert scratch_paths and all(not path.exists() for path in scratch_paths)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_calibration_and_final_use_separate_recorded_seeds(specs, tmp_path):
    inputs, expected = specs
    observed = {}
    for split in ("calibration", "final"):
        report, code = harness.evaluate(inputs, expected, split=split, selected={"T03-crossing"},
                                        resource_root=tmp_path / split)
        assert code == 0
        attempt = row_for(report, "T03-crossing")["attempts"][0]
        assert attempt["seed"] == attempt["evidence"]["seed"]
        assert attempt["split"] == split and attempt["mode"] == "mock"
        observed[split] = attempt["seed"]
    assert observed == {"calibration": 11002, "final": 91002}


def test_repeat_failure_is_not_hidden_by_a_passing_attempt(specs, tmp_path):
    inputs, expected = specs
    calls = 0

    def divergent(**kwargs):
        nonlocal calls
        actual, evidence = harness.run_world(**kwargs)
        calls += 1
        if calls == 2:
            actual["b_x"] = -1
        return actual, evidence

    report, code = harness.evaluate(inputs, expected, selected={"T01-blocked"}, repeat=3,
                                    runner_fn=divergent, resource_root=tmp_path)
    assert code == 1
    assert report["summary"]["passed"] == 0
    assert report["summary"]["failed"] == 1
    assert report["summary"]["attempts_passed_partial"] == 2
    assert report["summary"]["attempts_failed"] == 1
    assert report["summary"]["attempts_executed"] == 3


def test_registered_extension_is_reported_unrun_not_dropped(specs, tmp_path):
    inputs, expected = specs
    case_id = "T04-s1a-silent"
    report, code = harness.evaluate(inputs, expected, selected={case_id}, resource_root=tmp_path)
    assert code == 3
    assert row_for(report, case_id)["status"] == "unsupported"
    assert report["summary"]["registered"] == len(expected["variant_expected"])
    assert report["pending_extension_inputs"]["l3_cases"] == inputs["l3_cases"]
    assert report["pending_extension_inputs"]["rag_comparison"] == inputs["rag_comparison"]
    assert report["pending_extension_inputs"]["l3_case_defaults"] == inputs["l3_case_defaults"]
    for key in ("l3_direct_assertions", "l3_assertion_rules", "rag_reference_expectations"):
        assert report["pending_extension_expected"][key] == expected[key]
    assert report["pending_extension_status"].startswith("not_run")


@pytest.mark.parametrize("mutation", [
    lambda i, e: i["versions"].update(map="unsupported-map-v999"),
    lambda i, e: i["versions"].update(fixture="unsupported-fixture-v999"),
    lambda i, e: i["versions"].update(policy="unsupported-policy-v999"),
    lambda i, e: i["versions"].update(document="unsupported-document-v999"),
    lambda i, e: i["versions"].update(index="unsupported-index-v999"),
    lambda i, e: i.pop("l3_cases"),
    lambda i, e: i["l3_cases"].clear(),
    lambda i, e: e.pop("l3_direct_assertions"),
    lambda i, e: e["l3_direct_assertions"].pop("T04-s1a-silent"),
    lambda i, e: e["l3_direct_assertions"].update(orphan={"rule": "receipt_no_motion"}),
    lambda i, e: i["l3_cases"][1].update(seed=i["l3_cases"][0]["seed"]),
    lambda i, e: i["l3_cases"][0].update(calibration_seed=i["l3_cases"][0]["seed"]),
    lambda i, e: i["l3_cases"][0].update(fixture="unsupported-scene"),
    lambda i, e: i["l3_cases"][0].pop("injection"),
    lambda i, e: i.pop("l3_case_defaults"),
    lambda i, e: e.pop("l3_assertion_rules"),
    lambda i, e: e["l3_assertion_rules"]["receipt_no_motion"].pop("checks"),
    lambda i, e: e["l3_assertion_rules"]["receipt_no_motion"]["checks"].pop(),
    lambda i, e: e.pop("rag_reference_expectations"),
    lambda i, e: e["rag_reference_expectations"]["sim0_current"]["groups"].pop("R01-s1a-procedure"),
    lambda i, e: e["rag_reference_expectations"]["sim0_current"]["groups"]["R01-s1a-procedure"]["required_references"].pop(),
    lambda i, e: e["rag_reference_expectations"]["sim0_current"].update(release_id="other-release"),
    lambda i, e: i["rag_comparison"]["questions"].pop(),
    lambda i, e: i["rag_comparison"]["questions"][1].update(seed=i["rag_comparison"]["questions"][0]["seed"]),
    lambda i, e: i["rag_comparison"]["questions"][0].update(role="driver"),
    lambda i, e: i["rag_comparison"]["same_condition_fields"].remove("principal_role"),
    lambda i, e: e["rag_reference_expectations"]["comparison"].update(same_access_filter_required=False),
    lambda i, e: e["rag_reference_expectations"]["legacy_fixture"]["cases"].pop(),
    lambda i, e: i["rag_comparison"].update(legacy_expected_path=".env"),
    lambda i, e: i.update(version_contract="fixture-runtime-v999"),
    lambda i, e: i.pop("fixture_versions"),
    lambda i, e: i["fixture_versions"]["s1a-foundation-v1"].update(map_version="unknown-map"),
    lambda i, e: i["fixture_versions"]["s1a-foundation-v1"].update(document="loaded"),
], ids=["map", "fixture", "policy", "document", "index", "missing-l3", "empty-l3",
        "missing-l3-expected", "missing-l3-id", "orphan-l3-id", "duplicate-final", "split-overlap",
        "unsupported-scene", "missing-injection", "missing-defaults", "missing-rules", "missing-checks", "lost-rule-check",
        "missing-rag-truth", "missing-rag-group", "incomplete-rag-refs", "wrong-release", "missing-question",
        "duplicate-rag-seed", "wrong-reader", "missing-filter", "false-filter", "missing-legacy-case",
        "secret-path", "future-version-contract", "missing-fixture-versions", "unknown-runtime-map", "false-loaded"])
def test_adopted_extension_contract_rejects_loss_before_running(specs, tmp_path, mutation):
    inputs, expected = specs
    mutation(inputs, expected)
    with pytest.raises(harness.SpecError):
        harness.evaluate(inputs, expected, resource_root=tmp_path,
                         runner_fn=lambda **kwargs: pytest.fail("invalid extension reached product"))
    assert not (tmp_path / "tmp").exists()


def test_new_registry_requires_explicit_reviewed_suite_version(specs):
    inputs, expected = specs
    inputs["groups"][0]["variants"].append("new-unreviewed")
    expected["variant_expected"]["T01-new-unreviewed"] = {"decision": "held"}
    with pytest.raises(harness.SpecError, match="new suite/schema"):
        harness.validate_specs(inputs, expected)


def test_runtime_version_mismatch_is_execution_failure_not_declared_bundle_match(specs, tmp_path):
    inputs, expected = specs
    inputs["fixture_versions"]["s1a-foundation-v1"]["configuration_version"] = "sim0-v1"
    report, code = harness.evaluate(inputs, expected, selected={"T01-blocked"}, resource_root=tmp_path)
    assert code == 1
    attempt = row_for(report, "T01-blocked")["attempts"][0]
    assert attempt["runtime_version_comparison"] == "failed"
    assert attempt["evidence"]["actual_versions"]["configuration_version"] == "foundation-v1"
    assert attempt["evidence"]["actual_versions"]["document"] == "not_loaded"
    assert report["declared_versions_scope"] == "suite_bundle_declared"
    assert attempt["mismatches"] == [{"field": "version/configuration_version", "expected": "sim0-v1",
                                      "actual": "foundation-v1"}]


@pytest.mark.parametrize("removed", [
    ("version_contract",), ("versions_scope",), ("fixture_versions",),
    ("version_contract", "versions_scope", "fixture_versions"),
])
def test_adopted_runtime_version_contract_cannot_be_removed(specs, tmp_path, removed):
    inputs, expected = specs
    for key in removed:
        inputs.pop(key)
    with pytest.raises(harness.SpecError):
        harness.evaluate(inputs, expected, selected={"T01-blocked"}, resource_root=tmp_path,
                         runner_fn=lambda **kwargs: pytest.fail("missing contract reached product"))
    assert not (tmp_path / "tmp").exists()


@pytest.mark.parametrize("status,dirty,tracked,untracked", [
    (" M code/simulator/devices.py\0?? scratch/\0", True, True, True),
    ("?? scripts/run_acceptance.py\0", True, False, True), ("", False, False, False),
    (None, None, None, None),
])
def test_provenance_observes_dirty_and_untracked_names_without_opening_them(
        monkeypatch, status, dirty, tracked, untracked):
    calls, read_paths = [], []
    original = Path.read_bytes

    def read(path):
        read_paths.append(path)
        return original(path)

    def git(args, **kwargs):
        calls.append(args)
        assert args[:4] == ["git", "--no-optional-locks", "-c", f"safe.directory={harness.ROOT}"]
        assert kwargs["cwd"] == harness.ROOT
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        return subprocess.CompletedProcess(args, 1 if status is None else 0, status or "", "")

    monkeypatch.setattr(harness.subprocess, "run", git)
    monkeypatch.setattr(Path, "read_bytes", read)
    result = harness.code_provenance()
    assert result["dirty"] is dirty and result["tracked_dirty"] is tracked
    assert result["untracked_present"] is untracked
    assert result["working_tree_status"] == ("unavailable" if status is None else "observed")
    assert len(calls) == 2 and "--untracked-files=normal" in calls[1]
    assert "code/simulator/devices.py" in result["file_sha256"]
    assert "code/contracts/models.py" in result["file_sha256"]
    assert all("scratch" not in str(path) and path.name != ".env" for path in read_paths)


@pytest.mark.parametrize("body", ['{"same":1,"same":2}', '{"value":NaN}', '[]', '{'])
def test_bad_json_rejected(tmp_path, body):
    path = tmp_path / "bad.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(harness.SpecError):
        harness.load_json(path)


def test_cli_returns_report_and_missing_input_and_preserves_existing_output(specs, cli_resources, capsys):
    tmp_path = cli_resources
    output = tmp_path / "outputs" / "result.json"
    args = ["--direct-only", "--resource-root", str(tmp_path), "--output", str(output)]
    assert tmp_path.resolve().is_relative_to(ROOT / "Work_tree/artifacts/acceptance-194")
    assert harness.main(args) == 0
    original = output.read_bytes()
    report = json.loads(original)
    assert report["sources"]["product_code"] == str(ROOT / "code")
    assert report["sources"]["inputs_sha256"] == harness.load_json(
        ROOT / "tests/scenarios/acceptance-inputs.json")[1]
    assert harness.main(args) == 2
    assert output.read_bytes() == original
    assert harness.main(["--resource-root", str(tmp_path), "--inputs", str(tmp_path / "missing.json")]) == 2
    assert "input/output error" in capsys.readouterr().err


def test_actual_command_line_unsupported_case_exits_three_and_writes_report(cli_resources):
    tmp_path = cli_resources
    output = tmp_path / "outputs" / "cli.json"
    completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/run_acceptance.py"),
                                "--case", "R08-full-docs-vs-keyword", "--resource-root", str(tmp_path),
                                "--output", str(output)], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 3, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["executed"] == 0
    assert report["summary"]["acceptance_not_run"] == report["summary"]["registered"]


def test_recursive_projection_leak_fails_without_reading_any_expected_in_product(specs, tmp_path, monkeypatch):
    from simulator import world
    inputs, expected = specs
    original = world.public_state

    def leak(world_state):
        result = original(world_state)
        result["snapshot"]["nested"] = [{"future_path": [1, 2]}]
        return result

    monkeypatch.setattr(world, "public_state", leak)
    report, code = harness.evaluate(inputs, expected, selected={"V03-public-projection"}, resource_root=tmp_path)
    assert code == 1
    attempt = row_for(report, "V03-public-projection")["attempts"][0]
    assert attempt["actual"]["private_fields_absent"] is False
    assert attempt["evidence"]["private_paths"] == ["/snapshot/nested/0/future_path"]


@pytest.fixture(scope="module")
def native_calibration(tmp_path_factory):
    inputs, _ = harness.load_json(ROOT / "tests/scenarios/acceptance-inputs.json")
    expected, _ = harness.load_json(ROOT / "tests/expected/acceptance-cases.json")
    root = tmp_path_factory.mktemp("native-calibration")
    report, code = harness.evaluate(inputs, expected, native_l3=True,
        selected={case["id"] for case in inputs["l3_cases"]}, resource_root=root)
    return inputs, expected, report, code


def test_native26_calibration_keeps_declarations_denominators_and_partial_limits(native_calibration):
    inputs, expected, report, code = native_calibration
    assert code == 3
    s = report["summary"]
    assert (s["registered"], s["native_registered"], s["native_executed"]) == (194, 26, 26)
    assert (s["native_matched"], s["native_partial"], s["native_failed"]) == (15, 11, 0)
    assert s["native_checks_not_observable"] == 11
    assert s["passed"] == s["acceptance_passed"] == 0 and s["acceptance_not_run"] == 194
    assert report["actual_provider_calls"] == 0 and not report["full_acceptance"]
    assert report["pending_extension_inputs"]["l3_case_defaults"] == inputs["l3_case_defaults"]
    assert report["pending_extension_inputs"]["l3_cases"] == inputs["l3_cases"]
    attempts = [a for r in report["cases"] for a in r["attempts"]]
    assert {a["seed"] for a in attempts} == set(range(12001, 12027))
    assert {a["declared_final_seed"] for a in attempts} == set(range(92001, 92027))
    assert all(a["seed_mapping"]["offset"] == -80000 and a["reference_unit_seed"] in range(61, 69) for a in attempts)
    assert all(a["scratch_cleaned"] and not Path(a["scratch_path"]).exists() for a in attempts)
    assert report["resources"]["all_scratch_cleaned"]
    for a in attempts:
        e = a["evidence"]
        assert e["seed"] == a["seed"] and e["tick"] == a["tick"]
        assert len(e["tick_times"]) == a["tick"] and e["sim_time_ms"] == a["tick"] * 100
        assert all(t["after_ms"] - t["before_ms"] == 100 for t in e["tick_times"])
        assert {c["check"] for c in a["checks"]} == set(expected["l3_assertion_rules"][
            expected["l3_direct_assertions"][next(r["id"] for r in report["cases"] if a in r["attempts"])]["rule"]]["checks"])


def test_native26_deadline_and_partial_checks_are_not_passes(native_calibration):
    _, _, report, _ = native_calibration
    for scene in ("s1a", "s1b", "s1c"):
        delay = row_for(report, f"T04-{scene}-delayed-will-move")["attempts"][0]
        assert delay["status"] == "native_partial" and delay["evidence"]["sim_time_ms"] == 900
        checks = {c["check"]: c["status"] for c in delay["checks"]}
        assert checks == {"at_tick_9_pose_equals_initial": "observed", "action_queued_at_tick_9": "observed",
                          "movement_only_after_deadline": "not_observable"}
        for response in ("silent", "acknowledged", "cannot-move", "question"):
            a = row_for(report, f"T04-{scene}-{response}")["attempts"][0]
            assert a["status"] == ("native_matched" if scene == "s1b" else "native_partial")
            assert next(c for c in a["checks"] if c["check"] == "no_resolution_claim")["status"] == (
                "observed" if scene == "s1b" else "not_observable")


def test_receipt_native_business_requires_a_real_current_incident_and_separates_synthetic_reaction(native_calibration):
    _, _, report, _ = native_calibration
    for scene in ("s1a", "s1b", "s1c"):
        for response in ("silent", "acknowledged", "cannot-move", "question"):
            attempt = row_for(report, f"T04-{scene}-{response}")["attempts"][0]
            evidence = attempt["evidence"]
            timeline = evidence["business_history"]
            assert [s["tick_ms"] for s in timeline if s["phase"] == "advance"] == list(range(100, 2501, 100))
            final = timeline[-1]
            assert final["phase"] == "final" and final["run_id"] == evidence["run_id"]
            assert final["object_id"] == "obj-car-02"
            assert final["notifications"] == [] and final["responses"] == []
            assert final["reaction_injection_source"].startswith("simulator.queue_vehicle_response")
            assert evidence["injection"]["response"] == (None if response == "silent" else response.replace("-", "_"))
            assert evidence["runtime_dependency_contract"] == "runtime-business-evidence-v2"
            if scene == "s1b":
                creation = evidence["created_incident"]
                assert creation and creation["created_at_ms"] == 2000
                assert evidence["incident_attempt"]["status"] == "created"
                assert creation["run_id"] == evidence["run_id"]
                assert final["incidents"] == [{"incident_id": creation["incident_id"],
                    "run_id": evidence["run_id"], "primary_object_id": "obj-car-02",
                    "status": "active", "resource_version": 1}]
                assert final["executions"] == [{"execution_id": creation["execution_id"],
                    "incident_id": creation["incident_id"], "run_id": evidence["run_id"],
                    "tool_name": "create_or_update_incident", "status": "succeeded"}]
            else:
                assert evidence["created_incident"] is None and final["incidents"] == []
                assert final["analysis"]["violation_candidate"] is False
                assert evidence["incident_attempt"]["error_code"] == "OBSERVATION_NOT_READY"
                assert evidence["incident_attempt"]["at_ms"] == 2500


@pytest.mark.parametrize("tamper", ["missing_creation", "missing_row", "wrong_run", "closed_status",
                                    "missing_execution", "missing_timeline", "missing_attempt"])
def test_receipt_business_evaluator_rejects_false_resolution_proof(native_calibration, tmp_path, tamper):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1b-silent"
    original = row_for(report, name)["attempts"][0]
    evidence = deepcopy(original["evidence"])
    final = evidence["business_history"][-1]
    if tamper == "missing_creation":
        evidence["created_incident"] = None
    elif tamper == "missing_row":
        final["incidents"] = []
    elif tamper == "wrong_run":
        final["incidents"][0]["run_id"] = "run-another"
    elif tamper == "closed_status":
        final["incidents"][0]["status"] = "resolved"
    elif tamper == "missing_execution":
        final["executions"] = []
    elif tamper == "missing_attempt":
        evidence["incident_attempt"] = None
    else:
        evidence["business_history"] = []
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), evidence), resource_root=tmp_path)
    assert code == 1 and row_for(result, name)["status"] == "failed"
    check = next(c for c in row_for(result, name)["attempts"][0]["checks"]
                 if c["check"] == "no_resolution_claim")
    assert check["status"] == "failed"


def test_receipt_business_evaluator_does_not_accept_incident_without_candidate(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1a-silent"
    original = row_for(report, name)["attempts"][0]
    evidence = deepcopy(original["evidence"])
    evidence["created_incident"] = {"incident_id": "incident-false"}
    evidence["business_history"][-1]["incidents"] = [{"incident_id": "incident-false", "status": "active"}]
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), evidence), resource_root=tmp_path)
    assert code == 1 and row_for(result, name)["status"] == "failed"


def test_receipt_business_evaluator_requires_actual_rejection_when_no_candidate(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1c-silent"
    original = row_for(report, name)["attempts"][0]
    evidence = deepcopy(original["evidence"])
    evidence["incident_attempt"] = None
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), evidence), resource_root=tmp_path)
    assert code == 1 and row_for(result, name)["status"] == "failed"


@pytest.mark.parametrize("name,missing_phase", [
    ("T04-s1b-silent", "initialized"), ("V02-s1a-pending-restart", "reopened")])
def test_native_dependency_checkpoints_cannot_be_dropped(native_calibration, tmp_path, name, missing_phase):
    inputs, expected, report, _ = native_calibration
    original = row_for(report, name)["attempts"][0]
    evidence = deepcopy(original["evidence"])
    evidence["runtime_dependencies"] = [s for s in evidence["runtime_dependencies"]
                                        if s["phase"] != missing_phase]
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), evidence), resource_root=tmp_path)
    attempt = row_for(result, name)["attempts"][0]
    assert code == 1 and attempt["status"] == "error"
    assert attempt["error_type"] == "RuntimeError" and "missing runtime dependency checkpoint" in attempt["error"]


def test_native_dependency_contract_cannot_be_relabelled(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1b-silent"
    original = row_for(report, name)["attempts"][0]
    evidence = deepcopy(original["evidence"])
    evidence["runtime_dependency_contract"] = "runtime-native-dependencies-v1"
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), evidence), resource_root=tmp_path)
    attempt = row_for(result, name)["attempts"][0]
    assert code == 1 and attempt["status"] == "failed"
    assert any(d["field"] == "dependency/contract" for d in attempt["mismatches"])


def test_native26_runtime_restart_uses_12_advances_and_separate_real_dependencies(native_calibration):
    _, _, report, _ = native_calibration
    names = [f"V02-{s}-pending-restart" for s in ("s1a", "s1b", "s1c")] + ["T23-unknown-gate-restart"]
    for name in names:
        a = row_for(report, name)["attempts"][0]
        assert a["status"] == "native_matched"
        e = a["evidence"]
        assert e["fixture_comparison_scope"] == "world-map-configuration-behavior"
        assert set(e["actual_versions"]) == set(harness.WORLD_VERSION_KEYS)
        assert e["tick"] == 12 and e["sim_time_ms"] == 1200
        paused = next(s for s in e["schedule"] if s["action"] == "paused_tick_probe")
        assert paused["before_ms"] == paused["after_ms"] == (100 if name.startswith("T23") else 200)
        resumed = next(s for s in e["schedule"] if s["action"] == "explicit_resume")
        assert 12 - resumed["tick"] == (11 if name.startswith("T23") else 10)
        assert a["runtime_dependency_comparison"] == "matched"
        assert [s["phase"] for s in e["runtime_dependencies"]] == ["initialized", "reopened", "final"]
        for s in e["runtime_dependencies"]:
            assert s["database_exists"] and Path(s["database"]).is_absolute()
            assert not Path(s["database"]).exists() and not Path(s["index_directory"]).exists()
            assert s["policies"][0]["policy_version"] == 2
            assert s["releases"][0]["knowledge_release_id"] == "knowledge-demo-v2"
            assert s["releases"][0]["file_digest"] == s["releases"][0]["index_digest"]
    e = row_for(report, "T23-unknown-gate-restart")["attempts"][0]["evidence"]
    assert [(s["tick"], s["key"]) for s in e["schedule"] if "key" in s] == [
        (0, "native-notice"), (1, "native-deny"), (1, "native-sensor"),
        (1, "native-close"), (1, "native-unknown"), (12, "native-blind-retry")]


@pytest.mark.parametrize("mutation", [
    lambda e: e["runtime_dependencies"][1]["policies"][0].update(policy_version=999),
    lambda e: e["runtime_dependencies"][1]["releases"][0].update(file_digest="sha256:wrong"),
    lambda e: e["runtime_dependencies"][1]["documents"].clear(),
    lambda e: e["runtime_dependencies"][1]["world_versions"].update(map_version="wrong"),
    lambda e: e["actual_versions"].update(configuration_version="wrong"),
])
def test_native_dependency_or_world_mismatch_fails_not_partial(native_calibration, tmp_path, mutation):
    inputs, expected, report, _ = native_calibration
    name = "V02-s1a-pending-restart"
    original = row_for(report, name)["attempts"][0]
    def corrupt(**kwargs):
        evidence = deepcopy(original["evidence"])
        mutation(evidence)
        return deepcopy(original["actual"]), evidence
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
                                    native_runner_fn=corrupt, resource_root=tmp_path)
    assert code == 1 and row_for(result, name)["status"] == "failed"
    assert result["summary"]["native_matched"] == result["summary"]["native_partial"] == 0


def test_native_missing_evidence_is_error_not_observation_limit(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1a-silent"
    original = row_for(report, name)["attempts"][0]
    def missing(**kwargs):
        evidence = deepcopy(original["evidence"])
        del evidence["history"][0]["pose"]
        return {}, evidence
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
                                    native_runner_fn=missing, resource_root=tmp_path)
    assert code == 1 and row_for(result, name)["status"] == "error"
    assert result["resources"]["all_scratch_cleaned"]


@pytest.mark.parametrize("exception,code,status", [(RuntimeError("native failure"), 1, "error"),
                                                  (KeyboardInterrupt(), 130, "interrupted")])
def test_native_error_or_interrupt_cleans_scratch_and_never_passes(specs, tmp_path, exception, code, status):
    inputs, expected = specs
    paths = []
    def fail(**kwargs):
        assert set(kwargs) == {"fixture", "seed", "tick", "injection", "scratch"}
        paths.append(kwargs["scratch"])
        (kwargs["scratch"] / "partial.db").write_text("partial")
        raise exception
    report, actual_code = harness.evaluate(inputs, expected, native_l3=True,
        selected={c["id"] for c in inputs["l3_cases"]}, native_runner_fn=fail, resource_root=tmp_path)
    assert actual_code == code
    assert row_for(report, "T04-s1a-silent")["status"] == status
    assert all(not p.exists() for p in paths)
    assert report["summary"]["native_matched"] == report["summary"]["acceptance_passed"] == 0


def test_native_final_seed_mapping_without_using_final_quality_as_calibration(specs, tmp_path):
    inputs, expected = specs
    captured = []
    def capture(**kwargs):
        captured.append(kwargs["seed"])
        assert set(kwargs) == {"fixture", "seed", "tick", "injection", "scratch"}
        raise RuntimeError("seed contract spy; not final execution")
    report, code = harness.evaluate(inputs, expected, split="final", native_l3=True,
        selected={c["id"] for c in inputs["l3_cases"]}, native_runner_fn=capture, resource_root=tmp_path)
    assert code == 1 and set(captured) == set(range(92001, 92027))
    assert all(a["seed_mapping"]["offset"] == 0 for r in report["cases"] for a in r["attempts"])


def test_native_cli_partial_exit_and_metadata(cli_resources):
    output = cli_resources / "outputs/native.json"
    assert harness.main(["--native-l3-only", "--resource-root", str(cli_resources), "--output", str(output)]) == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["native_executed"] == 26
    assert report["summary"]["native_partial"] == 11 and report["summary"]["native_matched"] == 15
    assert "code/backend/runtime.py" in report["code_provenance"]["file_sha256"]
    assert "data/samples/operating_knowledge/manifest.json" in report["code_provenance"]["file_sha256"]
    assert harness.main(["--native-l3-only", "--direct-only", "--resource-root", str(cli_resources)]) == 2


@pytest.mark.parametrize("exception,code", [(RuntimeError("after reopen"), 1), (KeyboardInterrupt(), 130)])
def test_native_runtime_failure_after_reopen_closes_both_store_handles(specs, tmp_path, monkeypatch, exception, code):
    import sqlite3
    from backend.runtime import Runtime
    inputs, expected = specs
    handles = []
    original = Runtime.__init__
    def observe_init(self, database):
        original(self, database)
        handles.append(self.store.db)
    async def fail_tick(self):
        raise exception
    monkeypatch.setattr(Runtime, "__init__", observe_init)
    monkeypatch.setattr(Runtime, "tick", fail_tick)
    report, actual = harness.evaluate(inputs, expected, native_l3=True,
        selected={"V02-s1a-pending-restart"}, resource_root=tmp_path)
    assert actual == code and len(handles) == 2
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert report["resources"]["all_scratch_cleaned"]
    assert list((tmp_path / "tmp").iterdir()) == []


def test_native_early_movement_fails_instead_of_becoming_partial(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1a-delayed-will-move"
    original = row_for(report, name)["attempts"][0]
    def early(**kwargs):
        evidence = deepcopy(original["evidence"])
        next(s for s in evidence["history"] if s["phase"] == "advance")["pose"][0] += 1
        return {}, evidence
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
                                    native_runner_fn=early, resource_root=tmp_path)
    assert code == 1
    check = next(c for c in row_for(result, name)["attempts"][0]["checks"] if c["check"] == "movement_only_after_deadline")
    assert check["status"] == "failed"


def test_native_missing_intermediate_advance_cannot_pass(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T05-s1b-mid-route-obstacle"
    original = row_for(report, name)["attempts"][0]
    def missing(**kwargs):
        evidence = deepcopy(original["evidence"])
        evidence["history"] = [s for s in evidence["history"] if not (s["phase"] == "advance" and s["tick"] == 16)]
        return {}, evidence
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name},
                                    native_runner_fn=missing, resource_root=tmp_path)
    assert code == 1
    assert any(d["field"] == "native/tick-accounting" for d in row_for(result, name)["attempts"][0]["mismatches"])


def test_native_observable_subset_exit_zero_remains_whole_acceptance_zero(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T05-s1b-mid-route-obstacle"
    original = row_for(report, name)["attempts"][0]
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name}, resource_root=tmp_path,
        native_runner_fn=lambda **kwargs: (deepcopy(original["actual"]), deepcopy(original["evidence"])))
    assert code == 0 and result["summary"]["native_matched"] == 1
    assert result["summary"]["acceptance_passed"] == 0 and not result["full_acceptance"]


def test_native_runner_cannot_change_declared_controls(native_calibration, tmp_path):
    inputs, expected, report, _ = native_calibration
    name = "T04-s1a-silent"
    original = row_for(report, name)["attempts"][0]
    def wrong_seed(**kwargs):
        evidence = deepcopy(original["evidence"])
        evidence["seed"] += 1
        return {}, evidence
    result, code = harness.evaluate(inputs, expected, native_l3=True, selected={name}, resource_root=tmp_path,
                                    native_runner_fn=wrong_seed)
    assert code == 1
    assert any(d["field"] == "controls/seed" for d in row_for(result, name)["attempts"][0]["mismatches"])


@pytest.mark.parametrize("reference", ["../sentinel.manual.json", "unlisted.manual.json", "absolute"])
def test_native_dependency_rejects_nonallowlisted_reference_before_content_read(tmp_path, monkeypatch, reference):
    from backend import knowledge
    manifest, _ = harness.load_json(knowledge.DEFAULT_MANIFEST)
    sentinel = tmp_path / "sentinel.manual.json"
    sentinel.write_text('{"public_test_sentinel": true}', encoding="utf-8")
    public = tmp_path / "public"
    public.mkdir()
    source = public / "manifest.json"
    manifest["documents"][0]["file"] = str(sentinel.resolve()) if reference == "absolute" else reference
    source.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(knowledge, "DEFAULT_MANIFEST", source)
    reads = []
    original_read = Path.read_bytes
    def read(path):
        reads.append(path)
        assert path == source, "a rejected reference reached a content read"
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(harness.SpecError, match="allowlisted public manual filename"):
        harness.compare_runtime_dependencies([])
    assert reads == [source]


def test_native_dependency_rejects_escaped_resolved_path_before_content_read(tmp_path, monkeypatch):
    from backend import knowledge
    manifest, _ = harness.load_json(knowledge.DEFAULT_MANIFEST)
    sentinel = tmp_path / "sentinel.manual.json"
    sentinel.write_text('{"public_test_sentinel": true}', encoding="utf-8")
    public = tmp_path / "public"
    public.mkdir()
    source = public / "manifest.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    candidate = public / manifest["documents"][0]["file"]
    assert candidate.name in harness.MANUAL_FILES
    original_resolve, original_read = Path.resolve, Path.read_bytes
    # Simulate a symlink/junction target without requiring Windows link privileges.
    def resolve(path, *args, **kwargs):
        return sentinel if path == candidate else original_resolve(path, *args, **kwargs)
    reads = []
    def read(path):
        reads.append(path)
        assert path == source, "escaped resolved path reached a content read"
        return original_read(path)
    monkeypatch.setattr(knowledge, "DEFAULT_MANIFEST", source)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(harness.SpecError, match="resolves outside its directory"):
        harness.compare_runtime_dependencies([])
    assert reads == [source]
