"""Coverage cannot turn regression links or matching subchecks into acceptance."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('coverage_runner', ROOT / 'scripts/report_acceptance_coverage.py')
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)
harness = coverage.harness


@pytest.fixture
def suite():
    inputs, ih = harness.load_json(ROOT / 'tests/scenarios/acceptance-inputs.json')
    expected, eh = harness.load_json(ROOT / 'tests/expected/acceptance-cases.json')
    registry = harness.validate_specs(inputs, expected)
    provenance = {'file_sha256': {'code/backend/runtime.py': 'unchanged-product'},
                  'baseline_product_tree_matches': True}
    hashes = {'inputs_sha256': ih, 'expected_sha256': eh}
    # A deliberately unexecuted report isolates ledger tests from product runs.
    baseline = {'schema_version': 'acceptance-report-v1', 'suite_version': inputs['suite_version'],
                'split': 'final', 'repeat': 1, 'actual_provider_calls': 0, 'sources': hashes,
                'summary': {'registered': 194, 'executed': 0, 'acceptance_passed': 0},
                'code_provenance': provenance, 'cases': [
                    {'id': case, 'selected': True, 'status': 'unsupported', 'attempts': [],
                     'independent_acceptance_expected': expected['variant_expected'][case],
                     'criteria': expected['criteria'][data['group']]} for case, data in registry.items()]}
    return inputs, expected, baseline, hashes, provenance


def evaluate(suite, tmp_path, **kwargs):
    inputs, expected, baseline, hashes, provenance = suite
    return coverage.evaluate_coverage(inputs, expected, baseline, source_hashes=hashes,
                                     provenance=provenance, resource_root=tmp_path, **kwargs)


def test_all_ids_classified_without_running_or_accepting_links(suite, tmp_path):
    suite[2]['cases'][0]['linked_nodeids'] = ['tests/fake.py::test_passed']
    report, code = evaluate(suite, tmp_path, execute_local=False)
    assert code == 3 and len(report['cases']) == 194
    assert report['summary']['blocked'] == 194 and report['summary']['complete'] == 0
    assert report['summary']['acceptance_passed'] == 0 and not report['full_acceptance']
    assert report['summary']['local_executed'] == 0
    rows = {r['id']: r for r in report['cases']}
    assert any(r['category'] == 'upstream_result' for r in rows['R08-full-docs-vs-keyword']['missing'])
    assert any(r['category'] == 'external_dependency' for r in rows['V05b-keyboard']['missing'])
    assert any(r['category'] == 'conditions_need_definition' for r in rows['T02-yield-then-move']['missing'])
    assert rows['T01-blocked']['expected_fields_unmeasured'] == ['decision']


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(split='calibration'),
    lambda r: r['sources'].update(expected_sha256='changed'),
    lambda r: r['cases'].append(deepcopy(r['cases'][0])),
    lambda r: r['cases'][0].update(selected=False),
    lambda r: r['cases'][0].update(independent_acceptance_expected={'decision': 'invented'}),
    lambda r: r['code_provenance']['file_sha256'].update({'code/backend/runtime.py': 'changed'}),
    lambda r: r.update(actual_provider_calls=1),
])
def test_untrusted_baseline_rejected_before_product_execution(suite, tmp_path, mutation):
    # Copies prevent corrupting the independent current hashes/provenance.
    inputs, expected, baseline, hashes, provenance = suite
    baseline = deepcopy(baseline)
    mutation(baseline)
    def forbidden(**kwargs):
        pytest.fail('invalid baseline reached product')
    with pytest.raises(harness.SpecError):
        coverage.evaluate_coverage(inputs, expected, baseline, source_hashes=hashes,
                                   provenance=provenance, resource_root=tmp_path, runner=forbidden)
    assert not (tmp_path / 'tmp').exists()


@pytest.mark.parametrize('actual,flag,status', [(False, True, 'failed'), (True, False, 'partial')])
def test_runner_flags_recomputed_and_matching_probe_stays_partial(suite, tmp_path, actual, flag, status):
    def fake(**args):
        return {'controls': args['controls'], 'provider_calls': 0,
                'checks': [{'name': 'subcheck', 'actual': actual, 'expected': True, 'matched': flag}]}
    report, code = evaluate(suite, tmp_path, runner=fake, selected_local=['T24-unknown-zone'])
    row = next(r for r in report['cases'] if r['id'] == 'T24-unknown-zone')
    assert row['coverage_status'] == status and row['acceptance_status'] == 'not_established'
    assert code == (1 if status == 'failed' else 3)
    assert report['summary']['complete'] == report['summary']['acceptance_passed'] == 0
    assert row['local_probe']['scratch_cleaned']
    assert not list((tmp_path / 'tmp').iterdir())


@pytest.mark.parametrize('case', ['T01-blocked', 'T01-existing-request', 'T04-will-move', 'T07-unregistered', 'T07-no-link',
                                  'T09-same-key-different-args', 'T09-different-key-same-work',
                                  'T13-one-of-two-alarms', 'T11-during-close',
                                  'T11-obstacle-before-close', 'T04-acknowledged',
                                  'R01-s1b-procedure', 'R04-utc-end', 'R04-restart-old-proof',
                                  'V02-storage-error', 'V04-invalid-unit'])
def test_representative_real_probe_returns_actual_predicates(suite, tmp_path, case):
    plan = harness.local_probe_plans(suite[0])[case]
    evidence = harness.run_local_probe(case_id=case, controls=plan, scratch=tmp_path)
    assert evidence['controls'] == plan and evidence['provider_calls'] == 0
    assert evidence['checks'] and all(c['actual'] == c['expected'] for c in evidence['checks'])
    assert evidence['decision_semantics'] == 'not_evaluated'


def test_frozen_controls_and_rag_questions_never_change(suite):
    inputs = suite[0]
    before = deepcopy(inputs)
    plans = harness.local_probe_plans(inputs)
    assert inputs == before
    assert plans['T01-blocked']['seed'] == 91001 and plans['T01-blocked']['tick'] == 60
    assert plans['R01-s1b-procedure']['seed'] == 93002
    assert plans['R01-s1b-procedure']['fixture'] == 's1b-blocked-v1'
    assert plans['R01-closing']['role'] == 'owner'
    assert plans['T04-will-move']['condition_authority'] == 'supplemental_not_adopted_final'
    assert not any(case.startswith(('R08-', 'V05a-')) for case in plans)


def test_baseline_paths_do_not_override_frozen_hashes(suite, tmp_path):
    suite[2]['sources']['product_code'] = str(ROOT / 'code')
    report, code = evaluate(suite, tmp_path, execute_local=False)
    assert code == 3 and report['summary']['classified'] == 194


def test_changed_unhashed_product_tree_rejects_reuse(suite, tmp_path):
    suite[4]['baseline_product_tree_matches'] = False
    with pytest.raises(harness.SpecError, match='entire tracked'):
        evaluate(suite, tmp_path, execute_local=False)


@pytest.mark.parametrize('field,value', [('fixture', 'other-fixture'), ('injection', {'invented': True}),
                                        ('tick', -1), ('seed', -1)])
def test_native_controls_are_checked_beyond_attempt_seed(suite, tmp_path, field, value):
    item = suite[0]['l3_cases'][0]
    row = next(r for r in suite[2]['cases'] if r['id'] == item['id'])
    row['attempts'] = [{'seed': item['seed'], 'declared_final_seed': item['seed'], 'tick': item['tick'],
                        'repeat': 1, 'mode': 'mock', 'split': 'final', 'scratch_cleaned': True,
                        'runner': harness.NATIVE_RUNNER_VERSION,
                        'evidence': {k: deepcopy(item[k]) for k in ('fixture', 'injection', 'tick', 'seed')}}]
    row['attempts'][0]['evidence'][field] = value
    with pytest.raises(harness.SpecError, match='fixture/injection/runner'):
        evaluate(suite, tmp_path, execute_local=False)


@pytest.mark.parametrize('matched', [False, True])
def test_upstream_reference_requires_exact_frozen_condition_and_stays_partial(suite, tmp_path, matched):
    from hashlib import sha256
    report, _ = evaluate(suite, tmp_path, execute_local=False)
    q = next(q for q in suite[0]['rag_comparison']['questions'] if q['id'].startswith('R08-'))
    attempt = {'corpus': 'sim0', 'question': q['query'], 'seed': q['seed'] if matched else -1,
               'fixture': q['fixture'], 'role': q['role'], 'topic': q.get('topic'), 'method': 'keyword_rag', 'attempt': 1}
    upstream = {'version': 'acceptance-rag-comparison-v1', 'provider_calls': 0, 'attempts': [attempt],
                'source_provenance': {'git_commit': 'separate-source'}, 'frozen_sha256': {
                    key: sha256((ROOT / relative).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
                    for key, relative in [('inputs', 'tests/scenarios/acceptance-inputs.json'),
                                           ('expected', 'tests/expected/acceptance-cases.json')]}}
    coverage.attach_upstream_rag(report, upstream, path='reference.json', digest='recorded', inputs=suite[0], expected=suite[1])
    row = next(r for r in report['cases'] if r['id'] == q['id'])
    assert row['upstream_reference']['matching_condition_attempts'] == int(matched)
    assert any(r['category'] == ('upstream_partial_remaining' if matched else 'upstream_result') for r in row['missing'])
    assert row['coverage_status'] == 'blocked' and row['acceptance_status'] == 'not_established'
    assert report['summary']['acceptance_passed'] == 0 and not report['upstream_rag_summary']['acceptance_transfer']
