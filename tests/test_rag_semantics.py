"""Provenance and missing-evidence gates, not automatic meaning judgments."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_rag_semantics as semantic


def material(answer="완료로 볼 수 없습니다."):
    condition = {"role": "owner", "policy": 3}
    excerpt = "이동하겠다는 응답은 이동 완료가 아니다."
    row = {"id": "probe", "question": "이동 응답은 완료인가?", "condition": condition,
           "condition_fingerprint": semantic.hashed(condition), "source_hash": "actual-response-hash",
           "answer": answer, "references": [{"reference_id": "ref1", "excerpt": excerpt}],
           "quality_eligible": bool(answer)}
    claim = {"label": "supported" if answer else "not_observed", "reason": "독립 독해 판정 예시",
        "context_conditions": "응답만 있는 조건", "exceptions": "현재 공간 회복은 별도", "action_scope": "해결 보류",
        "uncertainty": "현 관측 미확인", "answer_span": {"start": 0, "end": len(answer), "quote": answer} if answer else None,
        "evidence": [{"reference_id": "ref1", "start": 0, "end": len(excerpt), "quote": excerpt}]}
    packet = {"rows": [row]}
    review = {"packet_hash": semantic.hashed(packet), "reviewer": "independent-test-reader",
              "rows": [{k: row[k] for k in ("id", "question", "source_hash", "condition_fingerprint")} | {"claims": [claim]}]}
    return packet, review


def test_external_reading_counts_and_missing_answer():
    packet, review = material()
    assert semantic.validate_review(packet, review)["claim_counts"] == {"supported": 1}
    packet, review = material("")
    assert semantic.validate_review(packet, review)["claim_counts"] == {"not_observed": 1}
    review["rows"][0]["claims"][0]["label"] = "supported"
    with pytest.raises(ValueError, match="Absent answer"):
        semantic.validate_review(packet, review)


@pytest.mark.parametrize("field", ["packet_hash", "source_hash", "question", "condition_fingerprint"])
def test_provenance_tampering_fails(field):
    packet, review = material()
    if field == "packet_hash":
        review[field] = "other"
    else:
        review["rows"][0][field] = "other"
    with pytest.raises(ValueError, match="mismatch"):
        semantic.validate_review(packet, review)


@pytest.mark.parametrize("change", ["invented_reference", "wrong_quote", "out_of_bounds", "unreviewed_tail", "overlap", "missing_condition", "missing_evidence", "duplicate_row"])
def test_review_cannot_fabricate_or_hide_evidence(change):
    packet, review = material()
    annotation = review["rows"][0]
    claim = annotation["claims"][0]
    if change == "invented_reference":
        claim["evidence"][0]["reference_id"] = "not-returned"
    elif change == "wrong_quote":
        claim["evidence"][0]["quote"] = "의미 비슷해도 조작한 인용"
    elif change == "out_of_bounds":
        claim["evidence"][0]["end"] = 999
    elif change == "unreviewed_tail":
        claim["answer_span"].update(end=1, quote=packet["rows"][0]["answer"][:1])
    elif change == "overlap":
        annotation["claims"].append(deepcopy(claim))
    elif change == "missing_condition":
        del claim["exceptions"]
    elif change == "missing_evidence":
        claim["evidence"] = []
    else:
        review["rows"].append(deepcopy(annotation))
    with pytest.raises(ValueError):
        semantic.validate_review(packet, review)


def test_tool_does_not_replace_independent_judgment_with_keywords():
    packet, review = material("우주선은 내일 출발합니다.")
    # Even an incorrect external verdict is not silently 'fixed' by keywords.
    # Correctness is the independent reviewer's responsibility, exposed in JSON.
    assert semantic.validate_review(packet, review)["claim_counts"]["supported"] == 1
    review["rows"][0]["claims"][0]["label"] = "unsupported"
    assert semantic.validate_review(packet, review)["claim_counts"]["unsupported"] == 1


def test_controlled_answer_uses_real_query_and_cleanup_without_evaluator(monkeypatch):
    directory = semantic.OUTPUT_ROOT / "scratch" / ("unit-" + uuid4().hex)
    monkeypatch.setattr(semantic.rag, "ARTIFACT_ROOT", semantic.OUTPUT_ROOT / "scratch")
    monkeypatch.setattr(semantic, "validate_review", lambda *_: pytest.fail("Executor consulted adjudication"))
    answer = "독립 판정에 제출할 시험 답변"
    row = asyncio.run(semantic.collect_case("unit", directory, reply=answer))
    assert row["answer"] == answer and row["references"]
    assert row["response"]["tool_results"][0]["result"]["status"] == "matched"
    assert "label" not in row and row["provider_calls"] == 0
    assert row["cleanup"] == {"scratch_removed": True, "queries_closed": True, "query_active": 0,
        "autonomous_closed": True, "reader_alive": False, "store_lock_closed": True}


def test_output_guard_and_no_overwrite():
    with pytest.raises(ValueError, match="inside"):
        semantic.bounded(semantic.rag.ROOT / "tests/expected/invented.json")
    path = semantic.OUTPUT_ROOT / "scratch" / ("unit-write-" + uuid4().hex + ".json")
    try:
        semantic.save_new(path, {"one": 1})
        with pytest.raises(FileExistsError):
            semantic.save_new(path, {"two": 2})
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture
def collector_io_fake(tmp_path, monkeypatch):
    """No product Runtime, SQLite, provider, adjudication or result file writes."""
    root = tmp_path / "local-rag-completion"
    monkeypatch.setattr(semantic, "OUTPUT_ROOT", root)
    conditions = tmp_path / "conditions.md"
    conditions.write_text("Declared before execution", encoding="utf-8")
    inputs, expected = tmp_path / "inputs.json", tmp_path / "expected.json"
    inputs.write_text("{}", encoding="utf-8")
    expected.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(semantic.rag, "INPUT_PATH", inputs)
    monkeypatch.setattr(semantic.rag, "EXPECTED_PATH", expected)
    preflight, calls, saved = [], [], {}
    monkeypatch.setattr(semantic.rag, "read_frozen", lambda path, key: preflight.append(key))
    async def fake_case(case_id, directory, **kwargs):
        assert preflight[-2:] == ["inputs", "expected"]
        assert semantic.rag.ARTIFACT_ROOT == directory.parent
        calls.append((case_id, directory))
        return {"id": case_id, "provider_calls": 0}
    async def fake_recovery(case, method, directory, **kwargs):
        assert preflight[-2:] == ["inputs", "expected"]
        assert semantic.rag.S2S3_ROOT == directory.parent
        assert kwargs == {"recovery_probe": True}
        calls.append((method, directory))
        return {"method": method, "provider_calls": 0}
    def fake_save(path, value):
        path = semantic.bounded(path)
        assert path not in saved
        saved[path] = value
    monkeypatch.setattr(semantic, "collect_case", fake_case)
    monkeypatch.setattr(semantic.rag, "execute_s2s3_case", fake_recovery)
    monkeypatch.setattr(semantic, "save_new", fake_save)
    return root, conditions, calls, saved


def test_collector_io_without_histories_isolated_fake(collector_io_fake):
    root, conditions, calls, saved = collector_io_fake
    output = root / "new-run/evidence.json"
    packet = asyncio.run(semantic.collect(output, conditions=conditions))
    assert len(packet["rows"]) == 10 and len(packet["recovery"]) == 3
    assert {x["status"] for x in packet["history_inputs"]} == {"not_supplied"}
    assert {x["status"] for x in packet["reused"]} == {"not_supplied"}
    assert all(path.is_relative_to(output.parent) for _, path in calls)
    assert len(saved) == 14 and all(path.is_relative_to(output.parent) for path in saved)
    assert saved[output] == packet and not output.exists()
    # Another fresh run is isolated; it does not reuse the first raw directory.
    second = root / "second-run/evidence.json"
    asyncio.run(semantic.collect(second, conditions=conditions))
    assert second in saved and not second.exists()


def test_collector_io_explicit_histories_fake(collector_io_fake, tmp_path):
    root, conditions, _, _ = collector_io_fake
    ref = {"reference_id": "returned-ref", "excerpt": "Actual historical excerpt"}
    response = {"answer": "Historical answer", "tool_results": [
        {"name": "search_operating_knowledge", "result": {"references": [ref]}}]}
    pair = {"question": "historical question", "condition": {"role": "owner"}, "condition_fingerprint": "same"}
    documents = {"recovered": {"cases": [{"stored": {"response": response, "context_stamp": "stamp"}}]},
                 "retrieval": {"attempts": [pair, pair]}, "business": {"runs": [{"attempts": [1, 2]}]}}
    paths = {}
    for name, value in documents.items():
        paths[name] = tmp_path / (name + ".json")
        paths[name].write_text(json.dumps(value), encoding="utf-8")
    packet = asyncio.run(semantic.collect(root / "explicit/evidence.json", conditions=conditions,
        recovered_live=paths["recovered"], retrieval_history=paths["retrieval"], business_history=paths["business"]))
    assert len(packet["rows"]) == 11
    assert packet["rows"][-1]["kind"] == "historical_live"
    assert all(x["status"] == "available" and x["file_sha256"] for x in packet["history_inputs"])
    assert packet["reused"][0]["condition_matched_pairs"] == 1
    assert packet["reused"][1]["attempts"] == 2


def test_collector_io_unavailable_histories_do_not_require_personal_files_fake(collector_io_fake, tmp_path):
    root, conditions, _, _ = collector_io_fake
    packet = asyncio.run(semantic.collect(root / "missing-history/evidence.json", conditions=conditions,
        recovered_live=tmp_path / "missing.json", retrieval_history=tmp_path / "also-missing.json"))
    assert len(packet["rows"]) == 10
    assert [x["status"] for x in packet["history_inputs"]] == ["unavailable", "unavailable", "not_supplied"]


@pytest.mark.parametrize("failure", ["conditions_missing", "conditions_empty", "frozen"])
def test_collector_io_preflight_before_execution_fake(collector_io_fake, monkeypatch, failure):
    root, conditions, calls, saved = collector_io_fake
    if failure == "conditions_missing":
        conditions.unlink()
    elif failure == "conditions_empty":
        conditions.write_text(" ", encoding="utf-8")
    else:
        def fail_frozen(*args):
            raise ValueError("Frozen input mismatch")
        monkeypatch.setattr(semantic.rag, "read_frozen", fail_frozen)
    with pytest.raises(ValueError):
        asyncio.run(semantic.collect(root / "preflight/evidence.json", conditions=conditions))
    assert not calls and not saved and not (root / "preflight").exists()


@pytest.mark.parametrize("existing", ["evidence.json", "raw", "scratch", "scratch-recovery"])
def test_collector_io_existing_output_or_resources_block_before_execution_fake(collector_io_fake, existing):
    root, conditions, calls, saved = collector_io_fake
    run = root / "existing-run"
    run.mkdir(parents=True)
    path = run / existing
    if existing.endswith(".json"):
        path.write_text("old report", encoding="utf-8")
    else:
        path.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(semantic.collect(run / "evidence.json", conditions=conditions))
    assert not calls and not saved
    if path.is_file():
        assert path.read_text(encoding="utf-8") == "old report"


def test_collector_io_cli_requires_conditions_and_forwards_optional_paths_fake(monkeypatch, tmp_path):
    captured = []
    async def fake_collect(output, **kwargs):
        captured.append((output, kwargs))
        return {"rows": [], "recovery": []}
    monkeypatch.setattr(semantic, "collect", fake_collect)
    output = tmp_path / "run/evidence.json"
    with pytest.raises(SystemExit) as error:
        semantic.main(["collect", "--output", str(output)])
    assert error.value.code == 2 and not captured
    flags = {"conditions": tmp_path / "conditions.md", "recovered-live": tmp_path / "recovered.json",
             "retrieval-history": tmp_path / "retrieval.json", "business-history": tmp_path / "business.json"}
    args = ["collect", "--output", str(output)]
    for name, path in flags.items():
        args.extend(["--" + name, str(path)])
    semantic.main(args)
    assert captured == [(output, {name.replace("-", "_"): path for name, path in flags.items()})]
