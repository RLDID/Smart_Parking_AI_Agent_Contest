"""Native supplemental measurements; no provider credentials/shared DB/socket server."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceptance_native_extensions', ROOT / 'scripts/run_acceptance.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture(scope='module')
def plans():
    inputs, _ = runner.load_json(ROOT / 'tests/scenarios/acceptance-inputs.json')
    return runner.local_probe_plans(inputs)


def assert_matched(evidence):
    assert evidence['checks']
    assert [r for r in evidence['checks'] if not r['matched']] == []
    assert evidence['provider_calls'] == 0
    assert isinstance(evidence['end_sim_time_ms'], int)
    assert evidence['observed_clock_endpoints']
    assert evidence['measurement_kind']


@pytest.mark.parametrize('case', [
    'T08-timeout', 'T08-tool-error', 'T08-budget-limit', 'V05a-event-budget',
    'V05a-daily-budget', 'V05a-time-limit', 'V05a-call-limit', 'V05a-concurrent-events'])
def test_native_product_cost_bounds_and_failure(case, plans, tmp_path):
    evidence = runner.run_local_probe(case_id=case, controls=plans[case], scratch=tmp_path)
    assert_matched(evidence)
    assert 'product_live_read_adapter' in evidence['layers'] or 'product_budget_ledger' in evidence['layers']
    if case == 'T08-budget-limit':
        assert evidence['fake_provider_calls'] == 0
    if case == 'T08-timeout':
        assert evidence['budget']['unknown_count'] == 1
        assert evidence['budget']['total_pending_krw'] == 3


def test_unknown_policy_conflict_is_not_silently_passed(plans, tmp_path):
    evidence = runner.run_local_probe(case_id='T08-consecutive-failures',
        controls=plans['T08-consecutive-failures'], scratch=tmp_path)
    failed = [c['name'] for c in evidence['checks'] if not c['matched']]
    assert failed == ['frozen_no_new_dispatch_after_unknown']
    assert evidence['contract_conflict']['actual_distinct_dispatches'] == 3
    assert all(evidence['current_contract_checks'].values())
    assert evidence['budget']['unknown_count'] == 3


@pytest.mark.parametrize('case', ['V04-process-separation', 'V04-delay-loss-duplicate'])
def test_actual_subprocess_contract_duplicate_and_recovery(case, plans, tmp_path):
    evidence = runner.run_local_probe(case_id=case, controls=plans[case], scratch=tmp_path)
    assert_matched(evidence)
    assert evidence['before'] == evidence['after']
    assert evidence['termination'] == {'worker_returncode': 0, 'invalid_returncode': 2}


@pytest.mark.parametrize('case', ['V03-public-projection', 'V03-model-payload', 'V03-api',
                                  'V03-search-index', 'R07-expected-path', 'R07-future-path'])
def test_measured_payloads_and_existing_forbidden_content(case, plans, tmp_path):
    evidence = runner.run_local_probe(case_id=case, controls=plans[case], scratch=tmp_path)
    assert_matched(evidence)
    if case.startswith('R07'):
        assert evidence['rejected_source']['file_existed']
        assert evidence['rejected_source']['content_sha256']
    elif case == 'V03-model-payload':
        assert evidence['fake_provider_calls'] == 1
        assert 'scenario' not in evidence['measured_payload'][0]['request']['context']


@pytest.mark.parametrize('case', ['R06-index-timeout', 'R06-search-timeout',
                                  'R06-result-lost', 'R06-independent-alarm'])
def test_native_rag_delay_loss_and_independent_path(case, plans, tmp_path):
    evidence = runner.run_local_probe(case_id=case, controls=plans[case], scratch=tmp_path)
    assert_matched(evidence)


def test_native_plans_keep_frozen_truth_and_unimplemented_bundle_separate(plans):
    inputs, input_digest = runner.load_json(ROOT / 'tests/scenarios/acceptance-inputs.json')
    expected, _ = runner.load_json(ROOT / 'tests/expected/acceptance-cases.json')
    runner.validate_frozen_inputs(inputs, expected)
    assert 'V03-bundle' not in plans
    original = next(p for p in inputs['direct'] if p['id'] == 'V03-public-projection')
    assert plans['V03-public-projection']['seed'] == original['seed']
    assert plans['V03-public-projection']['tick'] == original['tick']
    assert plans['T08-timeout']['model_timeout_s'] == .02
    assert plans['V05a-time-limit']['wall_s'] == .02
    assert plans['V04-process-separation']['process_timeout_s'] == 15


def test_silent_business_wall_deadline_report_and_no_motion(plans, tmp_path):
    evidence = runner.run_local_probe(case_id='T04-silent', controls=plans['T04-silent'], scratch=tmp_path)
    assert_matched(evidence)
    deadline = evidence['silent_deadline']
    assert deadline['offsets_ms'] == [-1, 1]
    assert deadline['response_timeout_wall_ms'] > 0
    assert deadline['samples'][0]['owner_reports'] == 0
    assert deadline['samples'][-1]['owner_reports'] == 1
    assert all(s['owner_reports'] <= 1 for s in deadline['samples'])
    assert all(s['move_requests'] <= deadline['contact_max_sequence'] for s in deadline['samples'])
    assert len(deadline['deadlines']) == deadline['contact_max_sequence']
    assert evidence['end_sim_time_ms'] == plans['T04-silent']['tick'] * 100
    assert evidence['observed_clock_endpoints']['controlled_wall_end_utc'] == deadline['samples'][-1]['wall_utc']


@pytest.mark.parametrize("case", ["R02-oversize-required-group", "R03-structured-policy-conflict", "R04-policy-replaced", "R04-index-failure", "R05-other-facility", "R07-authority-injection", "R07-external-link"])
def test_actual_private_release_transitions_and_boundaries(case, plans, tmp_path):
    evidence = runner.run_local_probe(case_id=case, controls=plans[case], scratch=tmp_path)
    assert_matched(evidence)
    assert evidence["initial_retrieval"]["status"] == "matched"
    assert not evidence["declaration_fully_satisfied"]
    assert evidence["semantic_judgment"] == "not_automatically_evaluated"
    if case == "R02-oversize-required-group":
        assert evidence["retrieval_after"]["reason_code"] == "incomplete_context"
        assert evidence["retrieval_after"]["references"] == []
    elif case == "R04-policy-replaced":
        assert evidence["proof_recheck"] == "KNOWLEDGE_CHANGED"
        assert evidence["retrieval_after"]["policy_version"] == 4
    elif case == "R03-structured-policy-conflict":
        assert evidence["policy_attempt"]["outcome"] == "rejected"
    elif case == "R05-other-facility":
        assert evidence["retrieval_after"]["references"] == []


@pytest.mark.parametrize("fault", [None, "risk_without_claim", "missing_device_claim", "premature_clear", "different_run"])
def test_safety_history_allows_sustained_clear_but_rejects_lost_alarm(fault):
    def sample(tick, status, claims):
        return {"run_id": "run-a", "sim_time_ms": tick, "analysis_status": status,
                "claims": claims, "device_claim_ids": list(claims)}
    rows = [sample(100, "risk_candidate", {"alarm-a": {"clear_since_ms": None}}),
            sample(200, "clear_projection", {"alarm-a": {"clear_since_ms": 200}}),
            sample(1200, "clear_projection", {})]
    if fault == "risk_without_claim":
        rows[0]["claims"] = {}
        rows[0]["device_claim_ids"] = []
    elif fault == "missing_device_claim":
        rows[0]["device_claim_ids"] = []
    elif fault == "premature_clear":
        rows[-1]["sim_time_ms"] = 1199
    elif fault == "different_run":
        rows[-1]["run_id"] = "run-b"
    result = runner._safety_history_result(rows, 1000)
    assert result["matched"] is (fault is None)
    assert bool(result["violations"]) is (fault is not None)
