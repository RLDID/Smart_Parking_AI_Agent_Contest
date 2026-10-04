"""Exhaustive 194-ID ledger plus isolated local probes; no inferred acceptance.

The immutable baseline report is reused only after source/ID/control checks.
Each new probe records its own controls, actual predicates and missing layers.
Exit 3 means remaining partial/blocked coverage, not a product failure.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('acceptance_coverage_harness', ROOT / 'scripts/run_acceptance.py')
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)
RESOURCE_ROOT = ROOT / 'Work_tree/artifacts/acceptance-194'


def remaining_reasons(case_id, inputs, has_measurement):
    """Conditions, runner support and external dependencies are distinct."""
    group = case_id.split('-', 1)[0]
    reasons = []
    if case_id in inputs['external_variants'] or group == 'V05b' or case_id.endswith('screen-execution-text'):
        reasons.append({'category': 'external_dependency',
                        'reason': 'Actual browser/client rendering, accessibility or separate process evidence required; outside assigned access.'})
    if group == 'R08':
        reasons.append({'category': 'upstream_result',
                        'reason': 'Method comparison and model repetition require a separate condition-matched result; not executed by this local adapter.'})
    if group == 'V05a' or case_id == 'T08-budget-limit':
        reasons.append({'category': 'upstream_contract',
                        'reason': 'This adapter does not measure exact budget acceptance variants. Optional-cap unit tests and live usage records are separate evidence, not whole-variant acceptance.'})
    detailed = {i['id'] for i in inputs['direct']} | {i['id'] for i in inputs['l3_cases']}
    rag_questions = {q['id'] for q in inputs['rag_comparison']['questions']}
    if case_id not in detailed | rag_questions:
        reasons.append({'category': 'conditions_need_definition',
                        'reason': 'v1 declares fixture and expected decision but no adopted per-case seed/tick/injection; supplemental controls are not frozen final.'})
    if has_measurement:
        reasons.append({'category': 'unmeasured_acceptance_layers',
                        'reason': 'Recorded subchecks do not cover every group allow/forbid/evidence criterion; decision semantics and missing layers remain unobserved.'})
    elif group not in ('R08', 'V05b'):
        reasons.append({'category': 'runner_unsupported',
                        'reason': 'No implemented direct/native/local adapter for this exact variant; linked nodeids are references, not execution results.'})
    if case_id == 'R03-semantic-conflict':
        reasons.append({'category': 'semantic_evaluation',
                        'reason': 'Metadata conflict flag checks do not detect unflagged natural-language contradiction; semantic miss evidence required.'})
    if case_id == 'T07-changed-link':
        reasons.append({'category': 'unmeasured_acceptance_layers',
                        'reason': 'Revoked old recipient is measured; establishing/contacting a new current recipient remains unmeasured.'})
    if case_id == 'V04-invalid-unit':
        reasons.append({'category': 'unmeasured_acceptance_layers',
                        'reason': 'Unexpected unit field rejection is measured; numeric unit semantic conversion is not demonstrated.'})
    details = {
        'T04-silent': 'Only immediate no-response reprocessing is measured; timeout elapsed and bounded followup are unmeasured.',
        'T05-observation-loss': 'Loss and unresolved incident are measured; prescribed review/report/followup semantics remain unmeasured.',
        'T24-allowed-zone': 'Receipt accepted is measured; simulated playback completion and browser output are unmeasured.',
        'V01-snapshot-race': 'Sequential snapshot/commit/stream lookup is measured; concurrent client snapshot/event race is unmeasured.',
        'V01-lost-event': 'Stored post-cursor event is measured; actual client receive loss and recovery are unmeasured.',
        'V02-recovery-required-no-motion': 'Recovery flag and paused no-motion are measured; explicit recovery/new observation/resume sequence is unmeasured.',
        'T20-duplicate-contact': 'Same S1-a work deduplication is measured; movement/new-impact and same-cause incident linkage are unmeasured.',
        'V03-search-index': 'Three forbidden path strings are absent; scenario/seed/scheduled events and content-level future leakage remain unmeasured.',
    }
    if has_measurement and case_id in details:
        reasons.append({'category': 'unmeasured_acceptance_layers', 'reason': details[case_id]})
    return reasons


def validate_baseline(baseline, inputs, expected, source_hashes, provenance):
    registry = harness.validate_specs(inputs, expected)
    harness.require(baseline.get('schema_version') == 'acceptance-report-v1', 'unsupported baseline report')
    harness.require(baseline.get('split') == 'final' and baseline.get('repeat') == 1, 'reuse requires final repeat1 baseline')
    harness.require(baseline.get('suite_version') == inputs['suite_version'], 'baseline suite mismatch')
    harness.require(all(baseline.get('sources', {}).get(k) == v for k, v in source_hashes.items()),
                    'baseline frozen source hashes mismatch')
    rows = baseline.get('cases', [])
    harness.require(len(rows) == len(registry) and {r['id'] for r in rows} == set(registry), 'baseline must contain every unique adopted ID')
    harness.require(all(r.get('selected') for r in rows), 'baseline must select all 194 cases')
    harness.require(baseline.get('actual_provider_calls') == 0, 'baseline provider budget must be zero')
    old_hashes = baseline.get('code_provenance', {}).get('file_sha256', {})
    product_paths = {p: v for p, v in provenance['file_sha256'].items() if p.startswith(('code/', 'data/')) or p == 'requirements.lock.txt'}
    harness.require(product_paths and all(old_hashes.get(p) == digest for p, digest in product_paths.items()),
                    'baseline product/dependency source changed; new run required')
    harness.require(provenance.get('baseline_product_tree_matches') is True,
                    'baseline entire tracked code/data/dependency tree must remain unchanged')
    direct = {i['id']: i for i in inputs['direct']}
    native = {i['id']: i for i in inputs['l3_cases']}
    for row in rows:
        harness.require(row.get('independent_acceptance_expected') == expected['variant_expected'][row['id']], 'baseline expected mismatch')
        harness.require(row.get('criteria') == expected['criteria'][registry[row['id']]['group']], 'baseline criteria mismatch')
        for attempt in row.get('attempts', []):
            item = (native if attempt.get('runner') == harness.NATIVE_RUNNER_VERSION else direct).get(row['id'])
            harness.require(item is not None and attempt.get('seed') == item['seed'] and attempt.get('tick') == item['tick'],
                            'baseline per-attempt final controls mismatch')
            harness.require(attempt.get('split') == 'final' and attempt.get('scratch_cleaned') is True,
                            'baseline attempt scope/cleanup mismatch')
            harness.require(attempt.get('repeat') == 1 and attempt.get('mode') == 'mock'
                            and attempt.get('declared_final_seed') == item['seed'], 'baseline repeat/mode/final seed mismatch')
            if row['id'] in native:
                evidence = attempt.get('evidence') or {}
                harness.require(attempt.get('runner') == harness.NATIVE_RUNNER_VERSION
                                and all(evidence.get(k) == item[k] for k in ('fixture', 'seed', 'tick', 'injection')),
                                'baseline native fixture/injection/runner mismatch')
    return registry


def evaluate_coverage(inputs, expected, baseline, *, source_hashes, provenance, resource_root=RESOURCE_ROOT,
                      execute_local=True, runner=None, selected_local=None):
    registry = validate_baseline(baseline, inputs, expected, source_hashes, provenance)
    plans = harness.local_probe_plans(inputs)
    if selected_local is not None:
        harness.require(set(selected_local) <= plans.keys(), 'unknown local probe selection')
        plans = {k: v for k, v in plans.items() if k in selected_local}
    runner = runner or harness.run_local_probe
    root = Path(resource_root).resolve()
    temp = root / 'tmp'
    temp.mkdir(parents=True, exist_ok=True)
    rows = []
    for base in baseline['cases']:
        row = deepcopy(base)
        case_id = row['id']
        row['baseline_status'] = row.pop('status')
        row['baseline_execution'] = bool(row['attempts'])
        row['local_probe'] = None
        plan = plans.get(case_id)
        if execute_local and plan is not None:
            started, scratch = perf_counter(), None
            local = {'controls': deepcopy(plan), 'status': 'started', 'evidence': None}
            try:
                with TemporaryDirectory(prefix='local194-', dir=temp) as scratch:
                    local['scratch_path'] = str(Path(scratch).resolve())
                    evidence = runner(case_id=case_id, controls=deepcopy(plan), scratch=Path(scratch))
                    harness.require(evidence.get('controls') == plan, 'runner controls differ from explicit plan')
                    harness.require(evidence.get('provider_calls') == 0, 'local provider calls prohibited')
                    checks = evidence.get('checks', [])
                    harness.require(checks and all(set(c) >= {'name', 'actual', 'expected', 'matched'} for c in checks), 'missing actual local predicates')
                    # Never trust a precomputed matched flag from a runner.
                    for check in checks:
                        check['matched'] = check['actual'] == check['expected']
                    if case_id in expected['rag_reference_expectations']['sim0_current']['groups']:
                        wanted = expected['rag_reference_expectations']['sim0_current']['groups'][case_id]
                        refs = evidence.get('search', {}).get('references', [])
                        actual_refs = sorted(r['reference_id'] for r in refs)
                        checks.append({'name': 'independent_required_references', 'actual': actual_refs,
                                       'expected': sorted(wanted['required_references']), 'comparison': 'required_subset',
                                       'matched': set(wanted['required_references']) <= set(actual_refs)})
                        if wanted.get('status') == 'no_match':
                            actual_status = evidence.get('search', {}).get('status')
                            checks.append({'name': 'independent_no_match', 'actual': [actual_status, actual_refs],
                                           'expected': ['no_match', []], 'matched': actual_status == 'no_match' and not actual_refs})
                        else:
                            actual_documents = sorted({(r['document_id'], r['document_version'], r['procedure_group_id']) for r in refs})
                            target = [(wanted['document_id'], wanted['document_version'], wanted['group'])]
                            checks.append({'name': 'independent_document_group_version', 'actual': actual_documents,
                                           'expected': target, 'comparison': 'required_subset', 'matched': all(value in actual_documents for value in target)})
                    local.update(evidence=evidence, status='matched_partial' if all(c['matched'] for c in checks) else 'failed')
            except KeyboardInterrupt:
                local.update(status='interrupted', reason='operator interrupted local probe')
            except Exception as error:
                local.update(status='error', error_type=type(error).__name__, reason=str(error))
            finally:
                local['wall_seconds'] = perf_counter() - started
                local['scratch_cleaned'] = scratch is None or not Path(scratch).exists()
                if not local['scratch_cleaned']:
                    local.update(status='error', reason='scratch cleanup failed')
            row['local_probe'] = local
        measured = row['baseline_execution'] or row['local_probe'] is not None
        failures = row['baseline_status'] in ('failed', 'error', 'interrupted') or (row['local_probe'] or {}).get('status') in ('failed', 'error', 'interrupted')
        row['coverage_status'] = 'failed' if failures else 'partial' if measured else 'blocked'
        # Local probes intentionally lack complete allow/forbid evidence and/or
        # adopted final controls. No code path can label them full acceptance.
        row['acceptance_status'] = 'not_established'
        row['missing'] = remaining_reasons(case_id, inputs, measured)
        row['expected_fields_measured'] = sorted({field for a in row['attempts'] for field in (a.get('actual') or {})}
                                               & set(expected['variant_expected'][case_id]))
        row['expected_fields_unmeasured'] = sorted(set(expected['variant_expected'][case_id]) - set(row['expected_fields_measured']))
        rows.append(row)
    counts = Counter(r['coverage_status'] for r in rows)
    local_results = [r['local_probe'] for r in rows if r['local_probe']]
    summary = {'registered': len(registry), 'classified': len(rows), 'complete': 0,
               'partial': counts['partial'], 'failed': counts['failed'], 'blocked': counts['blocked'],
               'baseline_executed': sum(r['baseline_execution'] for r in rows),
               'local_executed': len(local_results), 'local_matched_partial': sum(r['status'] == 'matched_partial' for r in local_results),
               'local_failed': sum(r['status'] == 'failed' for r in local_results), 'local_errors': sum(r['status'] == 'error' for r in local_results),
               'acceptance_passed': 0, 'acceptance_denominator': len(registry)}
    report = {'schema_version': 'acceptance-coverage-v1', 'suite_version': inputs['suite_version'],
              'split': 'final', 'actual_provider_calls': 0, 'full_acceptance': False, 'summary': summary,
              'sources': source_hashes, 'code_provenance': provenance, 'baseline_summary': baseline['summary'],
              'local_adapter_version': harness.LOCAL_PROBE_VERSION, 'local_conditions': harness.local_probe_plans(inputs),
              'resources': {'tmp': str(temp), 'all_scratch_cleaned': all(r['scratch_cleaned'] for r in local_results),
                            'server': 'not_started', 'browser': 'not_accessed', 'database': 'per_attempt_isolated'},
              'cases': rows}
    code = 1 if counts['failed'] else 3
    report['exit_code'] = code
    return report, code


def attach_upstream_rag(report, upstream, *, path, digest, inputs, expected):
    """Read/reference main's measured comparison; never promote it to acceptance."""
    harness.require(upstream.get('version') == 'acceptance-rag-comparison-v1', 'unknown upstream comparison')
    harness.require(upstream.get('provider_calls') == 0, 'upstream paid calls outside assigned scope')
    for key, relative in [('inputs', 'tests/scenarios/acceptance-inputs.json'),
                          ('expected', 'tests/expected/acceptance-cases.json')]:
        canonical = (ROOT / relative).read_bytes().replace(b'\r\n', b'\n')
        harness.require(upstream.get('frozen_sha256', {}).get(key) == sha256(canonical).hexdigest(),
                        'upstream frozen LF-normalized source pin mismatch')
    questions = {q['id']: q for q in inputs['rag_comparison']['questions']}
    source = upstream.get('source_provenance', {})
    for row in report['cases']:
        if not row['id'].startswith('R08-'):
            continue
        q = questions[row['id']]
        attempts = [a for a in upstream.get('attempts', []) if a.get('corpus') == 'sim0'
                    and a.get('question') == q['query'] and a.get('seed') == q['seed']
                    and a.get('fixture') == q['fixture'] and a.get('role') == q['role']
                    and a.get('topic') == q.get('topic')]
        row['upstream_reference'] = {'path': str(path), 'sha256': digest,
            'source_commit': source.get('git_commit'), 'source_commit_matches_local': source.get('git_commit') == report['code_provenance'].get('commit'),
            'scope': 'separate-source partial retrieval/structural comparison; not semantic support or business/live-model acceptance',
            'matching_condition_attempts': len(attempts),
            'methods': sorted({a['method'] for a in attempts}),
            'measured': [{k: a.get(k) for k in ('method', 'attempt', 'status', 'quality_match', 'condition_fingerprint',
                          'calls', 'wall_latency_ms', 'citation_structure')} for a in attempts]}
        if attempts:
            row['missing'] = [r for r in row['missing'] if r['category'] != 'upstream_result']
            row['missing'].append({'category': 'upstream_partial_remaining',
                'reason': f"Main supplied {len(attempts)} condition-matching retrieval attempts across repetitions {sorted({a['attempt'] for a in attempts})}. Semantic support, other business methods and live-model quality remain unmeasured; source differs from local final."})
        else:
            row['upstream_reference']['unmatched_condition_reason'] = 'No exact frozen question/seed/fixture/role/topic attempt in supplied report.'
    report['upstream_rag_summary'] = {'path': str(path), 'sha256': digest, 'source_commit': source.get('git_commit'),
        'provider_calls': upstream['provider_calls'], 'cleanup': upstream.get('cleanup'),
        'summary': [{k: v for k, v in r.items() if k != 'wall_latency_ms'} for r in upstream.get('summary', [])],
        'acceptance_transfer': False}


def write_markdown(report, path):
    lines = ['# 194-ID final coverage ledger', '', 'Whole acceptance: 0/194. Partial predicates never count as full acceptance.', '',
             '| ID | Classification | Baseline | New local probe | Remaining reasons |', '|---|---|---|---|---|']
    for row in report['cases']:
        local = (row['local_probe'] or {}).get('status', 'not_run')
        reasons = '; '.join(f"{r['category']}: {r['reason']}" for r in row['missing'])
        lines.append(f"| {row['id']} | {row['coverage_status']} | {row['baseline_status']} | {local} | {reasons} |")
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--resource-root', type=Path, default=RESOURCE_ROOT / 'extended')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ledger-only', action='store_true')
    parser.add_argument('--local-case', action='append', help='Run only named supplemental adapters, retaining every registered row.')
    parser.add_argument('--upstream-rag-report', type=Path)
    args = parser.parse_args(argv)
    try:
        root, output = args.resource_root.resolve(), args.output.resolve()
        harness.require(root.is_relative_to(RESOURCE_ROOT.resolve()), 'resources must stay in assigned acceptance-194 path')
        harness.require(output.is_relative_to(root) and not output.exists() and not output.with_suffix('.md').exists(), 'choose new output inside resource root')
        inputs, ih = harness.load_json(ROOT / 'tests/scenarios/acceptance-inputs.json')
        expected, eh = harness.load_json(ROOT / 'tests/expected/acceptance-cases.json')
        baseline, bh = harness.load_json(args.baseline_report)
        provenance = harness.code_provenance()
        source_commit = baseline.get('code_provenance', {}).get('commit')
        harness.require(isinstance(source_commit, str) and len(source_commit) == 40
                        and all(c in '0123456789abcdef' for c in source_commit), 'baseline source commit missing')
        check = subprocess.run(['git', '--no-optional-locks', '-c', f'safe.directory={ROOT}',
                                'diff', '--quiet', source_commit, '--', 'code', 'data', 'requirements.lock.txt'],
                               cwd=ROOT, capture_output=True, timeout=10)
        product_changes = [c for c in provenance['changes'] if c['path'].startswith(('code/', 'data/'))
                           or c['path'] == 'requirements.lock.txt']
        provenance['baseline_product_tree_matches'] = check.returncode == 0 and not product_changes
        provenance['baseline_tree_check_scope'] = 'Git diff against baseline commit for entire tracked code/data/lock, plus current untracked/dirty product path names; explicit hashes also checked.'
        provenance['file_sha256']['scripts/report_acceptance_coverage.py'] = sha256(Path(__file__).read_bytes()).hexdigest()
        report, code = evaluate_coverage(inputs, expected, baseline,
            source_hashes={'inputs_sha256': ih, 'expected_sha256': eh}, provenance=provenance,
            resource_root=root, execute_local=not args.ledger_only, selected_local=args.local_case)
        report['baseline_report'] = {'path': str(args.baseline_report.resolve()), 'sha256': bh}
        if args.upstream_rag_report:
            upstream, upstream_hash = harness.load_json(args.upstream_rag_report)
            attach_upstream_rag(report, upstream, path=args.upstream_rag_report.resolve(), digest=upstream_hash,
                                inputs=inputs, expected=expected)
        harness.write_report(report, output)
        write_markdown(report, output.with_suffix('.md'))
        print(json.dumps({'output': str(output), 'exit_code': code, **report['summary']}, ensure_ascii=False))
        return code
    except (harness.SpecError, OSError) as error:
        print(f'coverage input/output error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
