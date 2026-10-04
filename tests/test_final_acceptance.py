"""Final target/evidence gates. Fakes never invoke product/provider execution."""
import hashlib
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_final_acceptance as final


def actual_specs():
    return final.specs()


def provenance():
    return {"commit": "a" * 40, "working_tree_status": "observed", "changes": [], "file_sha256": {"code/backend/runtime.py": "current"}}


def test_original194_criteria_and_missing_ui_never_become_whole_pass():
    inputs, expected, registry = actual_specs()
    result = final.inventory(inputs, expected, registry, provenance())
    assert len(result["cases"]) == 194 and result["summary"]["whole_acceptance_passed"] == 0
    assert not result["full_acceptance"] and result["summary"]["fresh_executed"] == 0
    assert all(r["original_expected"] == expected["variant_expected"][r["id"]] for r in result["cases"])
    assert sum("actual_frontend_evidence" in r["missing"] for r in result["cases"]) == 55  # 54 E-UI + R07 exact screen requirement
    assert sum(r["condition_authority"] == "not_adopted" for r in result["cases"]) == 149


def test_partial_probe_even_with_all_checks_true_cannot_certify_original_variant():
    inputs, expected, registry = actual_specs()
    fresh = {"actual_provider_calls": 0, "cases": [{"id": name, "attempts": [{"status": "passed_partial", "checks": [{"matched": True}]}]} for name in registry]}
    result = final.inventory(inputs, expected, registry, provenance(), fresh=fresh)
    assert result["summary"]["coverage"] == {"partial": 194}
    assert result["summary"]["whole_acceptance_passed"] == 0
    assert all(r["acceptance_status"] == "not_established" for r in result["cases"])


@pytest.mark.parametrize("mutation", ["target", "dirty_product", "missing_status"])
def test_target_source_gate(mutation):
    value = provenance()
    if mutation == "target": value["commit"] = "b" * 40
    elif mutation == "dirty_product": value["changes"] = [{"path": "code/backend/runtime.py", "status": " M"}]
    else: value["working_tree_status"] = "unavailable"
    with pytest.raises(final.harness.SpecError): final.assert_target("a" * 40, value)


def test_new_report_resources_cannot_overwrite_or_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(final, "ARTIFACT_ROOT", tmp_path)
    outside = tmp_path.parent / "not-assigned.json"
    with pytest.raises(final.harness.SpecError): final.fresh_output(outside)
    run = tmp_path / "run"
    run.mkdir()
    output = run / "evidence.json"
    (run / "resources").mkdir()
    with pytest.raises(final.harness.SpecError): final.fresh_output(output)


def test_conditions_and_only_frozen_cases_gate_before_fake_execution(tmp_path, monkeypatch):
    root = tmp_path / "artifact"
    monkeypatch.setattr(final, "ARTIFACT_ROOT", root)
    monkeypatch.setattr(final.harness, "code_provenance", provenance)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("Not authorized to execute")
    with pytest.raises(final.harness.SpecError, match="conditions"):
        final.run(root / "run/a.json", target="a"*40, execute=True, runner=forbidden)
    declaration = tmp_path / "declared.md"
    declaration.write_text("before execution", encoding="utf-8")
    with pytest.raises(final.harness.SpecError, match="frozen"):
        final.run(root / "run/a.json", target="a"*40, execute=True, conditions=declaration,
                  selected=["V05b-keyboard"], runner=forbidden)
    assert calls == []


def test_fake_execute_preserves_original_final_seed_and_one_repeat(tmp_path, monkeypatch):
    root = tmp_path / "artifact"
    monkeypatch.setattr(final, "ARTIFACT_ROOT", root)
    monkeypatch.setattr(final.harness, "code_provenance", provenance)
    inputs, expected, registry = actual_specs()
    observed = []
    def fake_runner(actual_inputs, actual_expected, **kwargs):
        assert actual_inputs == inputs and actual_expected == expected
        observed.append(kwargs)
        return {"actual_provider_calls": 0, "cases": [{"id": name, "attempts": [{"status": "passed_partial"}] if name == "T01-blocked" else []} for name in registry]}, 3
    declaration = tmp_path / "declared.md"
    declaration.write_text("before execution", encoding="utf-8")
    result = final.run(root / "run/a.json", target="a"*40, execute=True, conditions=declaration,
                       selected=["T01-blocked"], runner=fake_runner)
    assert observed[0]["split"] == "final" and observed[0]["repeat"] == 1
    assert observed[0]["selected"] == ["T01-blocked"]
    assert result["summary"]["fresh_executed"] == 1 and result["summary"]["whole_acceptance_passed"] == 0


def test_frozen_source_byte_drift_rejected(tmp_path, monkeypatch):
    original = final.harness.load_json
    def changed(path):
        data, checksum = original(path)
        return data, "bad" if str(path).endswith("acceptance-inputs.json") else checksum
    monkeypatch.setattr(final.harness, "load_json", changed)
    with pytest.raises(final.harness.SpecError, match="Frozen source bytes"):
        final.specs()


def test_historical_changed_dependency_is_only_an_old_reference(tmp_path):
    inputs, expected, registry = actual_specs()
    base = {"schema_version": "acceptance-report-v1", "cases": [{"id": name, "status": "passed_partial", "attempts": []} for name in registry],
            "sources": {"inputs_sha256": final.PINS["inputs"], "expected_sha256": final.PINS["expected"]},
            "code_provenance": {"commit": "b"*40, "file_sha256": {"code/backend/runtime.py": "old"}}}
    path = tmp_path / "historical.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    reference = final.historical_reference(path, inputs, registry, provenance())
    assert reference["status"] == "historical_reference_dependencies_changed"
    assert not reference["whole_acceptance_transfer"]
    assert all(not r["fresh_execution"] for r in reference["cases"].values())


def test_paid_rows_or_id_substitution_cannot_enter_local_inventory():
    inputs, expected, registry = actual_specs()
    for fresh in ({"actual_provider_calls": 1, "cases": [{"id": x} for x in registry]},
                  {"actual_provider_calls": 0, "cases": [{"id": "fake"}] * 194}):
        with pytest.raises(final.harness.SpecError):
            final.inventory(inputs, expected, registry, provenance(), fresh=fresh)



def test_cli_keeps_real_failure_distinct_from_partial_evidence(monkeypatch):
    monkeypatch.setattr(final, "run", lambda *a, **k: {"summary": {"coverage": {"failed": 1}}})
    assert final.main(["--target-commit", "a"*40, "--output", "unused.json"]) == 1


def test_semantic_preparation_rejects_unassigned_output_before_runtime(tmp_path):
    import asyncio
    import evaluate_rag_semantics as semantic
    with pytest.raises(ValueError, match="contest-final"):
        asyncio.run(semantic.prepare_live_samples(final.ROOT / "no-assigned-output.json", tmp_path / "no-config.json"))



def test_semantic_material_does_not_reveal_conflict_or_opposite_in_metadata(tmp_path):
    import evaluate_rag_semantics as semantic
    manifest = semantic.conflict_source(tmp_path / "source", neutral_metadata=True)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    values = [data["knowledge_release_id"], data["policy"]["knowledge_release_id"]]
    for doc in data["documents"]:
        assert not doc["reviewed_conflict"]
        raw = (manifest.parent / doc["file"]).read_bytes()
        assert doc["content_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        values.extend((doc["document_id"], doc["document_version"], doc["title"], doc["source_ref"]))
        for chunk in json.loads(raw)["chunks"]:
            values.extend((chunk["reference_id"], chunk["section"], chunk["procedure_group_id"]))
    assert all("conflict" not in v and "opposite" not in v for v in values)


def declared_plan(tmp_path):
    """Standalone synthetic full194 declaration; ignored personal artifacts are not fixtures."""
    from copy import deepcopy
    inputs, expected, registry = actual_specs()
    frozen = {r['id']:r for r in inputs['direct']+inputs['l3_cases']+inputs['rag_comparison']['questions']}
    plans = final.extended_probe_plans(inputs)
    fixtures = {f"{g['test']}-{v}":g['fixture'] for g in inputs['groups'] for v in g['variants']}
    rows=[]
    for case,entry in registry.items():
        controls=deepcopy(plans.get(case,dict(seed=123,tick=0,fixture=fixtures[case])))
        if case in frozen:
            controls.update(deepcopy(frozen[case])); controls.setdefault('tick',0)
        controls.setdefault('observation_end_tick',controls['tick'])
        rows.append(dict(id=case,authority='original_detailed' if case in frozen else 'proposed_for_adoption',
            original_detailed=deepcopy(frozen.get(case)), original_expected=deepcopy(expected['variant_expected'][case]),
            original_criterion=deepcopy(expected['criteria'][entry['group']]),controls=controls,
            schedule=dict(execute_tick=controls['tick'],observation_end_tick=controls['observation_end_tick'],provider_calls=0,steps=['explicit synthetic test condition'])))
    path=tmp_path/'declared.json';path.write_text(json.dumps(dict(version='contest-194-conditions-proposal-v1',frozen_sha256=final.PINS,cases=rows)),encoding='utf-8')
    return path


def test_149_explicit_conditions_preserve45_original_locked_controls(tmp_path):
    inputs, expected, registry = actual_specs()
    rows, digest = final.validate_condition_plan(declared_plan(tmp_path), inputs, expected, registry)
    assert len(rows) == 194 and len(digest) == 64
    assert sum(r["authority"] == "original_detailed" for r in rows.values()) == 45
    assert sum(r["authority"] == "proposed_for_adoption" for r in rows.values()) == 149
    assert all(r["schedule"]["steps"] and r["schedule"]["observation_end_tick"] >= r["controls"]["tick"] for r in rows.values())


@pytest.mark.parametrize("mutation", ["expected", "locked_seed", "duplicate", "window", "paid"])
def test_conditions_drift_rejected_before_execution(tmp_path, mutation):
    inputs, expected, registry = actual_specs()
    original = json.loads(declared_plan(tmp_path).read_text(encoding="utf-8"))
    if mutation == "expected": original["cases"][0]["original_expected"] = {"decision":"invented"}
    elif mutation == "locked_seed": original["cases"][0]["controls"]["seed"] += 1
    elif mutation == "duplicate": original["cases"][1] = original["cases"][0]
    elif mutation == "window": original["cases"][0]["schedule"]["observation_end_tick"] = -1
    else: original["cases"][0]["schedule"]["provider_calls"] = 1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(final.harness.SpecError):
        final.validate_condition_plan(path, inputs, expected, registry)


def test_extended_actual_predicates_recomputed_and_t08_contract_not_promoted(tmp_path, monkeypatch):
    path = declared_plan(tmp_path)
    monkeypatch.setattr(final, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(final.harness, "code_provenance", provenance)
    def run_probe(**kwargs):
        return {"controls":kwargs["controls"],"provider_calls":0,
                "checks":[{"name":"real_failure","actual":False,"expected":True,"matched":True}]}
    result = final.extended_local(tmp_path/"trial/report.json", target="a"*40, conditions=path,
            selected=["T08-budget-limit"], local_runner=run_probe)
    row = next(r for r in result["cases"] if r["id"] == "T08-budget-limit")
    assert row["local_probe"]["status"] == "failed"
    assert not row["local_probe"]["evidence"]["checks"][0]["matched"]
    assert row["contract_revision_required"]["status"] == "resolved_by_main_contract_commit_baa9e99"
    assert "original_T08_contract_adoption_revision" not in row["missing"]
    assert result["summary"]["whole_acceptance_passed"] == 0 and not result["full_acceptance"]
    assert result["resources"]["all_scratch_cleaned"]


def test_wrong_adoption_hash_cannot_start_replay(tmp_path):
    inputs, expected, registry = actual_specs()
    with pytest.raises(final.harness.SpecError, match="adoption hash"):
        final.validate_condition_plan(declared_plan(tmp_path), inputs, expected, registry, expected_hash="0"*64)


@pytest.mark.parametrize('mutation',['end','execute'])
def test_supplemental_control_schedule_disagreement_is_rejected(tmp_path,mutation):
    inputs,expected,registry=actual_specs(); path=declared_plan(tmp_path)
    value=json.loads(path.read_text(encoding='utf-8'))
    row=next(r for r in value['cases'] if r['id']=='T18-repark')
    if mutation=='end':row['controls']['observation_end_tick']-=1
    else:row['schedule']['execute_tick']-=1
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(final.harness.SpecError,match='schedule'):
        final.validate_condition_plan(path,inputs,expected,registry)


@pytest.mark.parametrize('case',final.GEOMETRY_IDS+final.API_IDS)
def test_declared_adapter_measures_actual_product_and_cleans(case,tmp_path):
    inputs,expected,registry=actual_specs(); controls=final.extended_probe_plans(inputs)[case]
    evidence=final.run_extended_probe(case_id=case,controls=controls,scratch=tmp_path)
    assert evidence['controls']==controls and evidence['provider_calls']==0
    assert evidence['checks'] and all(c['matched']==(c['actual']==c['expected']) for c in evidence['checks'])
    assert evidence['actual_end_sim_time_ms']==controls['observation_end_tick']*100
    assert evidence['cleanup']['store_lock_closed']
    assert evidence['scope'].endswith('no UI/live model')
    # Unsupported geometry is kept as a failed predicate; it cannot be called success.
    if evidence.get('observations'):
        current=evidence['observations'][-1]['assessment']
        if current['support_status']=='unsupported_geometry':
            assert any(not c['matched'] for c in evidence['checks'])
    if case=='T18-repark':
        assert evidence['responses']==[{'response':'will_move'}]
        assert evidence['followup']['status']=='resolved'
    if case=='T06-clear-context':
        assert len(evidence['operations'])>=2
        assert evidence['http'] and evidence['devices_final']
        assert all(c['matched'] for c in evidence['checks']), evidence['checks']


def test_unreached_declared_window_remains_missing(tmp_path,monkeypatch):
    path=declared_plan(tmp_path);monkeypatch.setattr(final,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(final.harness,'code_provenance',provenance)
    def probe(**kwargs):
        return dict(controls=kwargs['controls'],provider_calls=0,world_tick_replay=True,actual_end_sim_time_ms=0,
                    checks=[dict(name='subcheck',actual=True,expected=True,matched=True)])
    result=final.extended_local(tmp_path/'run/evidence.json',target='a'*40,conditions=path,selected=['T18-repark'],local_runner=probe)
    row=next(r for r in result['cases'] if r['id']=='T18-repark')
    assert not row['observed_window']['reached']
    assert 'declared_observation_window_not_reached' in row['missing']
    assert row['acceptance_status']=='not_established'


def test_device_operations_do_not_certify_declared_world_window(tmp_path, monkeypatch):
    path=declared_plan(tmp_path);monkeypatch.setattr(final,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(final.harness,'code_provenance',provenance)
    def probe(**kwargs):
        return dict(controls=kwargs['controls'],provider_calls=0,world_tick_replay=False,actual_end_sim_time_ms=100000,
                    checks=[dict(name='device_receipt',actual=True,expected=True,matched=True)])
    result=final.extended_local(tmp_path/'run/evidence.json',target='a'*40,conditions=path,selected=['T24-allowed-zone'],local_runner=probe)
    row=next(r for r in result['cases'] if r['id']=='T24-allowed-zone')
    assert not row['observed_window']['reached']
    assert not row['observed_window']['world_tick_replay']
    assert 'declared_world_observation_window_not_exercised' in row['missing']
    assert row['acceptance_status']=='not_established'


@pytest.mark.parametrize("frozen_status", ["failed", "error"])
@pytest.mark.parametrize("historical_only", [False, True])
def test_frozen_failure_and_cleanup_survive_local_success_or_historical_override(tmp_path, monkeypatch, frozen_status, historical_only):
    path=declared_plan(tmp_path);monkeypatch.setattr(final,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(final.harness,'code_provenance',provenance)
    inputs, expected, registry=actual_specs()
    def frozen(*args, **kwargs):
        return {'actual_provider_calls':0,'resources':{'all_scratch_cleaned':False,'sentinel':'preserve original frozen detail'},
            'cases':[{'id':case,'attempts':[{'status':frozen_status,'scratch_cleaned':False}] if case=='V03-public-projection' else []} for case in registry]},None
    def probe(**kwargs):
        result=dict(controls=kwargs['controls'],provider_calls=0,checks=[dict(name='local_observation',actual=True,expected=True,matched=True)])
        if historical_only:
            result.update(contract_conflict={'historical':True}, current_contract_checks={'current':True},
                checks=[dict(name='frozen_no_new_dispatch_after_unknown',actual=False,expected=True,matched=False)])
        return result
    result=final.extended_local(tmp_path/'run/report.json',target='a'*40,conditions=path,selected=['V03-public-projection'],local_runner=probe,frozen_runner=frozen)
    row=next(r for r in result['cases'] if r['id']=='V03-public-projection')
    assert row['coverage_status']=='failed' and row['execution_components']['frozen_failed']
    assert result['summary']['coverage']['failed']==1
    assert not result['resources']['all_scratch_cleaned']
    assert result['resources']['frozen']['sentinel']=='preserve original frozen detail'
    assert result['resources']['extended']['all_scratch_cleaned']
    assert result['summary']['whole_acceptance_passed']==0


def test_t20_retains_prior_and_new_impact_without_rejecting_other_valid_impacts():
    def row(kind,zone,incident='same'):
        return {'incident_id':incident,'type':kind,'zone_id':zone}
    prior=[row('exit_blocked','B01'),row('aisle_obstruction','aisle-west')]
    added=row('bay_intrusion','B01')
    current=prior+[added,row('bay_intrusion','B02')]
    assert final.prior_impacts_and_new_impact_linked(prior,current,'same')
    assert not final.prior_impacts_and_new_impact_linked(prior,[prior[0],added],'same')
    assert not final.prior_impacts_and_new_impact_linked(prior,prior+[row('bay_intrusion','B01','different')],'same')
    assert not final.prior_impacts_and_new_impact_linked([],current,'same')


def test_other_facility_probe_reaches_authorization_with_current_run(tmp_path):
    evidence = final._declared_api_probe("T10-other-facility",
        {"seed": 96047, "fixture": "s1a-foundation-v1", "tick": 0, "observation_end_tick": 0}, tmp_path)
    response = evidence["http"][-1]
    assert "?run_id=" in response["path"]
    assert response["path"].split("?run_id=")[1]
    assert response["status"] in (403, 404)
    assert all(row["matched"] for row in evidence["checks"])
