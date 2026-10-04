"""Local N3 security/contract regression; not full R08 or LLM acceptance."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("acceptance_rag_comparison", ROOT / "scripts/compare_acceptance_rag.py")
rag = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rag
SPEC.loader.exec_module(rag)


def isolated(name):
    return rag.ARTIFACT_ROOT / "tmp" / (name + "-" + uuid4().hex)


def business_world(case):
    world = rag.initial_world(case["seed"], case["fixture"])
    for _ in range(case["ticks"]):
        rag.advance(world)
    return world


def test_recovery_probe_rejects_unrelated_cases_before_starting_resources(monkeypatch):
    async def forbidden(*args):
        pytest.fail("Invalid probe started resources")
    monkeypatch.setattr(rag.S2S3Rig, "open", forbidden)
    with pytest.raises(ValueError, match="S3 playback failure"):
        asyncio.run(rag.execute_s2s3_case(rag.S2S3_CASES[0], "fixed_rule",
            rag.ROOT / "Work_tree/artifacts/local-rag-completion/scratch/invalid", recovery_probe=True))


@pytest.fixture(scope="module")
def s2s3_rows():
    async def exercise():
        with rag.patch.object(rag, "s2s3_rubric", side_effect=AssertionError("Execution must not consult evaluator")):
            return [await rag.execute_s2s3_case(case, method,
                rag.S2S3_ROOT / "tmp" / ("pytest-" + uuid4().hex))
                for case in rag.S2S3_CASES for method in rag.BUSINESS_METHODS]
    return asyncio.run(exercise())


def test_s2s3_is_opt_in_and_does_not_replace_frozen_v1(monkeypatch):
    assert rag.BUSINESS_SPEC == "local-business-comparison-v1" and len(rag.BUSINESS_CASES) == 9
    assert len(rag.S2S3_CASES) == 9 and len(rag.BUSINESS_METHODS) == 3
    assert rag.S2S3_CONTRACT["repeat"] == 1
    assert {c["seed"] for c in rag.S2S3_CASES} == {3, 6}
    assert rag.load_execution_plan()[1]["sim0"][0]["seed"] == 93001
    called = []
    async def legacy(output, repeat):
        called.append("default")
        return {"summary": [], "cleanup": {"scratch_removed": True},
                "overall_acceptance": {"passed": 0, "denominator": 194}}
    monkeypatch.setattr(rag, "run_comparison", legacy)
    assert rag.main([]) == 0 and called == ["default"]
    with pytest.raises(SystemExit):
        rag.main(["--business-only", "--business-s2-s3"])
    with pytest.raises(SystemExit):
        rag.main(["--business-s2-s3", "--repeat", "3"])
    outputs = []
    async def supplemental(output):
        outputs.append(output)
        return {"evaluation": {"summary": [], "failed": 0}, "cleanup": {"scratch_removed": True}}
    monkeypatch.setattr(rag, "run_s2s3_comparison", supplemental)
    path = rag.S2S3_ROOT / "outputs" / "equals-boundary.json"
    assert rag.main(["--business-s2-s3", f"--output={path}"]) == 0
    assert outputs == [path]


@pytest.mark.parametrize("case", rag.S2S3_CASES, ids=lambda c: c["id"])
def test_s2s3_real_routes_identical_inputs_and_separate_safety(case, s2s3_rows):
    rows = [r for r in s2s3_rows if r["case"] == case["id"]]
    result = rag.evaluate_s2s3(rows, rag.s2s3_rubric())
    assert len(rows) == 3
    assert all(r["matched"] for r in result["attempts"]), [(r["method"], r["status"], r.get("detail"), r["mismatches"]) for r in result["attempts"]]
    assert len({r["final_public_state"]["snapshot"]["run_id"] for r in rows}) == 3
    assert all(r["cleanup"]["scratch_removed"] and r["provider_calls"] == 0 for r in rows)
    for row in rows:
        assert all(not any(key in d["input"] for key in ("expected", "case", "fault", "manual_trace", "seed", "fixture")) for d in row["decisions"])
        if case["scenario"] == "s2":
            assert 1 <= row["first_candidate_tick"] <= 45
            assert row["capture_devices"]["alarms"][0]["claim_count"] > 0
        else:
            preview = next(t for t in row["trace"] if t["stage"] == "plan_preview")
            assert preview["result"]["status"] == "proposed"
            assert len(preview["result"]["steps"]) == 3
            confirmed = next(t for t in row["trace"] if t["stage"] == "confirm")
            assert preview["result"]["plan_id"] == confirmed["result"]["plan_id"]
            assert all(d["decision"]["action"] == "clarify" for d in row["decisions"][:2])


def test_s2s3_evaluator_rejects_false_playback_safety_and_completion(s2s3_rows):
    original = deepcopy(s2s3_rows)
    mutations = (
        ("s2-normal", "independent_alarm", lambda r: r["final_devices"]["alarms"][0].update(audio="failed")),
        ("s3-normal", "both_played", lambda r: r["final_devices"]["broadcasts"][0].update(simulated_playback="pending")),
        ("s3-normal", "outbound_absent", lambda r: r["final_public_state"]["snapshot"]["objects"].append({"object_id": "obj-car-s3-w"})),
        ("s3-broadcast_failed", "no_early_success", lambda r: r["command"].update(aggregate_status="succeeded")),
        ("s3-same_key", "same_key_replayed", lambda r: r["duplicate_result"].update(job_id="wrong")),
        ("s3-restart", "explicit_recovery", lambda r: r["pre_resume_guard"].update(error={"code": "other"})),
    )
    for case, check, mutate in mutations:
        rows = deepcopy([r for r in s2s3_rows if r["case"] == case])
        mutate(rows[0])
        result = rag.evaluate_s2s3(rows, rag.s2s3_rubric())
        assert check in result["attempts"][0]["mismatches"]
    assert s2s3_rows == original


def test_s2s3_rule_does_not_copy_normalized_action(s2s3_rows):
    row = next(r for r in s2s3_rows if r["case"] == "s3-normal" and r["method"] == "fixed_rule")
    context = deepcopy(row["decisions"][2]["input"])
    context["command"]["normalized_goal"]["action"] = "restrict_entry"
    assert rag.s2s3_rule(context) == "announce"
    context["public_devices"]["broadcasts"] = [{"message_id": "closing_notice", "zone_id": "announcement-a", "receipt": "accepted", "simulated_playback": "unknown"}]
    assert rag.s2s3_rule(context) == "hold"


def test_s2s3_evidence_uses_actual_search_and_no_oracle_input(s2s3_rows):
    for row in s2s3_rows:
        assert row["counts"]["knowledge_retrievals"] == len(row["retrievals"])
        inventory = {r["retrieval_id"]: r for r in row["retrievals"]}
        for decision in row["decisions"]:
            search = decision["input"]["knowledge"]
            assert inventory[search["retrieval_id"]] == search
    # Missing semantics/unknown are never synthesized into a supported verdict.
    assert "not_supported" in rag.S2S3_CONTRACT["unknown"]


def test_s2s3_paths_and_existing_output_are_preserved():
    with pytest.raises(ValueError):
        rag.s2s3_path(rag.ROOT / "artifacts" / "outside-s2s3.json")
    with pytest.raises(ValueError):
        rag.s2s3_path(rag.S2S3_ROOT)
    output = rag.S2S3_ROOT / "tmp" / (uuid4().hex + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("preserve", encoding="utf-8")
    try:
        with pytest.raises(FileExistsError):
            asyncio.run(rag.run_s2s3_comparison(output))
        assert output.read_text(encoding="utf-8") == "preserve"
    finally:
        output.unlink()


def test_business_plan_is_separate_and_frozen_before_results():
    assert len(rag.BUSINESS_CASES) == 9
    assert len({c["id"] for c in rag.BUSINESS_CASES}) == 9
    assert {c["ticks"] for c in rag.BUSINESS_CASES} == {0, 60}
    assert {c["fault"] for c in rag.BUSINESS_CASES} == {None, "document", "session", "same_key"}
    assert all("expected" not in c for c in rag.BUSINESS_CASES)
    assert rag.load_execution_plan()[1]["sim0"][0]["seed"] == 93001
    assert "whole_document" not in rag.BUSINESS_METHODS


@pytest.mark.parametrize("case", rag.BUSINESS_CASES, ids=lambda c: c["id"])
def test_business_methods_use_same_public_input_and_genuine_execution(case):
    async def exercise():
        world = business_world(case)
        rows = [await rag.execute_business_case(case, method, isolated("business"), world)
                for method in rag.BUSINESS_METHODS]
        assert len({r["condition_fingerprint"] for r in rows}) == 1
        assert len({r["retrieval_id"] for r in rows}) == 3
        assert all(r["genuine_search_record"] for r in rows)
        assert all(r["adapter_turns"] == 1 for r in rows)
        assert all(r["counts"]["knowledge_retrievals"] == 1 for r in rows)
        evaluated = rag.evaluate_business(rows, rag.business_rubric())
        assert all(r["matched"] for r in evaluated["attempts"]), evaluated
        assert all(s["matched"] == 1 and s["not_run"] == 8 for s in evaluated["summary"])
    asyncio.run(exercise())


def test_business_evaluator_rejects_wrong_action_side_effect_and_context():
    async def exercise():
        case = rag.BUSINESS_CASES[0]
        world = business_world(case)
        rows = [await rag.execute_business_case(case, method, isolated("oracle"), world)
                for method in rag.BUSINESS_METHODS]
        baseline = rag.evaluate_business(rows, rag.business_rubric())
        assert all(r["matched"] for r in baseline["attempts"])
        original = deepcopy(rows)
        for field, mutation in (
            ("decision_action", lambda r: r["decision"].update(action="report")),
            ("notification_count", lambda r: r["counts"].update(notifications=2)),
            ("active_incident", lambda r: r.update(incidents=[])),
            ("no_early_closure", lambda r: r["incidents"][0].update(status="resolved")),
            ("actual_search", lambda r: r.update(genuine_search_record=False)),
            ("same_public_input", lambda r: r.update(condition_fingerprint="different")),
        ):
            changed = deepcopy(rows)
            mutation(changed[0])
            result = rag.evaluate_business(changed, rag.business_rubric())
            assert field in result["attempts"][0]["mismatches"]
        oracle = rag.business_rubric()
        oracle[case["id"]]["action"] = "hold"
        assert not rag.evaluate_business(rows, oracle)["attempts"][0]["matched"]
        assert rows == original  # evaluator cannot modify execution evidence
    asyncio.run(exercise())


def test_real_mock_adapter_is_used_without_expected_input(monkeypatch):
    calls = []
    original = rag.MockOperationsAdapter.decide
    async def inspected(adapter, context):
        assert not any(key in context for key in ("expected", "manual_action", "case", "ticks", "fault"))
        calls.append(deepcopy(context))
        return await original(adapter, context)
    monkeypatch.setattr(rag.MockOperationsAdapter, "decide", inspected)
    case = rag.BUSINESS_CASES[0]
    row = asyncio.run(rag.execute_business_case(case, "mock_operations_agent", isolated("real-adapter"), business_world(case)))
    assert len(calls) == 1 and row["result"]["status"] == "accepted"


def test_failed_operation_requires_real_held_job_and_measured_no_dispatch():
    case = next(c for c in rag.BUSINESS_CASES if c["fault"] == "document")
    row = asyncio.run(rag.execute_business_case(case, "fixed_rule", isolated("held-job"), business_world(case)))
    assert row["jobs"] == [{"status": "held"}] and row["counts"]["autonomous_jobs"] == 1
    assert row["delivery_attempts"] == row["counts"]["delivery_attempts"] == 0
    assert rag.evaluate_business([row], rag.business_rubric())["attempts"][0]["matched"]
    for mutated in ({"jobs": []}, {"counts": {**row["counts"], "autonomous_jobs": 0}},
                    {"jobs": [{"status": "pending"}]}):
        changed = deepcopy(row)
        changed.update(mutated)
        assert "persisted_job" in rag.evaluate_business([changed], rag.business_rubric())["attempts"][0]["mismatches"]
    changed = deepcopy(row)
    changed["counts"]["delivery_attempts"] = 1
    assert "dispatch_count_consistent" in rag.evaluate_business([changed], rag.business_rubric())["attempts"][0]["mismatches"]


def test_business_failure_restores_product_adapter_and_closes_resources():
    from backend import autonomous
    original = autonomous.MockOperationsAdapter
    async def failure(runtime, session, context):
        raise RuntimeError("synthetic decision failure")
    case = rag.BUSINESS_CASES[0]
    directory = isolated("failure")
    with pytest.raises(RuntimeError, match="synthetic decision failure"):
        asyncio.run(rag.execute_business_case(case, "fixed_rule", directory, business_world(case), failure))
    assert not directory.exists()
    assert autonomous.MockOperationsAdapter is original


def question(query="통로 차단 이동 요청과 미응답", topic="parking_order"):
    return rag.Question(query, topic)


async def with_rig(callback, corpus="sim0", role="test_operator"):
    path = isolated("test")
    world = rag.initial_world(93001)
    async with rag.prepared_runtime(path, corpus, world, role) as (runtime, session):
        result = await callback(runtime, session)
    assert not path.exists()
    assert runtime.queries.closed and not runtime.queries.active
    assert runtime.autonomous.closed
    assert runtime.knowledge._reader is None or not runtime.knowledge._reader.is_alive()
    assert runtime.store.lock_file.closed
    return result


def test_frozen_plan_preserves_nine_slots_seven_queries_and_separate_legacy():
    declaration, plan = rag.load_execution_plan()
    assert declaration["runner"] == "pending_rag_comparison"
    assert len(plan["sim0"]) == 9 and len({s["query"] for s in plan["sim0"]}) == 7
    assert [s["seed"] for s in plan["sim0"]] == list(range(93001, 93010))
    assert len(plan["legacy"]) == 5
    assert all(s["seed"] == 1 and s["role"] == "test_operator" for s in plan["legacy"])
    for corpus, version in (("legacy", 2), ("sim0", 3)):
        manifest, sizes = rag.preflight_manifest(corpus)
        assert manifest.policy.policy_version == version
        assert all(s["bytes"] > 0 for s in sizes)


@pytest.mark.parametrize("corpus,release", [("legacy", "knowledge-demo-v2"), ("sim0", "knowledge-sim0-v3")])
def test_actual_runtime_keyword_and_whole_context_have_same_condition(corpus, release):
    async def check(runtime, session):
        first = await rag.execute_method(runtime, session, question(), "keyword_rag")
        count = runtime.store.db.execute("SELECT count(*) FROM knowledge_retrievals").fetchone()[0]
        second = await rag.execute_method(runtime, session, question(), "small_whole_document")
        assert first["condition_fingerprint"] == second["condition_fingerprint"]
        assert first["condition"]["policy"]["knowledge_release_id"] == release
        assert first["raw_context"]["retrieval_id"].startswith("ret-")
        assert "retrieval_id" not in second["raw_context"]
        assert runtime.store.db.execute("SELECT count(*) FROM knowledge_retrievals").fetchone()[0] == count == 1
        assert first["calls"]["retrieval"] == 1 and second["calls"]["retrieval"] == 0
        assert first["reader_selected"] == second["reader_selected"]
        assert first["citation_structure"]["checked"] > 0
    asyncio.run(with_rig(check, corpus))


def test_driver_prefilter_hides_operator_document_metadata_for_both_methods():
    async def check(runtime, session):
        for method in rag.METHODS:
            result = await rag.execute_method(runtime, session, question(), method)
            public = rag.encoded(result)
            assert "manual-parking-order" not in public
            assert "ref-order" not in public
            assert "가상 통로 질서" not in public
            assert all(r["topic"] == "user_guidance" for r in result["raw_context"]["references"])
            assert result["reader_selected"]["status"] == "no_match"
    asyncio.run(with_rig(check, role="driver"))


@pytest.mark.parametrize("change", [dict(status="draft"), dict(status="withdrawn"),
                                     dict(roles=["owner"]), dict(retired_at=rag.NOW)])
def test_document_access_filter_before_both_method_inputs(change):
    async def check(runtime, session):
        runtime.knowledge.document_access(rag.FACILITY, "manual-parking-order", "v2", **change)
        for method in rag.METHODS:
            actual = await rag.execute_method(runtime, session, question(), method)
            assert "manual-parking-order" not in rag.encoded(actual)
            assert "ref-order" not in rag.encoded(actual)
    asyncio.run(with_rig(check))


def test_other_facility_and_nonrelease_documents_never_enter_context():
    async def check(runtime, session):
        db = runtime.store.db
        before = rag.snapshot(runtime, session, question())
        db.execute("INSERT INTO facilities VALUES ('fac-other', 'other synthetic facility')")
        row = list(db.execute("SELECT * FROM knowledge_documents WHERE document_id='manual-parking-order' AND document_version='v2'").fetchone())
        row[0] = "fac-other"
        db.execute("INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        chunk = list(db.execute("SELECT * FROM knowledge_chunks WHERE reference_id='ref-order-conditions-v2'").fetchone())
        chunk[0], chunk[1] = "ref-other-hidden", "fac-other"
        db.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?)", chunk)
        db.commit()
        after = rag.snapshot(runtime, session, question())
        assert before["fingerprint"] == after["fingerprint"]
        for method in rag.METHODS:
            result = await rag.execute_method(runtime, session, question(), method)
            assert "ref-other-hidden" not in rag.encoded(result)
            assert not any(r["document_version"] == "v1" and r["document_id"] == "manual-parking-order"
                           for r in result["raw_context"]["references"])
    asyncio.run(with_rig(check))


@pytest.mark.parametrize("method", rag.METHODS)
@pytest.mark.parametrize("change", ["session", "membership", "relationship", "document", "policy", "clock"])
def test_change_before_publication_blocks_return(method, change):
    def mutate(runtime, session):
        db = runtime.store.db
        if change == "session":
            session.expires = time.monotonic() - 1
        elif change == "membership":
            db.execute("UPDATE memberships SET revoked_at=? WHERE user_id=?", (rag.NOW, session.username))
            db.commit()
        elif change == "relationship":
            db.execute("UPDATE object_mappings SET mapping_status='uncertain' WHERE object_id='obj-car-02'")
            db.commit()
        elif change == "document":
            runtime.knowledge.document_access(rag.FACILITY, "manual-user-guidance", "v1", status="withdrawn")
        elif change == "policy":
            db.execute("UPDATE policies SET retired_at=? WHERE policy_version=3", (rag.NOW,))
            db.commit()
        else:
            runtime.knowledge.clock = lambda: "2026-10-02T00:00:01.000000Z"
    async def check(runtime, session):
        with pytest.raises((ValueError, rag.ApiError)):
            await rag.execute_method(runtime, session, question("수락 응답", "user_guidance"), method, mutate)
    asyncio.run(with_rig(check, role="driver" if change == "relationship" else "test_operator"))


def test_complete_group_reader_and_context_cap_never_truncate(monkeypatch):
    async def check(runtime, session):
        state = rag.snapshot(runtime, session, question())
        context = rag.whole_document_context(state)
        selected = rag.deterministic_reader(question(), context)
        assert rag.verify_selected(selected["references"], state["references"])
        assert not rag.verify_selected(selected["references"][:-1], state["references"])
        monkeypatch.setattr(rag, "CONTEXT_CAP", 1)
        capped = rag.whole_document_context(state)
        assert capped["status"] == "unavailable" and not capped["references"]
        assert rag.deterministic_reader(question("우주선 연료세금", None), capped)["status"] == "error"
        monkeypatch.setattr(rag, "READER_CHAR_CAP", 1)
        assert rag.deterministic_reader(question(), context)["status"] == "error"
    asyncio.run(with_rig(check))


@pytest.mark.parametrize("method", rag.METHODS)
def test_corrupt_index_is_error_not_negative_success(method):
    async def check(runtime, session):
        path = next(runtime.knowledge.index_dir.glob("*.json"))
        for path in runtime.knowledge.index_dir.glob("*.json"):
            path.write_text("corrupted derivative", encoding="utf-8")
        with pytest.raises(ValueError):
            await rag.execute_method(runtime, session, question("우주선 연료세금", None), method)
    asyncio.run(with_rig(check))


def test_conflict_context_is_error_even_for_unrelated_question():
    async def check(runtime, session):
        runtime.knowledge.document_access(rag.FACILITY, "manual-parking-order", "v2", conflict=True)
        result = await rag.execute_method(runtime, session, question("우주선 연료세금", None), "small_whole_document")
        assert result["status"] == "error"
        assert result["reader_selected"]["status"] == "error"
        assert not result["raw_context"]["references"]
    asyncio.run(with_rig(check))


def test_expected_mutation_cannot_change_actual_and_missing_denominators(monkeypatch):
    _, full = rag.load_execution_plan()
    plan = {"sim0": [full["sim0"][0], full["sim0"][6]]}
    # Execution must not call the evaluator's expected-file loader.
    expected = rag.load_expectations()
    with monkeypatch.context() as patch:
        patch.setattr(rag, "load_expectations", lambda: pytest.fail("oracle read during execution"))
        results = asyncio.run(rag.execute_plan(plan, isolated("oracle")))
    original = deepcopy(results)
    one = rag.evaluate(results, plan, expected)
    altered = deepcopy(expected)
    altered["sim0"][plan["sim0"][0]["id"]]["required_references"] = ["oracle-poison"]
    two = rag.evaluate(results, plan, altered)
    assert results == original
    assert one["attempts"][0]["quality_match"] and not two["attempts"][0]["quality_match"]
    empty = rag.evaluate([], full, expected)
    for row in empty["summary"]:
        assert row["executed_slots"] == row["matched_slots"] == 0
        assert row["not_run_slots"] == row["registered_slots"]
        assert row["required_group_recall"]["positive_slots"] == (8 if row["corpus"] == "sim0" else 4)
        assert row["negative_cases"] == {"slots": 1, "correct": 0}
        assert row["citation_support"]["structural_rate"] is None
    failed = [{"corpus": "sim0", "slot": full["sim0"][6]["id"], "method": "keyword_rag",
               "attempt": 1, "status": "error"}]
    assert not rag.evaluate(failed, plan, expected)["attempts"][0]["quality_match"]


def test_method_reader_inputs_are_only_question_and_authorized_context(monkeypatch):
    calls = []
    original = rag.deterministic_reader
    def reader(q, context):
        assert set(vars(q)) == {"query", "topic"}
        serialized = rag.encoded(context)
        for forbidden in ("required_references", "same_expected_as", "R01-s1a-procedure", "93001", "future_path"):
            assert forbidden not in serialized
        calls.append(q)
        return original(q, context)
    monkeypatch.setattr(rag, "deterministic_reader", reader)
    async def check(runtime, session):
        for method in rag.METHODS:
            await rag.execute_method(runtime, session, question(), method)
    asyncio.run(with_rig(check))
    assert len(calls) == 2


def test_repeat_three_is_one_slot_and_new_databases_cleaned():
    _, full = rag.load_execution_plan()
    plan = {"sim0": [full["sim0"][0]]}
    path = isolated("repeat")
    results = asyncio.run(rag.execute_plan(plan, path, repeat=3))
    assert not path.exists() and len(results) == 6
    assert all(r["status"] == "executed" and r["same_condition_pair"] for r in results)
    assert len({r["raw_context"]["retrieval_id"] for r in results if r["method"] == "keyword_rag"}) == 3
    assert len({r["condition"]["run_id"] for r in results}) == 3
    evaluated = rag.evaluate(results, plan, rag.load_expectations(), repeat=3)
    assert evaluated["summary"][0]["registered_slots"] == 1
    assert evaluated["summary"][0]["attempts"] == 3
    assert evaluated["summary"][0]["matched_slots"] == 1


def test_interrupt_closes_tasks_threads_store_and_scratch(monkeypatch):
    _, full = rag.load_execution_plan()
    path = isolated("interrupt")
    runtimes = []
    async def interrupt(runtime, *args, **kwargs):
        runtimes.append(runtime)
        raise KeyboardInterrupt("synthetic interrupted comparison")
    monkeypatch.setattr(rag, "execute_method", interrupt)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(rag.execute_plan({"sim0": [full["sim0"][0]]}, path))
    assert not path.exists()
    assert runtimes[0].queries.closed and runtimes[0].store.lock_file.closed
    assert not runtimes[0].knowledge._reader.is_alive()


def test_output_and_scratch_are_bounded_and_reports_cannot_overwrite():
    with pytest.raises(ValueError):
        rag.safe_artifact_path(ROOT / "tests")
    with pytest.raises(ValueError):
        rag.safe_artifact_path(rag.ARTIFACT_ROOT)
    with pytest.raises(ValueError):
        asyncio.run(rag.execute_plan({}, isolated("invalid-repeat"), repeat=2))



def test_all_documents_missing_is_error_not_no_match():
    async def check(runtime, session):
        for document, version in (("manual-parking-order", "v2"), ("manual-entry-announcement", "v2"), ("manual-user-guidance", "v1")):
            runtime.knowledge.document_access(rag.FACILITY, document, version, status="withdrawn")
        for method in rag.METHODS:
            with pytest.raises(ValueError, match="No eligible document context"):
                await rag.execute_method(runtime, session, question("우주선 연료세금", None), method)
    asyncio.run(with_rig(check))


def test_utc_document_interval_is_half_open():
    async def check(runtime, session):
        runtime.knowledge.document_access(rag.FACILITY, "manual-parking-order", "v2",
                                          retired_at="2026-10-02T00:00:01Z")
        for clock, present in (("2026-10-01T00:00:00.000000Z", True),
                               ("2026-10-02T00:00:00.999999Z", True),
                               ("2026-10-02T00:00:01.000000Z", False)):
            runtime.knowledge.clock = lambda: clock
            state = rag.snapshot(runtime, session, question())
            assert any(ref["document_id"] == "manual-parking-order" for ref in state["references"]) == present
    asyncio.run(with_rig(check))


def test_existing_report_is_preserved():
    output = isolated("preserve") / "report.json"
    output.parent.mkdir(parents=True)
    output.write_text("existing evidence", encoding="utf-8")
    try:
        with pytest.raises(FileExistsError):
            asyncio.run(rag.run_comparison(output))
        assert output.read_text(encoding="utf-8") == "existing evidence"
    finally:
        output.unlink()
        output.parent.rmdir()


def test_cli_imports_without_pythonpath_from_another_cwd():
    import os
    import subprocess
    env = {key: value for key, value in os.environ.items()
           if key != "PYTHONPATH" and not any(name in key.upper() for name in ("API_KEY", "GOOGLE", "PARKING_LIVE_CONFIG"))}
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/compare_acceptance_rag.py"), "--help"],
                            cwd=rag.ARTIFACT_ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "--repeat" in result.stdout


def test_actual_no_match_has_no_failure_reason_and_remains_negative_success():
    async def check(runtime, session):
        for method in rag.METHODS:
            actual = await rag.execute_method(runtime, session, question("우주선 연료세금", None), method)
            assert actual["status"] == "executed"
            assert actual["reader_selected"] == {"status": "no_match", "references": []}
            if method == "keyword_rag":
                assert actual["raw_context"]["status"] == "no_match"
                assert actual["raw_context"].get("reason_code") is None
    asyncio.run(with_rig(check))


def test_provenance_rechecks_frozen_manifest_before_any_extra_file_read(tmp_path, monkeypatch):
    import json
    manifest = json.loads(rag.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["documents"][0]["file"] = "blocked-nonsensitive-sentinel.txt"
    changed = tmp_path / "changed-manifest.json"
    changed.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # No sentinel file exists. All reads other than the changed public manifest
    # fail the test, so the regression never creates or accesses a secret file.
    reads = []
    original = Path.read_bytes
    def guarded_read(path):
        reads.append(path)
        assert path == changed, "provenance attempted a non-allowlisted read"
        return original(path)
    monkeypatch.setattr(rag, "manifest_for", lambda corpus: changed)
    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(ValueError, match="Frozen legacy_manifest changed"):
        rag.source_provenance()
    assert reads == [changed]


@pytest.mark.parametrize("corpus", ["legacy", "sim0"])
@pytest.mark.parametrize("newline", ["lf", "crlf"])
def test_manifest_line_endings_preserve_runtime_index_and_raw_provenance(corpus, newline, monkeypatch):
    import hashlib
    import json
    paths = {rag.manifest_for(name).resolve() for name in ("legacy", "sim0")}
    original_read = Path.read_bytes
    def manifest_bytes(path):
        raw = original_read(path)
        if path.resolve() in paths:
            raw = raw.replace(b"\r\n", b"\n")
            if newline == "crlf":
                raw = raw.replace(b"\n", b"\r\n")
        return raw
    monkeypatch.setattr(Path, "read_bytes", manifest_bytes)
    monkeypatch.setattr(rag, "ARTIFACT_ROOT", rag.ARTIFACT_ROOT / "line-ending-fix")
    selected_path = rag.manifest_for(corpus)
    raw_hash = hashlib.sha256(selected_path.read_bytes()).hexdigest()
    assert (raw_hash == rag.FROZEN[corpus + "_manifest"]) == (newline == "lf")
    preflight, _ = rag.preflight_manifest(corpus)
    provenance = rag.source_provenance()
    assert provenance["sha256"][selected_path.relative_to(rag.ROOT).as_posix()] == raw_hash
    async def check(runtime, session):
        release = runtime.store.db.execute(
            "SELECT manifest_digest,index_file FROM knowledge_releases WHERE facility_id=? AND knowledge_release_id=?",
            (rag.FACILITY, preflight.knowledge_release_id)).fetchone()
        assert release["manifest_digest"] == "sha256:" + raw_hash
        index = json.loads((runtime.knowledge.index_dir / release["index_file"]).read_bytes())
        assert index["manifest_digest"] == "sha256:" + raw_hash
        first = await rag.execute_method(runtime, session, question(), "keyword_rag")
        second = await rag.execute_method(runtime, session, question(), "small_whole_document")
        assert first["status"] == second["status"] == "executed"
        assert first["condition_fingerprint"] == second["condition_fingerprint"]
        assert first["reader_selected"] == second["reader_selected"]
    asyncio.run(with_rig(check, corpus))


@pytest.mark.parametrize("corpus", ["legacy", "sim0"])
@pytest.mark.parametrize("newline", ["lf", "crlf"])
@pytest.mark.parametrize("mutation", ["content", "whitespace", "file_path", "bare_cr"])
def test_manifest_line_ending_allowance_still_rejects_other_byte_changes(corpus, newline, mutation, tmp_path):
    import json
    raw = rag.manifest_for(corpus).read_bytes().replace(b"\r\n", b"\n")
    if mutation == "content":
        changed = raw.replace(b'"synthetic_demo"', b'"modified_demo"', 1)
    elif mutation == "whitespace":
        changed = b" " + raw
    elif mutation == "file_path":
        filename = json.loads(raw)["documents"][0]["file"].encode("utf-8")
        changed = raw.replace(filename, b"blocked-nonsensitive-sentinel.txt", 1)
    else:
        changed = raw.replace(b"\n", b"\r", 1)
    assert changed != raw
    if newline == "crlf":
        changed = changed.replace(b"\n", b"\r\n")
    copied = tmp_path / "changed-public-manifest.json"
    copied.write_bytes(changed)
    with pytest.raises(ValueError, match="Frozen " + corpus + "_manifest changed"):
        rag.read_frozen(copied, corpus + "_manifest")


@pytest.mark.parametrize("key,path", [("inputs", rag.INPUT_PATH), ("expected", rag.EXPECTED_PATH),
                                      ("legacy_expected", rag.LEGACY_EXPECTED)])
def test_nonmanifest_frozen_pins_remain_strict_bytes(key, path, tmp_path):
    raw = path.read_bytes()
    assert b"\r\n" not in raw and b"\n" in raw
    copied = tmp_path / "changed-test-only-input.json"
    copied.write_bytes(raw.replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="Frozen " + key + " changed"):
        rag.read_frozen(copied, key)


@pytest.mark.parametrize("corpus", ["legacy", "sim0"])
def test_manual_digest_remains_raw_bytes_with_manifest_line_ending_allowance(corpus, monkeypatch):
    import json
    manifest_path = rag.manifest_for(corpus)
    manifest = json.loads(manifest_path.read_bytes())
    manual_path = (manifest_path.parent / manifest["documents"][0]["file"]).resolve()
    original_read = Path.read_bytes
    def changed_manual_bytes(path):
        raw = original_read(path)
        if path.resolve() == manual_path:
            raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return raw
    monkeypatch.setattr(Path, "read_bytes", changed_manual_bytes)
    with pytest.raises(ValueError, match="Frozen manual digest mismatch"):
        rag.preflight_manifest(corpus)
