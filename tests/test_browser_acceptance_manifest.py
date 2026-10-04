"""No browser/server/provider: frozen IDs, plan guards and evidence accounting."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("BROWSER_ACCEPTANCE_NODE") or shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node required; set BROWSER_ACCEPTANCE_NODE or PATH")
SCRIPT = ROOT / "scripts/run_browser_acceptance.mjs"


def node(source, payload=None):
    completed = subprocess.run([str(NODE), "--input-type=module", "-e",
        "import {pathToFileURL} from 'node:url'; "
        "import {readFileSync} from 'node:fs'; "
        "const m = await import(pathToFileURL(process.argv[1]).href); "
        "const payload = JSON.parse(readFileSync(0, 'utf8')); " + source,
        str(SCRIPT)], input=json.dumps(payload), cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", timeout=20, check=True)
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def manifest():
    return node("console.log(JSON.stringify(m.buildManifest(await m.readSpecs())));")


def plan(manifest, case_id="V01-browser-reconnect"):
    row = next(row for row in manifest["cases"] if row["id"] == case_id)
    return {"schema_version": "browser-acceptance-plan-v1", "frontend_version": "reviewed-frontend-sha",
            "origin": "http://127.0.0.1:18184", "cases": [{"id": case_id, "profile": "owner",
            "route": "/#/owner/monitor", "condition": {"source": "supplemental", "fixture": row["fixture"],
            "seed": 17, "tick": 60, "injection": {}}, "setup": [], "steps": [
                {"op": "text", "selector": {"by": "text", "name": "연결됨"}, "literal": "연결됨"}]}]}


def valid(manifest, value):
    return node("try { m.validatePlan(payload.plan, payload.manifest); console.log(JSON.stringify({valid:true})); }"
                "catch(e) { console.log(JSON.stringify({valid:false,error:e.message})); }",
                {"manifest": manifest, "plan": value})


def test_manifest_preserves_all_54_exact_ids_and_r07_supplement(manifest):
    inputs = json.loads((ROOT / "tests/scenarios/acceptance-inputs.json").read_text(encoding="utf-8-sig"))
    expected = json.loads((ROOT / "tests/expected/acceptance-cases.json").read_text(encoding="utf-8-sig"))
    wanted = {f"{group['test']}-{variant}" for group in inputs["groups"]
              if "E-UI" in expected["criteria"][group["test"]]["evidence"] for variant in group["variants"]}
    assert len(wanted) == 54
    assert {row["id"] for row in manifest["cases"] if row["e_ui_required"]} == wanted
    assert {row["id"] for row in manifest["cases"]} == wanted | {"R07-screen-execution-text"}
    assert manifest["whole_suite_registered"] == 194
    assert manifest["original_denominators"] == expected["denominators"]
    for row in manifest["cases"]:
        assert row["independent_expected"] == expected["variant_expected"][row["id"]]
        assert row["criteria"] == expected["criteria"][row["group"]]
    for key, relative in [("inputs_sha256", "tests/scenarios/acceptance-inputs.json"),
                          ("expected_sha256", "tests/expected/acceptance-cases.json")]:
        assert manifest["sources"][key] == sha256((ROOT / relative).read_bytes()).hexdigest()


def test_manifest_build_does_not_mutate_source_and_rejects_registry_changes():
    result = node("const specs=await m.readSpecs(); const before=JSON.stringify(specs);"
                  "m.buildManifest(specs); const unchanged=before===JSON.stringify(specs);"
                  "specs.inputs.groups[0].variants.push('made-up'); let rejected=false;"
                  "try {m.buildManifest(specs);} catch {rejected=true;}"
                  "console.log(JSON.stringify({unchanged,rejected}));")
    assert result == {"unchanged": True, "rejected": True}


def test_supplemental_conditions_do_not_become_adopted_final(manifest):
    value = plan(manifest)
    assert valid(manifest, value)["valid"]
    value["cases"][0]["condition"]["source"] = "adopted"
    assert not valid(manifest, value)["valid"]


@pytest.mark.parametrize("origin", ["https://example.com", "http://192.168.1.8:18080",
    "http://127.0.0.1:18184/path", "http://user:secret@localhost:18184", "http://localhost",
    "http://localhost:4178", "http://127.0.0.1:8018"])
def test_origin_is_isolated_loopback_and_excludes_other_task_ports(manifest, origin):
    value = plan(manifest); value["origin"] = origin
    assert not valid(manifest, value)["valid"]


@pytest.mark.parametrize("mutation", [
    lambda p: p["cases"].append(deepcopy(p["cases"][0])),
    lambda p: p["cases"][0].update(id="T01-blocked"),
    lambda p: p["cases"][0].update(profile="real-account"),
    lambda p: p["cases"][0].update(route="http://example.com/"),
    lambda p: p["cases"][0]["condition"].pop("tick"),
    lambda p: p["cases"][0]["steps"].append({"op": "evaluate", "script": "alert(1)"}),
    lambda p: p["cases"][0]["steps"].append({"op": "goto", "path": "https://example.com"}),
    lambda p: p["cases"][0]["steps"].append({"op": "api", "method": "POST",
        "path": "/api/v1/test/agent/live-queries", "body": {"goal": "regulation"}}),
    lambda p: p["cases"][0]["steps"].append({"op": "api", "method": "POST",
        "path": "/api/v1/test/agent/operations", "body": {"mode": "live", "action": "process"}}),
    lambda p: p["cases"][0]["steps"].append({"op": "api", "method": "POST",
        "path": "/api/v1/test/runs", "body": {"fixture_ref": "wrong-fixture", "seed": 17}}),
    lambda p: p["cases"][0]["steps"].append({"op": "click", "selector": {"by": "css", "name": "#x"}}),
])
def test_unsafe_or_unattributable_plans_are_rejected(manifest, mutation):
    value = plan(manifest); mutation(value)
    assert not valid(manifest, value)["valid"]


def test_mock_api_actions_and_real_offline_keyboard_steps_are_supported(manifest):
    value = plan(manifest)
    entry = value["cases"][0]
    entry["setup"] = [{"op": "api", "method": "POST", "path": "/api/v1/test/runs",
        "expected_status": 201, "save_as": "run", "body": {"facility_id": "fac-demo-01",
        "fixture_ref": entry["condition"]["fixture"], "seed": 17, "config_ref": "foundation-v1"}},
        {"op": "api", "method": "POST", "path": "/api/v1/test/agent/operations",
         "body": {"run_id": "${run.run_id}", "action": "process", "mode": "mock", "scenario": "s1a"}}]
    entry["steps"] += [{"op": "offline", "value": True}, {"op": "capture"},
        {"op": "offline", "value": False}, {"op": "keyboard", "count": 20}, {"op": "keyboard_zoom"},
        {"op": "permission_end", "private_text": "가상 차량"}]
    assert valid(manifest, value)["valid"]
    routes = node("console.log(JSON.stringify(["
        "m.allowedRequest('PUT','/api/v1/test/runs/r/device-faults',{}),"
        "m.allowedRequest('POST','/api/v1/test/agent/live-queries',{}),"
        "m.allowedRequest('POST','/api/v1/test/agent/operations',{mode:'mock'})]));")
    assert routes == [True, False, True]


def test_observed_browser_evidence_never_inflates_acceptance_or_manual_checks(manifest):
    result = node("console.log(JSON.stringify(m.summarize(payload.map(row=>({...row,status:'observed'})))));", manifest["cases"])
    assert result["checks_observed"] == 55
    assert result["acceptance_passed"] == 0 and result["full_acceptance"] is False
    reader = next(row for row in manifest["cases"] if row["id"] == "V05b-screen-reader")
    assert reader["manual_requirements"] == ["actual_screen_reader_navigation"]
    zoom = next(row for row in manifest["cases"] if row["id"] == "V05b-mobile-zoom")
    assert "actual_mobile_or_browser_zoom_review" in zoom["manual_requirements"]


def test_cli_is_dry_by_default_and_run_gate_precedes_browser_import():
    normal = subprocess.run([str(NODE), str(SCRIPT)], cwd=ROOT, capture_output=True,
                            encoding="utf-8", timeout=20)
    assert normal.returncode == 0
    assert json.loads(normal.stdout)["e_ui_required"] == 54
    gated = subprocess.run([str(NODE), str(SCRIPT), "--run"], cwd=ROOT, capture_output=True,
                           encoding="utf-8", timeout=20)
    assert gated.returncode == 3 and "start signal" in gated.stderr


def test_operator_api_requests_never_follow_redirects_or_leave_origin():
    result = node("const calls=[]; const request={fetch:async(url,options)=>{calls.push({url,options});return {status:()=>302};}};"
                  "const rejected=[]; for(const route of ['/health/ready','http://localhost:4178/health/ready','https://example.com/']) {"
                  "try {await m.localApiFetch(request,'http://127.0.0.1:18184',route,{maxRedirects:9});rejected.push(false);}"
                  "catch {rejected.push(true);}} console.log(JSON.stringify({calls,rejected}));")
    assert result["rejected"] == [True, True, True]
    assert len(result["calls"]) == 1
    assert result["calls"][0]["options"]["maxRedirects"] == 0


def test_read_routes_match_actual_history_and_progress_endpoints():
    result = node("console.log(JSON.stringify(payload.map(p=>m.allowedRequest('GET',p))));", [
        "/api/v1/facilities/f/commands?run_id=r", "/api/v1/facilities/f/executions?cursor=x",
        "/api/v1/commands/c/progress", "/api/v1/commands/c/plan", "/api/v1/executions/e",
        "/api/v1/commands/c/executions", "/api/v1/commands", "/api/v1/executions",
        "/api/v1/facilities/f/commands/extra", "/api/v1/me/made-up"])
    assert result == [True] * 5 + [False] * 5


def test_validated_origin_is_normalized_without_mutating_input(manifest):
    value = plan(manifest)
    value["origin"] += "/"
    result = node("const before=JSON.stringify(payload.plan); const result=m.validatePlan(payload.plan,payload.manifest);"
                  "console.log(JSON.stringify({origin:result.origin,unchanged:before===JSON.stringify(payload.plan)}));",
                  {"plan": value, "manifest": manifest})
    assert result == {"origin": "http://127.0.0.1:18184", "unchanged": True}


def test_vehicle_view_read_paths_are_allowed_without_allowing_mutations_or_extra_paths():
    result = node("console.log(JSON.stringify(payload.map(([method,path])=>m.allowedRequest(method,path))));", [
        ["GET", "/api/v1/me/vehicle-locations?facility_id=fac-demo-01&run_id=r"],
        ["GET", "/api/v1/me/parking-map?facility_id=fac-demo-01&map_version=v"],
        ["POST", "/api/v1/me/vehicle-locations"], ["PUT", "/api/v1/me/parking-map"],
        ["GET", "/api/v1/me/vehicle-locations/private"], ["GET", "/api/v1/me/parking-map/extra"]])
    assert result == [True, True, False, False, False, False]


def test_nested_variables_preserve_strict_body_types_and_reject_prototype_or_non_scalar_access():
    result = node("const vars=JSON.parse('{\"run\":{\"run_id\":\"r1\",\"snapshot\":{\"state_version\":7},\"items\":[{\"id\":\"n1\"}],\"enabled\":false}}');"
        "const expanded=m.substitute({version:'${run.snapshot.state_version}',flag:'${run.enabled}',path:'/runs/${run.run_id}',id:'${run.items.0.id}'},vars);"
        "const rejected=[]; for(const expression of ['run.constructor','run.snapshot.__proto__','run.prototype','run.snapshot','run.missing','run.items.length.nope','run..run_id']) {"
        "try{m.lookupVariable(vars,expression);rejected.push(false);}catch{rejected.push(true);}}"
        "const inherited=Object.create({hidden:'secret'});let ownOnly=false;try{m.lookupVariable({run:inherited},'run.hidden');}catch{ownOnly=true;}"
        "console.log(JSON.stringify({expanded,rejected,ownOnly}));")
    assert result["expanded"] == {"version": 7, "flag": False, "path": "/runs/r1", "id": "n1"}
    assert result["rejected"] == [True] * 7 and result["ownOnly"]


def test_scoped_selectors_accept_source_semantics_and_reject_arbitrary_css(manifest):
    value = plan(manifest)
    selector = value["cases"][0]["steps"][0]["selector"]
    selector["scope"] = {"by": "role", "role": "dialog", "name": "장치 상태"}
    assert valid(manifest, value)["valid"]
    selector["scope"] = {"by": "heading_section", "name": "요청 내용"}
    assert valid(manifest, value)["valid"]
    selector["scope"] = {"by": "css", "name": "section:nth-child(2)"}
    assert not valid(manifest, value)["valid"]
    selector["scope"] = {"by": "role", "role": "dialog", "name": "장치 상태", "scope": {"by": "css", "name": "#x"}}
    assert not valid(manifest, value)["valid"]
