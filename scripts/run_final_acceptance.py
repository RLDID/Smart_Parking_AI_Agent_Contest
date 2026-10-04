"""Locked 194 evidence inventory and exact frozen local probes.

Whole acceptance requires all original criteria and independent evidence;
this runner never promotes structural checks or partial native probes.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

import run_acceptance as harness
ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / "Work_tree/artifacts/contest-final-acceptance"
PINS = {"inputs": "d9992061c08952d3d6cce3f1dbc1cfbbc705d0f102705b6b61bceae15e5f1527",
        "expected": "bc3daea810ff386c7053036dbec8f66b94304fcf33805f18da5bba293e0f4272"}

HISTORICAL_EXPECTED_SHA256 = "c029e788a4813a817a6d7d72734549a24a500c9580b24a3239e4aa5a2fdff410"
ADOPTED_T08_FORBID = "무한 호출·전송 결과 미확인인 동일 작업 키의 중복 전송·명시한 비용 한도 초과 호출·AI 성공 위장"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def specs():
    inputs, ih = harness.load_json(ROOT / "tests/scenarios/acceptance-inputs.json")
    expected, eh = harness.load_json(ROOT / "tests/expected/acceptance-cases.json")
    harness.require({"inputs": ih, "expected": eh} == PINS, "Frozen source bytes changed")
    registry = harness.validate_specs(inputs, expected)
    return inputs, expected, registry


def fresh_output(path):
    path = Path(path).resolve()
    root = ARTIFACT_ROOT.resolve()
    harness.require(path.is_relative_to(root) and path.parent != root, "Choose a run subdirectory in contest-final-acceptance")
    harness.require(not path.exists(), "Report already exists")
    harness.require(not (path.parent / "resources").exists(), "Run resources already exist; choose a new run")
    return path


def assert_target(target, provenance):
    harness.require(isinstance(target, str) and len(target) == 40 and all(c in "0123456789abcdef" for c in target), "Full target commit required")
    harness.require(provenance.get("commit") == target, "Target differs from current checkout; no execution")
    harness.require(provenance.get("working_tree_status") == "observed", "Product working tree status unavailable")
    changes = provenance.get("changes", [])
    harness.require(not any(c["path"].startswith(("code/", "data/")) or c["path"] in
                    ("requirements.lock.txt", "tests/scenarios/acceptance-inputs.json", "tests/expected/acceptance-cases.json") for c in changes),
                    "Unfixed product/fixture source changes; no final execution")


def historical_reference(path, inputs, registry, provenance):
    if path is None:
        return {"status": "not_supplied", "cases": {}}
    value, file_hash = harness.load_json(path)
    harness.require(value.get("schema_version") == "acceptance-report-v1", "Historical baseline schema mismatch")
    rows = value.get("cases", [])
    harness.require(len(rows) == len(registry) and {r["id"] for r in rows} == set(registry), "Historical 194 IDs mismatch")
    harness.require(value.get("sources", {}).get("inputs_sha256") == PINS["inputs"] and
                    value.get("sources", {}).get("expected_sha256") in (PINS["expected"], HISTORICAL_EXPECTED_SHA256), "Historical frozen source mismatch")
    old = value.get("code_provenance", {})
    hashes = old.get("file_sha256", {})
    current = provenance.get("file_sha256", {})
    product = {p: h for p, h in hashes.items() if p.startswith(("code/", "data/")) or p == "requirements.lock.txt"}
    matched = bool(product) and all(current.get(p) == h for p, h in product.items())
    return {"status": "partial_reference_dependencies_match" if matched else "historical_reference_dependencies_changed",
            "path": str(Path(path).resolve()), "sha256": file_hash, "source_commit": old.get("commit"),
            "hash_scope": "historical declared product dependency allowlist; not complete final acceptance",
            "whole_acceptance_transfer": False,
            "historical_expected_sha256": value.get("sources", {}).get("expected_sha256"),
            "current_expected_sha256": PINS["expected"],
            "T08_contract_changed": value.get("sources", {}).get("expected_sha256") != PINS["expected"],
            "cases": {r["id"]: {"status": r.get("status"), "attempts": deepcopy(r.get("attempts", [])),
                                  "fresh_execution": False} for r in rows}}


def inventory(inputs, expected, registry, provenance, *, historical=None, fresh=None):
    detailed = {c["id"]: c for c in inputs["direct"] + inputs["l3_cases"]}
    rag_questions = {c["id"]: c for c in inputs["rag_comparison"]["questions"]}
    fresh_rows = {r["id"]: r for r in (fresh or {}).get("cases", [])}
    if fresh is not None:
        harness.require(len(fresh_rows) == len(registry) and set(fresh_rows) == set(registry), "Fresh inventory ID mismatch")
        harness.require(fresh.get("actual_provider_calls") == 0, "Local frozen probes cannot call providers")
    rows = []
    for case_id, entry in registry.items():
        criterion = deepcopy(expected["criteria"][entry["group"]])
        measurement = fresh_rows.get(case_id)
        attempts = measurement.get("attempts", []) if measurement else []
        missing = ["independent_allow_forbid_and_variant_judgment", "whole_required_evidence"]
        if case_id not in detailed and case_id not in rag_questions:
            missing.append("per_case_final_seed_tick_injection_not_adopted")
        if "E-UI" in criterion["evidence"] or case_id.endswith("screen-execution-text"):
            missing.append("actual_frontend_evidence")
        if case_id == "V04-process-separation":
            missing.append("actual_separate_process_contract")
        if case_id.startswith("R01-") or case_id in ("R03-semantic-conflict", "R08-full-docs-vs-keyword", "R08-manual-vs-rule-vs-agent"):
            missing.append("condition_matched_semantic_or_method_review")
        status = "partial" if attempts else "blocked"
        if any(a.get("status") in ("failed", "error", "interrupted") for a in attempts):
            status = "failed"
        rows.append({"id": case_id, "group": entry["group"], "coverage_status": status,
            "acceptance_status": "not_established", "original_expected": deepcopy(expected["variant_expected"][case_id]),
            "original_criterion": criterion, "original_conditions": deepcopy(detailed.get(case_id, rag_questions.get(case_id))),
            "condition_authority": "original_detailed" if case_id in detailed or case_id in rag_questions else "not_adopted",
            "fresh_measurement": measurement if attempts else None,
            "historical_reference": (historical or {}).get("cases", {}).get(case_id),
            "missing": missing, "server_or_physical_device_required_by_original": False})
    return {"schema_version": "contest-final-acceptance-evidence-v1", "mode": "local_mock",
        "target_provenance": provenance, "frozen_sha256": PINS, "cases": rows,
        "summary": {"registered": len(registry), "coverage": dict(Counter(r["coverage_status"] for r in rows)),
                    "whole_acceptance_passed": 0, "whole_acceptance_denominator": len(registry),
                    "fresh_executed": sum(bool(r["fresh_measurement"]) for r in rows)},
        "full_acceptance": False, "provider_calls": 0, "socket_server_starts": 0,
        "historical_reference_metadata": {k:v for k,v in (historical or {}).items() if k != "cases"},
        "resources": (fresh or {}).get("resources"),
        "scope": "original frozen native/direct predicates and remaining requirements; no 194 whole-pass certification",
        "final_integration_verified": False}


def run(output, *, target, conditions=None, historical=None, execute=False, selected=None, runner=None):
    output = fresh_output(output)
    inputs, expected, registry = specs()
    provenance = harness.code_provenance()
    assert_target(target, provenance)
    reference = historical_reference(historical, inputs, registry, provenance)
    result = None
    if execute:
        declaration = Path(conditions) if conditions else None
        harness.require(declaration is not None and declaration.is_file() and declaration.read_bytes().strip(), "Pre-execution conditions required")
        ids = selected or [c["id"] for c in inputs["direct"] + inputs["l3_cases"]]
        supported = {c["id"] for c in inputs["direct"] + inputs["l3_cases"]}
        harness.require(ids and len(ids) == len(set(ids)) and set(ids) <= supported, "Only original frozen direct/native IDs are executable here")
        runner = runner or harness.evaluate
        result, _ = runner(inputs, expected, split="final", repeat=1, selected=ids,
                           resource_root=output.parent / "resources", native_l3=True)
    packet = inventory(inputs, expected, registry, provenance, historical=reference, fresh=result)
    packet["execution_requested"] = execute
    packet["pre_execution_conditions"] = {"path": str(Path(conditions).resolve()), "sha256": digest(conditions)} if execute else None
    harness.write_report(packet, output)
    return packet


def validate_condition_plan(path, inputs, expected, registry, *, expected_hash=None):
    declaration, checksum = harness.load_json(path)
    harness.require(declaration.get("version") == "contest-194-conditions-proposal-v1", "Unknown conditions schema")
    harness.require(declaration.get("frozen_sha256") == PINS, "Conditions frozen sources changed")
    rows = declaration.get("cases", [])
    harness.require(len(rows) == len(registry) and {r.get("id") for r in rows} == set(registry), "Conditions must cover exactly194 unique original IDs")
    if expected_hash is not None:
        harness.require(checksum == expected_hash, "Conditions adoption hash differs")
    frozen = {r["id"]: r for r in inputs["direct"] + inputs["l3_cases"] + inputs["rag_comparison"]["questions"]}
    for row in rows:
        case = row["id"]
        harness.require(row.get("original_expected") == expected["variant_expected"][case] and row.get("original_criterion") == expected["criteria"][registry[case]["group"]], "Condition changed original judgment")
        controls, schedule = row.get("controls", {}), row.get("schedule", {})
        harness.require(type(controls.get("seed")) is int and type(controls.get("tick")) is int and 0 <= controls["tick"] <= 10000, "Explicit bounded seed/tick required")
        harness.require(type(schedule.get("observation_end_tick")) is int and schedule["observation_end_tick"] >= controls["tick"] and schedule.get("provider_calls") == 0 and schedule.get("steps"), "Explicit observation window and local0-call schedule required")
        harness.require(isinstance(controls.get("fixture"), str) and controls["fixture"], "Explicit fixture required")
        if case not in frozen:
            harness.require(controls.get('observation_end_tick') == schedule['observation_end_tick'], 'Declared controls/schedule observation window mismatch')
            harness.require(controls['tick'] == schedule.get('execute_tick'), 'Declared controls/schedule execution tick mismatch')
        if case in frozen:
            original = frozen[case]
            harness.require(row.get("original_detailed") == original, "Original locked condition was changed")
            for key in ("seed", "tick", "fixture", "injection", "query", "topic", "role"):
                if key in original:
                    harness.require(controls.get(key) == original[key], "Original locked control was changed: " + key)
    return {row["id"]: row for row in rows}, checksum



GEOMETRY_IDS = ('T02-normal-passage','T02-parking-maneuver','T02-gate-wait','T02-yield-then-move','T02-observation-loss',
    'T17-report-only','T17-space-restored-a-stays','T18-two-bays','T18-body-protrusion','T18-repark',
    'T19-transient-line','T19-adjacent-bay-blocked','T20-reblock-elsewhere','T20-multiple-impacts')
API_IDS = ('T06-ambiguous-close','T06-clear-context','T06-cancel-before-confirm',
           'T10-other-vehicle','T10-other-facility','T10-csrf-origin')


def extended_probe_plans(inputs):
    plans = harness.local_probe_plans(inputs)
    fixtures = {f"{g['test']}-{v}": g['fixture'] for g in inputs['groups'] for v in g['variants']}
    order = list(fixtures)
    for case in GEOMETRY_IDS + API_IDS:
        tick = 66 if case.startswith('T06-') else 0 if case.startswith('T10-') else 60
        end = 100 if case.startswith('T06-') else 360 if case in ('T17-space-restored-a-stays','T18-repark') else 120 if case.startswith('T20-') else tick
        pose = dict(x=11.0,y=26.5,heading_deg=90.0)
        if case.startswith('T02-'): pose.update(x=4.0,y=20.0)
        if case == 'T17-report-only': pose.update(x=27.0,y=21.7,heading_deg=0.0)
        if case == 'T17-space-restored-a-stays': pose.update(x=9.5,y=21.7,heading_deg=0.0)
        if case in ('T18-two-bays','T19-adjacent-bay-blocked'): pose.update(x=10.5,heading_deg=0.0)
        if case == 'T18-body-protrusion': pose.update(x=9.5,y=24.1)
        if case == 'T19-transient-line': pose.update(x=9.5,heading_deg=94.0)
        if case.startswith('T20-'): pose.update(x=9.5,y=21.7,heading_deg=0.0)
        plans[case] = dict(adapter='declared-api' if case in API_IDS else 'declared-geometry',
            version='declared-public-v1', fixture=fixtures[case], seed=inputs['splits']['final']['seed_base']+5000+order.index(case),
            tick=tick, observation_end_tick=end, tick_ms=100, condition_authority='supplemental_not_adopted_final',
            recipe_version='public-observation-recipe-v1', initial_pose=pose,
            response='will_move' if case in ('T17-space-restored-a-stays','T18-repark') else None,
            transient_ticks=[10,11] if case=='T19-transient-line' else None)
    return plans


def run_extended_probe(*, case_id, controls, scratch):
    if controls['adapter'] not in ('declared-geometry', 'declared-api'):
        return harness.run_local_probe(case_id=case_id, controls=controls, scratch=scratch)
    if controls['adapter'] == 'declared-api':
        return _declared_api_probe(case_id, controls, scratch)
    import asyncio
    return asyncio.run(_declared_geometry_probe(case_id, controls, scratch))


def _probe_evidence(controls):
    evidence = dict(controls=deepcopy(controls), provider_calls=0, checks=[], layers=[],
        decision_semantics='not_independently_adjudicated', scope='synthetic native/API partial evidence; no UI/live model')
    def check(name, actual, wanted):
        evidence['checks'].append(dict(name=name, actual=deepcopy(actual), expected=deepcopy(wanted), matched=actual==wanted))
    return evidence, check



def prior_impacts_and_new_impact_linked(prior, current, incident_id, new_impact=("bay_intrusion", "B01")):
    """The same incident keeps every prior impact and includes the new one."""
    previous = {(row["type"], row["zone_id"]) for row in prior if row["incident_id"] == incident_id}
    present = {(row["type"], row["zone_id"]) for row in current if row["incident_id"] == incident_id}
    return bool(incident_id and previous and previous <= present and new_impact in present)


async def _declared_geometry_probe(case, controls, scratch):
    from backend.runtime import Runtime
    from backend.auth import Auth
    from contracts.autonomous import AutonomousControl
    from contracts.synthetic_users import SyntheticUserInput
    from simulator.world import initial_world, observe, set_observation_mode
    runtime = Runtime(Path(scratch)/'geometry.sqlite3')
    e, check = _probe_evidence(controls)
    try:
        world = initial_world(controls['seed'], controls['fixture'])
        runtime.world = world
        target = next(a for a in world['actors'] if a['object_id']=='obj-car-02')
        target.update(controls['initial_pose'])
        # Remove conflicting distractors only in supplemental initial synthetic poses.
        if case.startswith('T02-'):
            world['actors'] = [target]
        world['observation_history']=[]; world['observation_queue']=[]
        world.pop('observation',None); observe(world)
        runtime.store.commit(world, runtime.event(world))
        auth=Auth(runtime.store); token, operator=auth.login('demo-operator','parking-demo-only','declared-measurement')
        run=world['run_id']; scenario='s1a' if case.startswith('T02-') else 's1b' if case.startswith(('T17-','T20-')) else 's1c'
        kind, zone = ('aisle_obstruction','aisle-west') if scenario=='s1a' else ('exit_blocked','B01') if scenario=='s1b' else ('bay_intrusion','B01')
        async def agent(key, sc=scenario):
            return await runtime.autonomous.control(operator,AutonomousControl(run_id=run,action='process',mode='mock',scenario=sc),key,lambda:auth.require(token))
        def sample(stage):
            a=runtime.business.impact_assessment(kind,'obj-car-02',zone)
            e.setdefault('observations',[]).append(dict(stage=stage,sim_time_ms=world['sim_time_ms'],observation=deepcopy(world['observation']),assessment=a))
            return a
        for index in range(1,controls['tick']+1):
            if case.startswith('T02-'):
                # Explicit synthetic observation choreography, not inferred traffic intent.
                if case=='T02-gate-wait': target.update(x=29.5,y=2.0,heading_deg=90.0)
                elif case=='T02-parking-maneuver': target.update(x=9.5,y=24.0+min(index,5)*.08,heading_deg=90.0)
                elif case=='T02-yield-then-move': target.update(x=4.0,y=20.0+max(0,index-5)*.2)
                else: target.update(x=4.0,y=20.0+index*.2)
                if case=='T02-observation-loss' and index==10: set_observation_mode(world,'unavailable')
            if case=='T19-transient-line':
                target.update(x=11.0 if index in controls['transient_ticks'] else 9.5,heading_deg=90.0)
            runtime.advance_candidate(world)
            if index%2==0: sample('setup')
        runtime.store.commit(world,runtime.event(world)); before=sample('before_execution')
        e['first']=await agent('first')
        if case in ('T17-space-restored-a-stays','T18-repark'):
            check('supported_candidate_before_response',[before['support_status'],before['violation_candidate']],['supported',True])
            runtime.synthetic_users.configure(operator,run,SyntheticUserInput(mode='will_move',expected_state_version=world['state_version']),'explicit-consumer')
            e['delivery_processed']=await runtime.business.deliver_one(); runtime.synthetic_users.process()
            world=runtime.world  # consumer commits a copy with the real queued movement
            e['responses']=[dict(r) for r in runtime.store.db.execute('SELECT response FROM notification_responses')]
            check('actual_will_move_receipt', [r['response'] for r in e['responses']],['will_move'])
            reference=deepcopy(next((a for a in world['actors'] if a['object_id']=='obj-car-01'),None))
            for index in range(controls['tick']+1,controls['observation_end_tick']+1):
                runtime.advance_candidate(world)
                if index%10==0: sample('response_movement')
            runtime.store.commit(world,runtime.event(world)); after=sample('final')
            e['followup']=await agent('recovery')
            check('supported_clearance_after_movement',[after['support_status'],after['clearance_sustained']],['supported',True])
            check('resolved_after_observed_clearance',e['followup']['status'],'resolved')
            if reference is not None: check('reference_vehicle_stays',next((a for a in world['actors'] if a['object_id']=='obj-car-01'),None),reference)
        elif case.startswith('T20-'):
            iid=e['first'].get('incident_id'); e['initial_incident_id']=iid
            e['initial_impacts']=[dict(row) for row in runtime.store.db.execute(
                'SELECT incident_id,type,zone_id FROM incident_impacts WHERE incident_id=?',(iid,))]
            # Explicit relocation remains evaluator injection, not proof of a drive action.
            target.update(x=11.0,y=26.5,heading_deg=90.0)
            for index in range(controls['tick']+1,controls['observation_end_tick']+1): runtime.advance_candidate(world)
            runtime.store.commit(world,runtime.event(world))
            e['other_impact']=runtime.business.impact_assessment('bay_intrusion','obj-car-02','B01')
            e['followup']=await agent('new-impact','s1c')
            e['impacts']=[dict(r) for r in runtime.store.db.execute('SELECT incident_id,type,zone_id FROM incident_impacts')]
            check('second_impact_supported',[e['other_impact']['support_status'],e['other_impact']['violation_candidate']],['supported',True])
            check('same_cause_incident',e['followup'].get('incident_id'),iid)
            check('prior_and_new_impacts_linked',prior_impacts_and_new_impact_linked(e['initial_impacts'],e['impacts'],iid),True)
            check('incident_not_resolved',runtime.store.db.execute('SELECT status FROM incidents WHERE incident_id=?',(iid,)).fetchone()[0]=='resolved',False)
            e['missing_variant_evidence']=['simultaneous_physical_impacts'] if case=='T20-multiple-impacts' else ['continuous_physical_relocation_not_pose_injection']
        else:
            support=before['support_status']
            check('geometry_supported' if case!='T02-observation-loss' else 'observation_loss_not_supported',support=='supported',case!='T02-observation-loss')
            if case in ('T18-two-bays','T19-adjacent-bay-blocked'):
                check('adjacent_bay_area_measured',bool(before.get('adjacent_space_occupation',{}).get('bay_ids')),True)
                check('persistent_intrusion_measured',before['violation_candidate'],True)
            elif case=='T18-body-protrusion':
                check('footprint_protrudes_despite_center_inside',before['occupied'],True)
                e['missing_variant_evidence']=['supported_usable_aisle_impact_beyond_bay_footprint']
            else:
                check('no_persistent_violation',before['violation_candidate'],False)
                check('no_private_contact',runtime.store.db.execute("SELECT count(*) FROM notifications WHERE purpose='move_request'").fetchone()[0],0)
            if case=='T17-report-only': e['report_input']={'text':'가상 이중주차 신고','authority':'evaluator supplemental allegation; not physical truth'};e['missing_variant_evidence']=['actual_customer_report_API_submission']
            if case.startswith('T02-'): e['missing_variant_evidence']=['semantic_traffic_context_independent_review','gate_device_open_transition'] if case=='T02-gate-wait' else ['semantic_traffic_context_independent_review']
        e.update(actual_end_sim_time_ms=world['sim_time_ms'],world_tick_replay=True,layers=['public_observation','real_operating_analysis','mock_autonomous','sqlite_business'],
                 notifications=[dict(r) for r in runtime.store.db.execute('SELECT notification_id,purpose,delivery_status FROM notifications')],
                 incidents=[dict(r) for r in runtime.store.db.execute('SELECT incident_id,status FROM incidents')],
                 executions=[dict(r) for r in runtime.store.db.execute('SELECT execution_id,tool_name,status,error_code FROM executions')])
        return e
    finally:
        await runtime.queries.close(); await runtime.autonomous.close(); runtime.store.close()
        e['cleanup']={'query_active':len(runtime.queries.active),'autonomous_active':len(runtime.autonomous.active),'store_lock_closed':runtime.store.lock_file.closed}


def _declared_api_probe(case, controls, scratch):
    from backend.app import Settings,create_app
    from fastapi.testclient import TestClient
    from simulator.world import initial_world
    e,check=_probe_evidence(controls); origin='http://testserver'
    app=create_app(Settings(database=Path(scratch)/'api.sqlite3',test_control=True,origins=(origin,),background_ticks=False))
    with TestClient(app) as client:
        runtime=app.state.runtime
        async def setup():
            runtime.world=initial_world(controls['seed'],controls['fixture'])
            for _ in range(controls['tick']): runtime.advance_candidate(runtime.world)
            runtime.store.commit(runtime.world,runtime.event(runtime.world))
            return runtime.world['run_id']
        run=client.portal.call(setup)
        login=client.post('/api/v1/auth/session',headers={'Origin':origin},json={'username':'demo-owner' if case.startswith('T06') else 'demo-driver','password':'parking-demo-only'})
        check('login_success',login.status_code,200)
        headers={'Origin':origin,'X-CSRF-Token':client.get('/api/v1/me').json()['csrf_token']}
        def request(method,path,body=None,key='key',custom=None):
            response=client.request(method,path,json=body,headers=(headers if custom is None else custom)|{'Idempotency-Key':key})
            e.setdefault('http',[]).append(dict(method=method,path=path,status=response.status_code,body=response.json()))
            return response
        async def effects():
            return dict(state=runtime.world['state_version'],devices=deepcopy(runtime.world['device_state']),counts={t:runtime.store.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('commands','executions','notifications')})
        before=client.portal.call(effects)
        body={'run_id':run,'purpose':'operational_goal','text':'문 닫아줘','based_on_state_version':runtime.world['state_version']}
        if case.startswith('T10-'):
            if case=='T10-other-vehicle':
                body.update(purpose='own_vehicle_query',target_vehicle_id='veh-demo-01')
                result=request('POST','/api/v1/facilities/fac-demo-01/commands',body)
            elif case=='T10-other-facility': result=request('GET',f'/api/v1/facilities/fac-other/state?run_id={run}')
            else:
                body.update(purpose='own_vehicle_query',target_vehicle_id='veh-demo-02')
                for fault in ({'Origin':'http://untrusted.invalid','X-CSRF-Token':headers['X-CSRF-Token']},{'Origin':origin},{'Origin':origin,'X-CSRF-Token':'wrong'}):
                    result=request('POST','/api/v1/facilities/fac-demo-01/commands',body,custom=fault)
                    check('origin_csrf_denied_'+str(len(e['http'])),result.status_code,403)
            if case!='T10-csrf-origin': check('scope_denied',result.status_code in (403,404),True)
            check('no_effects',client.portal.call(effects),before)
        else:
            if case=='T06-clear-context': body['text']='영업 종료 후 입차 제한, 출차 허용'
            created=request('POST','/api/v1/facilities/fac-demo-01/commands',body,'create')
            check('command_created',created.status_code,201); cid=created.json()['command_id']
            process={'run_id':run,'action':'process','mode':'mock','scenario':'s3','command_id':cid}
            preview=request('POST','/api/v1/test/agent/operations',process,'preview')
            e['preview']=preview.json()
            if case=='T06-clear-context':
                check('confirmation_required',preview.json().get('status'),'confirmation_required')
                plan=request('GET',f'/api/v1/commands/{cid}/plan')
                confirm=request('POST',f'/api/v1/commands/{cid}/confirm',{'expected_resource_version':plan.json()['command_version']},'confirm')
                check('confirmed',confirm.status_code,200)
                operations=[]
                for index in range(controls['tick'],controls['observation_end_tick']+1):
                    if index>controls['tick']:
                        async def step():
                            runtime.advance_candidate(runtime.world);runtime.store.commit(runtime.world,runtime.event(runtime.world))
                            runtime.devices.reconcile()  # same real callback as product business_loop
                            return dict(sim_time_ms=runtime.world['sim_time_ms'],devices=runtime.public_devices(runtime.world))
                        e.setdefault('device_feedback_ticks',[]).append(client.portal.call(step))
                    if index%2==0: operations.append(request('POST','/api/v1/test/agent/operations',process,'step-'+str(index)).json())
                e['operations']=operations; e['devices_final']=client.portal.call(effects)['devices']
                gates=e['devices_final']['gates']; check('entry_restricted',next(g for g in gates if g['gate_id']=='gate-in-01')['entry_policy'],'deny')
                check('exit_not_denied',next(g for g in gates if g['gate_id']=='gate-out-01')['entry_policy'],'allow')
            elif case=='T06-cancel-before-confirm':
                view=request('GET',f'/api/v1/commands/{cid}').json()
                cancelled=request('POST',f'/api/v1/commands/{cid}/cancel',{'expected_resource_version':view['resource_version']},'cancel')
                check('cancelled',cancelled.json().get('aggregate_status'),'cancelled')
                stale=request('POST',f'/api/v1/commands/{cid}/confirm',{'expected_resource_version':view['resource_version']},'stale')
                check('stale_confirmation_denied',stale.status_code,409)
                check('no_device_effects',client.portal.call(effects)['devices'],before['devices'])
            else:
                check('clarification_requested',preview.json().get('status'),'clarification_required')
                check('no_device_effects',client.portal.call(effects)['devices'],before['devices'])
        while runtime.world['sim_time_ms'] < controls['observation_end_tick']*100:
            async def finish_window():
                runtime.advance_candidate(runtime.world);runtime.store.commit(runtime.world,runtime.event(runtime.world))
            client.portal.call(finish_window)
        e.update(actual_end_sim_time_ms=runtime.world['sim_time_ms'],world_tick_replay=True,layers=['real_ASGI_API','auth','business','mock_autonomous'],missing_variant_evidence=['actual_frontend_evidence'])
    e['cleanup']={'app_lifespan_closed':True,'store_lock_closed':runtime.store.lock_file.closed}
    return e


def extended_local(output, *, target, conditions, adopted_hash=None, selected=None, local_runner=None, frozen_runner=None):
    """Execute declared probes and retain unsupported/error observations per ID.

    Declared supplemental conditions can be measured before adoption; complete
    original acceptance still needs independent judgment plus required layers.
    """
    output = fresh_output(output)
    inputs, expected, registry = specs()
    provenance = harness.code_provenance()
    assert_target(target, provenance)
    harness.require(conditions is not None, "Explicit194 conditions file required")
    declarations, checksum = validate_condition_plan(conditions, inputs, expected, registry, expected_hash=adopted_hash)
    ids = list(registry) if selected is None else selected
    harness.require(ids and len(ids) == len(set(ids)) and set(ids) <= set(registry), "Unknown or duplicate condition selection")
    plans = extended_probe_plans(inputs)
    for case, declaration in declarations.items():
        pinned = declaration.get("implemented_replay_controls")
        if pinned is not None:
            harness.require(plans.get(case) == pinned, "Implemented replay parameters changed before execution: " + case)
    local_runner = local_runner or run_extended_probe
    frozen_ids = {c["id"] for c in inputs["direct"] + inputs["l3_cases"]}
    selected_frozen = [case for case in ids if case in frozen_ids]
    if selected_frozen:
        frozen_runner = frozen_runner or harness.evaluate
        fresh, _ = frozen_runner(inputs, expected, split="final", repeat=1, selected=selected_frozen,
                                 resource_root=output.parent / "resources/frozen", native_l3=True)
    else:
        fresh = {"actual_provider_calls": 0, "cases": [{"id": case, "attempts": []} for case in registry]}
    packet = inventory(inputs, expected, registry, provenance, fresh=fresh)
    temp = output.parent / "resources/extended"
    temp.mkdir(parents=True, exist_ok=True)
    for row in packet["cases"]:
        case = row["id"]
        row["declared_condition"] = declarations[case]
        row["condition_authority"] = "original_detailed" if declarations[case]["authority"] == "original_detailed" else "adopted_exact_hash" if adopted_hash else "proposed_explicit_measurement"
        row["missing"] = [m for m in row["missing"] if m != "per_case_final_seed_tick_injection_not_adopted"]
        if not adopted_hash and declarations[case]["authority"] != "original_detailed":
            row["missing"].append("proposed_condition_adoption_confirmation")
        row["local_probe"] = None
        if case not in ids:
            row["execution_status"] = "not_selected"
            continue
        if case not in plans:
            row["execution_status"] = "frozen_executed" if case in selected_frozen else "requires_variant_adapter"
            if case not in selected_frozen:
                row["missing"].append("exact_variant_adapter_not_implemented")
            continue
        controls = deepcopy(plans[case])
        # New adapters may declare additional bounded numeric/fault parameters.
        # Preserve those and override all immutable input controls from the file.
        controls.update({k: deepcopy(v) for k,v in declarations[case]["controls"].items() if k not in ("adapter", "version", "condition_authority")})
        controls["condition_authority"] = row["condition_authority"]
        attempt = {"effective_controls": controls, "declared_conditions_sha256": checksum, "status": "started"}
        scratch, started = None, perf_counter()
        try:
            with TemporaryDirectory(prefix="extended194-", dir=temp) as scratch:
                evidence = local_runner(case_id=case, controls=deepcopy(controls), scratch=Path(scratch))
                harness.require(evidence.get("controls") == controls and evidence.get("provider_calls") == 0, "Probe control/provider mismatch")
                checks = evidence.get("checks", [])
                harness.require(checks and all(set(c) >= {"name", "actual", "expected", "matched"} for c in checks), "Missing actual probe predicates")
                for check in checks:
                    check["matched"] = check["actual"] == check["expected"]
                attempt.update(evidence=evidence, status="matched_partial" if all(c["matched"] for c in checks) else "failed")
        except KeyboardInterrupt:
            attempt.update(status="interrupted", reason="operator interrupted")
        except Exception as error:
            attempt.update(status="error", error_type=type(error).__name__, reason=str(error))
        finally:
            attempt.update(wall_seconds=perf_counter()-started, scratch_cleaned=scratch is None or not Path(scratch).exists())
            if not attempt["scratch_cleaned"]:
                attempt.update(status="error", reason="Scratch cleanup failed")
        row["local_probe"] = attempt
        row["execution_status"] = attempt["status"]
        row["coverage_status"] = "failed" if attempt["status"] in ("failed", "error", "interrupted") else "partial"
        evidence = attempt.get("evidence") or {}
        row['missing'].extend(evidence.get('missing_variant_evidence', []))
        if 'world_tick_replay' in evidence:
            actual_end = evidence.get('actual_end_sim_time_ms')
            declared_end = declarations[case]['schedule']['observation_end_tick'] * controls.get('tick_ms', 100)
            replayed = evidence['world_tick_replay'] is True
            row['observed_window'] = {'actual_end_sim_time_ms': actual_end, 'declared_end_sim_time_ms': declared_end,
                'world_tick_replay': replayed, 'measurement_kind': evidence.get('measurement_kind', 'device_operations_only' if not replayed else 'world_ticks'),
                'reached': replayed and type(actual_end) is int and actual_end >= declared_end}
            if not replayed:
                row['missing'].append('declared_world_observation_window_not_exercised')
            elif not row['observed_window']['reached']:
                row['missing'].append('declared_observation_window_not_reached')
        failed_checks = [c["name"] for c in evidence.get("checks", []) if not c["matched"]]
        if evidence.get("contract_conflict") and failed_checks == ["frozen_no_new_dispatch_after_unknown"] and all(evidence.get("current_contract_checks", {}).values()):
            row["coverage_status"] = "partial"
            row["execution_status"] = "historical_contract_diff_current_checks_matched"
            row["historical_predicate_conflict"] = "Old global unknown-cost blocking predicate retained as history; current adopted contract checks matched"
        if case == "V04-process-separation" and (attempt.get("evidence") or {}).get("processes"):
            row["missing"] = [m for m in row["missing"] if m != "actual_separate_process_contract"]
    for row in packet["cases"]:
        if row["group"] == "T08":
            row["contract_revision_required"] = {"original_forbid": row["original_criterion"]["forbid"],
                "latest_user_contract": "Keep unknown reservations for accounting; do not globally deny unrelated work; optional explicit caps still apply.",
                "status": "resolved_by_main_contract_commit_baa9e99",
                "historical_forbid": "무한 호출·불명 비용에서 새 유료 호출·AI 성공 위장",
                "current_forbid": ADOPTED_T08_FORBID}
            row["adopted_contract_revision"] = "criteria.T08.forbid only; IDs/variant_expected/inputs unchanged"
    for row in packet["cases"]:
        applicability = declarations[row["id"]].get("applicability")
        if applicability:
            row["conditional_applicability"] = applicability
            row["missing"] = [m for m in row["missing"] if m != "actual_separate_process_contract"]
            row["missing"].append("architecture_separation_regression_conditional_not_performed")
    packet.update(conditions={"path": str(Path(conditions).resolve()), "sha256": checksum, "adopted_exact_hash": bool(adopted_hash)},
                  execution_requested=True, scope="Declared original194 per-variant local measurement; missing UI/judgment retained; no inferred whole pass")
    packet["summary"].update(coverage=dict(Counter(r["coverage_status"] for r in packet["cases"])),
         fresh_executed=sum(bool(r["fresh_measurement"] or r["local_probe"]) for r in packet["cases"]),
         local_executed=sum(r["local_probe"] is not None for r in packet["cases"]),
         variant_adapter_missing=sum(r.get("execution_status") == "requires_variant_adapter" for r in packet["cases"]))
    reconcile_execution_components(packet, frozen_resources=fresh.get("resources", {}))
    harness.write_report(packet, output)
    return packet



def reconcile_execution_components(packet, frozen_resources=None):
    """Aggregate preserved measurements; never rerun or weaken any raw check."""
    failed_states = {"failed", "error", "interrupted"}
    frozen_attempts = []
    extended = []
    for row in packet["cases"]:
        native = (row.get("fresh_measurement") or {}).get("attempts", [])
        frozen_attempts.extend(native)
        local = row.get("local_probe")
        if local is not None:
            extended.append((row["id"], local))
        evidence = (local or {}).get("evidence") or {}
        bad_checks = [c["name"] for c in evidence.get("checks", []) if c["actual"] != c["expected"]]
        historical_only = bool(evidence.get("contract_conflict")) and bad_checks == ["frozen_no_new_dispatch_after_unknown"] and bool(evidence.get("current_contract_checks")) and all(evidence["current_contract_checks"].values())
        native_failed = any(a.get("status") in failed_states for a in native)
        local_failed = bool(local) and local.get("status") in failed_states and not historical_only
        row["execution_components"] = {"frozen_attempt_statuses": [a.get("status") for a in native],
            "local_attempt_status": (local or {}).get("status"), "frozen_failed": native_failed,
            "local_current_failed": local_failed, "local_historical_predicate_only": historical_only}
        if native_failed or local_failed:
            row["coverage_status"] = "failed"
            if native_failed:
                row["execution_status"] = "frozen_failure_preserved_with_supplemental_result"
        elif native or local:
            row["coverage_status"] = "partial"
        else:
            row["coverage_status"] = "blocked"
    if frozen_resources is None:
        frozen_resources = {"reconstructed_from_preserved_attempts": True,
            "all_scratch_cleaned": all(a.get("scratch_cleaned") is True for a in frozen_attempts),
            "attempt_cleanup": [{"scratch_path": a.get("scratch_path"), "scratch_cleaned": a.get("scratch_cleaned")} for a in frozen_attempts],
            "unrecorded_original_resource_fields": "not recoverable from overwritten old packet; no invented cleanup proof"}
    else:
        frozen_resources = deepcopy(frozen_resources)
    frozen_clean = not frozen_attempts or (frozen_resources.get("all_scratch_cleaned") is True and all(a.get("scratch_cleaned") is True for a in frozen_attempts))
    if frozen_resources.get("all_scratch_cleaned") is False:
        frozen_clean = False
    extended_clean = all(a.get("scratch_cleaned") is True for _, a in extended)
    packet["resources"] = {"frozen": frozen_resources,
        "extended": {"all_scratch_cleaned": extended_clean, "attempt_cleanup": [{"id": case, "scratch_cleaned": a.get("scratch_cleaned")} for case,a in extended]},
        "all_scratch_cleaned": frozen_clean and extended_clean, "socket_servers": 0}
    packet["summary"]["coverage"] = dict(Counter(r["coverage_status"] for r in packet["cases"]))
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--historical-baseline", type=Path)
    parser.add_argument("--execute-local", action="store_true")
    parser.add_argument("--execute-extended", action="store_true")
    parser.add_argument("--adopt-conditions-sha256")
    parser.add_argument("--conditions", type=Path)
    parser.add_argument("--case", action="append")
    args = parser.parse_args(argv)
    try:
        if args.execute_extended:
            harness.require(not args.execute_local, "Choose one execution mode")
            packet = extended_local(args.output, target=args.target_commit, conditions=args.conditions,
                                    adopted_hash=args.adopt_conditions_sha256, selected=args.case)
        else:
            packet = run(args.output, target=args.target_commit, conditions=args.conditions,
                         historical=args.historical_baseline, execute=args.execute_local, selected=args.case)
        print(json.dumps(packet["summary"], ensure_ascii=False))
        return 1 if packet["summary"]["coverage"].get("failed", 0) else 3
        # Partial/blocked never means whole acceptance; actual failures differ.
    except (OSError, harness.SpecError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
