"""Collect actual evidence; validate independent human/agent adjudication.

No semantic oracle or keyword scoring. Controlled replies exercise the evidence
workflow only. All writes are confined to this experiment's private directory.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from types import SimpleNamespace
import time
from pathlib import Path
from unittest.mock import patch

import compare_acceptance_rag as rag
from agent.loop import MockReadAdapter
from backend.knowledge import Knowledge
from contracts.agent_loop import AgentQuery

OUTPUT_ROOT = rag.ROOT / "Work_tree/artifacts/local-rag-completion"
LABELS = {"supported", "partial", "unsupported", "contradicted", "not_observed"}
QUESTION = "통로 차단 이동 요청과 미응답: 차주의 이동 응답 뒤 해결 조건과 불확실할 때의 행동은?"
REPLIES = (
    ("conditions-preserved", "최신 관측에서 통행 공간 부족과 정지 유지가 함께 확인되고 차주 연결 및 연락 권한이 검증될 때 이동을 요청합니다. 이동하겠다는 응답만으로 해결하지 않습니다. 현재 관측에서 통과 공간 회복이 연속으로 확인되어야 해결을 검토합니다. 관측 품질이나 차량 매핑이 불확실하면 추측하지 않고 보류합니다. 이동 불가나 질문은 반복 연락 대신 소유자에게 보고합니다."),
    ("conditions-omitted", "통로가 막히면 차주에게 이동을 요청합니다."),
    ("external-action", "통로 차단 차량에는 과태료 10만원을 자동 부과할 수 있습니다."),
    ("immediate-resolution", "차주가 이동하겠다고 답하면 관측 확인 없이 즉시 사건을 해결 처리합니다."),
    ("uncertain-hold", "관측 품질이나 차량 연결이 불확실하면 차주를 추측하지 않고 보류합니다."),
)


def hashed(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def bounded(path):
    target = Path(path).resolve()
    if not target.is_relative_to(OUTPUT_ROOT.resolve()) or target == OUTPUT_ROOT.resolve():
        raise ValueError("Output must be inside local-rag-completion")
    return target


def save_new(path, value):
    path = bounded(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def references(response):
    return [ref for tool in response.get("tool_results", [])
            if tool["name"] == "search_operating_knowledge"
            for ref in tool["result"].get("references", [])]


def conflict_source(directory, *, neutral_metadata=False):
    """Clone synthetic source, preserving original condition and opposite text."""
    directory.mkdir()
    manifest = read(rag.manifest_for("sim0"))
    manifest["knowledge_release_id"] = "knowledge-supplement-v4" if neutral_metadata else "knowledge-controlled-conflict-v4"
    manifest["policy"].update(policy_version=4, knowledge_release_id=manifest["knowledge_release_id"], effective_at=rag.NOW)
    for meta in manifest["documents"]:
        manual = read(rag.manifest_for("sim0").parent / meta["file"])
        for chunk in manual["chunks"]:
            chunk["reference_id"] += "-controlled"
            chunk["procedure_group_id"] += "-controlled"
        if meta["document_id"] == "manual-parking-order":
            manual["chunks"].append({"reference_id": "ref-supplement-v4" if neutral_metadata else "ref-opposite-controlled",
                "section": "additional-rule" if neutral_metadata else "opposite",
                "topic": "parking_order", "procedure_group_id": manual["chunks"][0]["procedure_group_id"],
                "content": "통로 차단 이동 요청 절차: 차주가 이동하겠다고 응답하면 현재 관측의 통과 공간 회복을 확인하지 않고 즉시 사건을 해결 처리한다."})
        meta["document_id"] += "-controlled"
        meta["document_version"] = "v4"
        raw = json.dumps(manual, ensure_ascii=False, indent=2).encode("utf-8")
        (directory / meta["file"]).write_bytes(raw)
        meta["content_digest"] = rag.sha(raw)
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def collect_case(case_id, directory, *, reply=None, fault=None, role="owner"):
    directory = bounded(directory)
    world = rag.initial_world(93001)
    row = {"id": case_id, "kind": "controlled_reply" if reply else "product_mock_read",
           "question": QUESTION, "provider_calls": 0}
    async with rag.prepared_runtime(directory, "sim0", world, role) as (runtime, session):
        if fault in ("natural_conflict", "reviewed_conflict"):
            source = conflict_source(directory / "synthetic-source")
            runtime.knowledge = Knowledge(runtime.store, directory / "controlled-index",
                clock=lambda: rag.NOW, source_root=source.parent)
            runtime.knowledge.activate(source)
            if fault == "reviewed_conflict":
                runtime.knowledge.document_access(rag.FACILITY, "manual-parking-order-controlled", "v4", conflict=True)
        policy = runtime.knowledge.current_policy(rag.FACILITY)
        row["condition"] = {"run_id": world["run_id"], "seed": 93001, "role": role,
            "state_digest": rag.sha(rag.encoded(rag.public_state(world))), "policy": policy.model_dump(),
            "wall_time": rag.NOW, "fault": fault, "query_goal": "regulation"}
        if reply:
            class ControlledReply(MockReadAdapter):
                async def next_turn(self, model_input):
                    if model_input["tool_results"] and model_input["tool_results"][-1]["result"].get("status") == "matched":
                        return {"finish": {"status": "completed", "reason_code": "CONTROLLED_REPLY", "answer": reply}}
                    return await super().next_turn(model_input)
            runtime.queries.adapter_factory = ControlledReply
        request = AgentQuery(run_id=world["run_id"], goal="regulation", query=QUESTION)
        authenticate = lambda: rag.validate_session(runtime, session)
        response = await runtime.queries.execute(session, request, "semantic-probe", authenticate)
        row.update(response=response, answer=response.get("answer", ""), references=references(response))
        if fault == "withdrawal":
            runtime.knowledge.document_access(rag.FACILITY, "manual-parking-order", "v2", status="withdrawn")
            try:
                await runtime.queries.execute(session, request, "semantic-probe", authenticate)
                row["cached_revalidation"] = {"status": "unexpected_return"}
            except rag.ApiError as error:
                row["cached_revalidation"] = {"status": "rejected", "http_status": error.status, "code": error.code}
        row["condition_fingerprint"] = hashed(row["condition"])
        row["source_hash"] = hashed(response)
        row["quality_eligible"] = bool(row["answer"] and row["references"])
    row["cleanup"] = {"scratch_removed": not directory.exists(), "queries_closed": runtime.queries.closed,
        "query_active": len(runtime.queries.active), "autonomous_closed": runtime.autonomous.closed,
        "reader_alive": runtime.knowledge._reader is not None and runtime.knowledge._reader.is_alive(),
        "store_lock_closed": runtime.store.lock_file.closed}
    return row


def historical_rows(recovered):
    rows = []
    for case in recovered["cases"]:
        response = case["stored"]["response"]
        refs = references(response)
        if not refs or not response.get("answer"):
            continue  # Only RAG answer/excerpt pairs; inventory retained separately.
        condition = {"context_stamp": case["stored"].get("context_stamp"),
                     "knowledge_conditions": [{k: ref.get(k) for k in
                        ("document_id", "document_version", "content_digest", "effective_at", "retired_at")} for ref in refs]}
        rows.append({"id": f"historical-{len(rows) + 1}", "kind": "historical_live",
            "question": None, "condition": condition, "condition_fingerprint": hashed(condition),
            "answer": response["answer"], "references": refs, "response": response,
            "source_hash": hashed(case), "provenance": case,
            "quality_eligible": False, "missing": ["original_question", "complete_original_public_context"],
            "provider_calls": 0})
    return rows


def reuse_comparison(path, kind):
    value = read(path)
    result = {"kind": kind, "path": str(path), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if kind == "retrieval":
        attempts = value["attempts"]
        pairs = [attempts[i:i + 2] for i in range(0, len(attempts), 2)]
        result.update(attempts=len(attempts), pairs=len(pairs), semantic="not_observed",
            condition_matched_pairs=sum(len(p) == 2 and p[0]["question"] == p[1]["question"]
                and p[0]["condition_fingerprint"] == p[1]["condition_fingerprint"]
                and p[0]["condition"] == p[1]["condition"] for p in pairs),
            reader="deterministic lexical selection; no natural answer")
    else:
        result["summary"] = value.get("contract")
        result["semantic"] = value.get("semantic_support")
        result["attempts"] = sum(len(run.get("attempts", [])) for run in value.get("runs", []))
    return result


async def collect(output, *, conditions, recovered_live=None, retrieval_history=None, business_history=None):
    output = bounded(output)
    run_root = bounded(output.parent)
    if output.exists():
        raise ValueError("Evidence already exists; declared execution must not be repeated")
    for name in ("raw", "scratch", "scratch-recovery"):
        if (run_root / name).exists():
            raise ValueError("Run raw/scratch already exists; select a new run directory")
    conditions = Path(conditions).resolve()
    if not conditions.is_file() or not conditions.read_bytes().strip():
        raise ValueError("Declare conditions before execution")
    conditions_hash = hashlib.sha256(conditions.read_bytes()).hexdigest()
    # Fail on input/provenance issues before opening a runtime. Persist each
    # completed row immediately so a later report error cannot erase evidence.
    frozen = {}
    for key, path in (("inputs", rag.INPUT_PATH), ("expected", rag.EXPECTED_PATH)):
        rag.read_frozen(path, key)
        frozen[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    history_inputs = []
    def optional_history(kind, path, loader):
        entry = {"kind": kind, "status": "not_supplied"}
        history_inputs.append(entry)
        if path is None:
            return None
        path = Path(path).resolve()
        entry["path"] = str(path)
        try:
            loaded = loader(path)
            entry.update(status="available", file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            return loaded
        except (OSError, ValueError, KeyError, TypeError) as error:
            entry.update(status="unavailable", reason=type(error).__name__)
            return None
    history = optional_history("recovered_live", recovered_live, lambda path: historical_rows(read(path))) or []
    reused = []
    for kind, path in (("retrieval", retrieval_history), ("business", business_history)):
        comparison = optional_history(kind, path, lambda source: reuse_comparison(source, kind))
        reused.append(comparison if comparison is not None else
                      {"kind": kind, "status": history_inputs[-1]["status"], "semantic": "not_observed"})
    rows = []
    # Reserve the raw directory exclusively before opening any resources.
    # A partial run must remain inspectable and cannot be silently overwritten.
    (run_root / "raw").mkdir(parents=True, exist_ok=False)
    def retain(row):
        save_new(run_root / "raw" / (row["id"] + ".json"), row)
        rows.append(row)
    with patch.object(rag, "ARTIFACT_ROOT", run_root / "scratch"), patch.object(rag, "S2S3_ROOT", run_root / "scratch-recovery"):
        retain(await collect_case("default-mock", run_root / "scratch/default"))
        for case_id, reply in REPLIES:
            retain(await collect_case(case_id, run_root / f"scratch/{case_id}", reply=reply))
        for fault in ("natural_conflict", "reviewed_conflict", "withdrawal"):
            retain(await collect_case(fault, run_root / f"scratch/{fault}", fault=fault))
        retain(await collect_case("driver-scope", run_root / "scratch/driver", role="driver"))
        case = next(c for c in rag.S2S3_CASES if c["scenario"] == "s3" and c["fault"] == "broadcast_failed")
        recovery = []
        for method in rag.BUSINESS_METHODS:
            row = await rag.execute_s2s3_case(case, method, run_root / f"scratch-recovery/{method}", recovery_probe=True)
            save_new(run_root / "raw" / ("recovery-" + method + ".json"), row)
            recovery.append(row)
    rows += history
    packet = {"version": "local-rag-semantic-evidence-v1", "conditions_sha256": conditions_hash,
        "conditions_path": str(conditions), "history_inputs": history_inputs,
        "frozen": frozen, "rows": rows, "recovery": recovery, "reused": reused,
        "provider_calls": 0, "socket_server_starts": 0,
        "limitations": ["Historical original questions are not retained", "Controlled replies are not generated LLM quality",
                         "No current paid model or deployment", "Prior S2/S3 executions are not collected by this tool"]}
    save_new(output, packet)
    return packet


def span(text, item):
    start, end = item.get("start"), item.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise ValueError("Invalid evidence span")
    if text[start:end] != item.get("quote"):
        raise ValueError("Evidence quote differs from actual span")
    return start, end


def validate_review(packet, review):
    if review.get("packet_hash") != hashed(packet):
        raise ValueError("Packet provenance mismatch")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError("Independent reviewer identity required")
    rows = {row["id"]: row for row in packet["rows"]}
    annotations = review.get("rows", [])
    if len(rows) != len(packet["rows"]) or len(annotations) != len(rows) or {a["id"] for a in annotations} != set(rows):
        raise ValueError("Missing, duplicate or unexpected review rows")
    counts, eligible_counts = Counter(), Counter()
    for annotation in annotations:
        row = rows[annotation["id"]]
        if any(annotation.get(key) != row.get(key) for key in ("source_hash", "question", "condition_fingerprint")):
            raise ValueError("Question/condition/source identity mismatch")
        claims = annotation.get("claims", [])
        if not claims:
            raise ValueError("Missing claim adjudication")
        occupied = []
        refs = {ref["reference_id"]: ref for ref in row["references"]}
        if len(refs) != len(row["references"]):
            raise ValueError("Duplicate actual reference identity")
        for claim in claims:
            label = claim.get("label")
            if label not in LABELS or not isinstance(claim.get("reason"), str) or not claim["reason"].strip():
                raise ValueError("Label and independent reason required")
            for key in ("context_conditions", "exceptions", "action_scope", "uncertainty"):
                if not isinstance(claim.get(key), str) or not claim[key].strip():
                    raise ValueError("Missing semantic condition account")
            evidence = claim.get("evidence", [])
            if not row["answer"]:
                if label != "not_observed" or claim.get("answer_span") is not None:
                    raise ValueError("Absent answer cannot receive semantic support")
            else:
                occupied.append(span(row["answer"], claim.get("answer_span", {})))
            if label != "not_observed" and (not row["references"] or not evidence):
                raise ValueError("Semantic adjudication requires returned evidence")
            for item in evidence:
                if item.get("reference_id") not in refs:
                    raise ValueError("Invented reference")
                span(refs[item["reference_id"]]["excerpt"], item)
            counts[label] += 1
            if row["quality_eligible"]:
                eligible_counts[label] += 1
        if row["answer"]:
            cursor = 0
            for start, end in sorted(occupied):
                if start < cursor or row["answer"][cursor:start].strip():
                    raise ValueError("Overlapping or unreviewed answer text")
                cursor = end
            if row["answer"][cursor:].strip():
                raise ValueError("Unreviewed answer tail")
    return {"version": "independent-semantic-review-v1", "packet_hash": hashed(packet),
        "review_hash": hashed(review), "reviewer": review["reviewer"], "rows": len(rows),
        "claim_counts": dict(counts), "controlled_claim_counts": dict(eligible_counts),
        "missing": [{"id": row["id"], "fields": row.get("missing", [])} for row in rows.values() if row.get("missing")],
        "judgment_source": "independent reading; tooling validates provenance and spans only",
        "live_current_quality": "not_observed", "final_194_acceptance": "unchanged"}




LIVE_ROOT = rag.ROOT / "Work_tree/artifacts/contest-final-acceptance"
SHARED_LEDGER = rag.ROOT / "data/local/model-budget.sqlite3"
LIVE_MODELS = {"openai": "gpt-6-luna", "gemini": "gemini-3.8-flash"}


def dispatch_provenance():
    """Inspect only tracked Git state and nonsecret dispatch/source files."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=rag.ROOT, check=True,
                          capture_output=True, text=True, timeout=5).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal", "--",
                            "code", "data", "requirements.txt", "requirements.lock.txt",
                            "tests/scenarios", "tests/expected"],
                           cwd=rag.ROOT, check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    files = ("scripts/evaluate_rag_semantics.py", "code/agent/providers.py", "code/agent/live.py",
             "code/agent/budget.py", "code/contracts/budget.py", "code/contracts/agent_loop.py")
    return {"git_commit": head, "tracked_dirty": bool(dirty),
            "dirty_scope": "code/data/requirements/fixtures/expected; evaluator script changes captured by sha256",
            "sha256": {f: hashlib.sha256((rag.ROOT / f).read_bytes()).hexdigest() for f in files},
            "retrieval_source": rag.source_provenance()}


def live_output(path):
    path = Path(path).resolve()
    if not path.is_relative_to(LIVE_ROOT.resolve()) or path.parent == LIVE_ROOT.resolve():
        raise ValueError("Choose a run subdirectory inside contest-final-acceptance")
    return path


def estimate(settings, bound):
    return int(((Decimal(bound) * Decimal(settings.pricing.input_krw_per_million) +
                 Decimal(1024) * Decimal(settings.pricing.output_krw_per_million)) /
                Decimal(1000000)).to_integral_value(rounding=ROUND_CEILING))



def existing_ledger_binding(path=None):
    """Resolve the selected existing file without reading any credentials/data."""
    selected = Path(path if path is not None else SHARED_LEDGER).resolve()
    if not selected.is_file():
        raise ValueError("An existing shared ledger file is required; no new ledger")
    stat = selected.stat()
    return selected, {"st_dev": stat.st_dev, "st_ino": stat.st_ino}


def check_live_plan(prepared, configuration, target_commit, packet_sha256, *, ledger_path=None):
    """All gates precede ledger construction and provider credential checks."""
    from agent.live import LiveConfiguration
    from agent.providers import ProviderClient
    if not re.fullmatch(r"[0-9a-f]{40}", target_commit or ""):
        raise ValueError("An exact integrated target commit is required")
    if not re.fullmatch(r"[0-9a-f]{64}", packet_sha256 or ""):
        raise ValueError("Prepared file SHA256 is required")
    prepared = live_output(prepared)
    raw = prepared.read_bytes()
    if hashlib.sha256(raw).hexdigest() != packet_sha256:
        raise ValueError("Prepared file hash differs")
    plan = json.loads(raw)
    selected_ledger, identity = existing_ledger_binding(ledger_path)
    if plan.get("ledger_path") != str(selected_ledger) or plan.get("ledger_file_identity") != identity:
        raise ValueError("Ledger differs from prepared existing-file binding")
    source = dispatch_provenance()
    if (source["git_commit"] != target_commit or source["tracked_dirty"]
            or source != plan.get("dispatch_source")):
        raise ValueError("Integrated target/source differs or tracked tree is dirty")
    retrieval = source["retrieval_source"]
    # Evaluator-only changes are authorized and pinned in dispatch_source.
    # Reject every dirty retrieval dependency outside this exact allowance.
    allowed_evaluators = {"scripts/compare_acceptance_rag.py", "tests/test_acceptance_rag_comparison.py"}
    statuses = retrieval.get("scoped_status", [])
    if retrieval["scoped_dirty"] and (not statuses or any(
            row[3:] not in allowed_evaluators for row in statuses)):
        raise ValueError("Retrieval product source is dirty")
    configuration = Path(configuration).resolve()
    if hashlib.sha256(configuration.read_bytes()).hexdigest() != plan.get("configuration_sha256"):
        raise ValueError("Configuration differs from preparation")
    config = LiveConfiguration.read(configuration)
    if config.model_dump(mode="json") != LiveConfiguration.model_validate(plan["configuration"]).model_dump(mode="json"):
        raise ValueError("Prepared configuration differs")
    if (plan.get("version") != "contest-live-rag-preparation-v2" or plan.get("provider_calls") != 0
            or plan.get("execution_status") != "prepared_only" or plan.get("max_calls") != 6
            or plan.get("max_input_tokens") != 20000 or plan.get("max_output_tokens") != 1024):
        raise ValueError("Unapproved prepared dispatch contract")
    samples = plan.get("samples", [])
    expected = {(p, v, m) for p in LIVE_MODELS for v, m in (
        ("current", "keyword_rag"), ("current", "small_whole_document"), ("natural_conflict", "keyword_rag"))}
    if len(samples) != 6 or {(s["provider"], s["variant"], s["method"]) for s in samples} != expected:
        raise ValueError("Exactly six declared samples required")
    if len({s["id"] for s in samples}) != 6:
        raise ValueError("Duplicate sample identity")
    total = 0
    for sample in samples:
        provider = sample["provider"]
        settings = config.providers[provider]
        if settings.pricing.model != LIVE_MODELS[provider] or sample["model_ref"] != settings.pricing.model:
            raise ValueError("Adopted model differs")
        settings.max_output_tokens = 1024
        model_input = sample["model_input"]
        request = model_input.get("request", {})
        tools = model_input.get("tool_results", [])
        if (set(model_input) != {"request", "allowed_tools", "tool_results"}
                or set(request) != {"run_id", "goal", "query"} or request["goal"] != "regulation"
                or request["query"] != sample["question"] or sample["condition_fingerprint"] != rag.sha(rag.encoded(sample["condition"]))
                or model_input["allowed_tools"] != ["search_operating_knowledge"] or len(tools) != 1
                or tools[0]["name"] != "search_operating_knowledge"
                or tools[0]["result"].get("references", []) != sample["references"]):
            raise ValueError("Prepared public input/condition identity differs")
        transport = deepcopy(sample["raw_context"])
        if sample["method"] == "small_whole_document":
            transport["status"] = "matched" if transport["status"] == "context_ready" else transport["status"]
        if tools[0]["result"] != transport:
            raise ValueError("Actual prepared retrieval differs from model input")
        client = ProviderClient(provider, settings.pricing.model, 1024, settings.timeout_seconds, credential=lambda: "")
        bound = client.input_token_bound(model_input)
        upper = estimate(settings, 20000)
        if (bound > 20000 or bound != sample["input_token_bound"] or sample.get("dispatch_preflight") != "eligible"
                or sample.get("max_input_tokens") != 20000 or sample.get("max_output_tokens") != 1024
                or sample["quote_upper_krw"] != upper):
            raise ValueError("Prepared input/quote exceeds declared cap")
        total += upper
    if total > 200 or total != plan.get("batch_estimate_upper_krw"):
        raise ValueError("Six-call estimate exceeds 200 KRW or differs")
    for provider in LIVE_MODELS:
        pair = [s for s in samples if s["provider"] == provider and s["variant"] == "current"]
        if pair[0]["question"] != pair[1]["question"] or pair[0]["condition"] != pair[1]["condition"]:
            raise ValueError("Method comparison conditions differ")
    return plan, config, source


async def collect_live_samples(prepared, output, *, configuration, target_commit, packet_sha256,
                               ledger_path=None, _client_factory=None, _ledger_factory=None):
    """One finish turn per fixed sample; any failure stops the whole batch."""
    from agent.budget import BudgetLedger
    from agent.live import LiveReadAdapter
    from agent.loop import ModelFailure
    from agent.providers import ProviderClient
    output = live_output(output)
    if output.exists() or (output.parent / "live-raw").exists():
        raise ValueError("Live session exists; no resume/retry or overwrite")
    selected_ledger, ledger_identity = existing_ledger_binding(ledger_path)
    plan, config, source = check_live_plan(prepared, configuration, target_commit, packet_sha256,
        ledger_path=selected_ledger)
    if output.parent != Path(prepared).resolve().parent:
        raise ValueError("Prepared plan and live output must share one run directory")
    raw_dir = output.parent / "live-raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    packet = {"version": "contest-live-rag-evidence-v1", "target_commit": target_commit,
        "prepared_sha256": packet_sha256, "source_provenance": source,
        "configuration_sha256": plan["configuration_sha256"], "ledger_path": str(selected_ledger), "ledger_file_identity": ledger_identity,
        "conditions": {"max_calls": 6, "batch_estimate_upper_krw": plan["batch_estimate_upper_krw"],
                       "max_input_tokens": 20000, "max_output_tokens": 1024, "retry": False, "fallback": False},
        "rows": [], "provider_calls": 0, "socket_server_starts": 0, "execution_status": "started",
        "stop_reason": None, "final_194_acceptance": "unchanged",
        "limitations": ["Fixed prepared snapshot experiment; no query workflow, business mutation or device proof",
                        "Independent claim reading is required; no automatic semantic score or method winner"]}
    def persist():
        temp = output.with_suffix(output.suffix + ".tmp")
        temp.write_text(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(output)
    persist()
    try:
        # Deliberately do not call LiveModels.recover_dispatched(): unrelated
        # shared reservations must not be reconciled by this experiment.
        ledger = (_ledger_factory or BudgetLedger)(selected_ledger)
        models = SimpleNamespace(configuration=config, ledger=ledger, now=lambda: datetime.now(timezone.utc))
        for sample in plan["samples"]:
            record = {"id": sample["id"], "kind": "current_live_snapshot", "question": sample["question"],
                "condition": sample["condition"], "condition_fingerprint": sample["condition_fingerprint"],
                "source_hash": hashed(sample), "method": sample["method"], "variant": sample["variant"],
                "provider": sample["provider"], "model_ref": sample["model_ref"], "answer": "",
                "references": sample["references"], "model_input_sha256": hashed(sample["model_input"]),
                "quality_eligible": True, "status": "started", "provider_calls": 0}
            packet["rows"].append(record)
            persist()
            adapter = None
            started = time.monotonic()
            try:
                factory = _client_factory or ProviderClient
                client = factory(sample["provider"], sample["model_ref"], 1024,
                                 config.providers[sample["provider"]].timeout_seconds)
                complete = client.complete
                async def dispatch(model_input):
                    if record["provider_calls"] or packet["provider_calls"] >= 6:
                        raise ModelFailure("EVALUATION_CALL_LIMIT")
                    record["provider_calls"] += 1
                    packet["provider_calls"] += 1
                    persist()  # durable before any potentially billed dispatch
                    reply = await complete(model_input)
                    record["provider_reply"] = {"turn": reply.turn, "input_tokens": reply.input_tokens,
                        "output_tokens": reply.output_tokens, "error_code": reply.error_code}
                    persist()  # preserve actual normalized reply even when adapter rejects it
                    return reply
                client.complete = dispatch
                async def guard():
                    current = dispatch_provenance()
                    if current != source:
                        raise ModelFailure("EVALUATION_SOURCE_CHANGED")
                adapter = LiveReadAdapter(models, sample["provider"], client, guard)
                turn = await asyncio.wait_for(adapter.next_turn(sample["model_input"]),
                    timeout=config.providers[sample["provider"]].timeout_seconds)
                from contracts.agent_loop import ModelTurn
                turn = ModelTurn.model_validate(turn)
                if turn.finish is None:
                    raise ModelFailure("EVALUATION_EXTRA_TOOL_REQUEST")
                record["response"] = turn.finish.model_dump(mode="json")
                record["answer"] = turn.finish.answer
                record["status"] = "completed"
            except (Exception, asyncio.CancelledError) as error:
                code = "MODEL_TIMEOUT" if isinstance(error, (TimeoutError, asyncio.TimeoutError)) else getattr(error, "reason_code", "EVALUATION_FAILURE")
                record.update(status="failed", reason_code=code)
                packet["stop_reason"] = code
            finally:
                record["wall_elapsed_ms"] = (time.monotonic() - started) * 1000
                record["model"] = adapter.result_metadata() if adapter else {"usage_status": "not_sent"}
                record["reservations"] = [x.model_dump(mode="json") for x in adapter.reservations] if adapter else []
                (raw_dir / (record["id"] + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                persist()
            if packet["stop_reason"]:
                break
        packet["execution_status"] = "stopped" if packet["stop_reason"] else "completed"
    except Exception:
        packet.update(execution_status="stopped", stop_reason="EVALUATION_SETUP_FAILURE")
        persist()
        raise
    finally:
        packet["not_dispatched"] = [s["id"] for s in plan["samples"] if s["id"] not in {r["id"] for r in packet["rows"]}]
        packet["cleanup"] = {"socket_server_started": False, "background_jobs": 0,
            "ledger_connections": "BudgetLedger closes each operation", "retry_resume_enabled": False}
        persist()
    return packet


async def prepare_live_samples(output, configuration, *, ledger_path=None):
    """Actual retrieval/inventory preparation, no credentials or ledger access."""
    from agent.providers import ProviderClient
    from decimal import Decimal, ROUND_CEILING
    output = Path(output).resolve()
    scope = rag.ROOT / "Work_tree/artifacts/contest-final-acceptance"
    if not output.is_relative_to(scope.resolve()) or output.parent == scope.resolve() or output.exists():
        raise ValueError("Choose a new contest-final-acceptance run output")
    scratch = output.parent / "resources"
    if scratch.exists():
        raise ValueError("Prepared resources already exist")
    selected_ledger, ledger_identity = existing_ledger_binding(ledger_path)
    config = read(configuration)
    raw_inputs = rag.read_frozen(rag.INPUT_PATH, "inputs")
    q = next(q for q in raw_inputs["rag_comparison"]["questions"] if q["id"] == "R01-s1a-procedure")
    world = rag.initial_world(q["seed"], q["fixture"])
    samples = []
    with patch.object(rag, "ARTIFACT_ROOT", scratch):
        for variant, method in (("current", "keyword_rag"), ("current", "small_whole_document"), ("natural_conflict", "keyword_rag")):
            directory = scratch / (variant + "-" + method)
            async with rag.prepared_runtime(directory, "sim0", world, q["role"]) as (runtime, session):
                derived_manifest_hash = None
                if variant == "natural_conflict":
                    manifest = conflict_source(directory / "synthetic-source", neutral_metadata=True)
                    derived_manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
                    runtime.knowledge = Knowledge(runtime.store, directory / "controlled-index", clock=lambda: rag.NOW, source_root=manifest.parent)
                    runtime.knowledge.activate(manifest)
                question = rag.Question(q["query"], q.get("topic"))
                actual = await rag.execute_method(runtime, session, question, method)
                state = rag.snapshot(runtime, session, question)
                found = actual["raw_context"]
                # Whole inventory uses an explicitly declared experimental
                # transport, not a fabricated server search receipt.
                transport = deepcopy(found)
                if method == "small_whole_document":
                    transport["status"] = "matched" if found["status"] == "context_ready" else found["status"]
                model_input = {"request": {"run_id": world["run_id"], "goal": "regulation", "query": q["query"]},
                    "allowed_tools": ["search_operating_knowledge"],
                    "tool_results": [{"call_id": "prepared-read", "name": "search_operating_knowledge", "result": transport}]}
                # This is a fixed snapshot reading experiment. No expected,
                # private simulator state or arbitrary prompts enter the input.
                for provider in ("openai", "gemini"):
                    settings = config["providers"][provider]
                    client = ProviderClient(provider, settings["pricing"]["model"], 1024,
                                            settings["timeout_seconds"], credential=lambda: "")
                    bound = client.input_token_bound(model_input)
                    rate = settings["pricing"]
                    quote = ((Decimal(20000) * Decimal(rate["input_krw_per_million"]) +
                              Decimal(1024) * Decimal(rate["output_krw_per_million"])) / Decimal(1000000))
                    samples.append({"id": provider + "-" + variant + "-" + method, "provider": provider,
                        "model_ref": settings["pricing"]["model"], "method": method, "variant": variant,
                        "question": q["query"], "role": q["role"], "seed": q["seed"], "tick": 0,
                        "condition": state["condition"], "condition_fingerprint": state["fingerprint"],
                        "raw_context": found, "references": transport.get("references", []),
                        "model_input": model_input, "input_token_bound": bound, "max_input_tokens": 20000,
                        "max_output_tokens": 1024, "quote_upper_krw": int(quote.to_integral_value(rounding=ROUND_CEILING)),
                        "dispatch_preflight": "eligible" if bound <= 20000 else "blocked_input_bound",
                        "whole_document_transport": "authorized inventory test wrapper" if method == "small_whole_document" else None,
                        "derived_manifest_hash": derived_manifest_hash, "provider_calls": 0})
            for sample in samples:
                if sample["variant"] == variant and sample["method"] == method:
                    sample["cleanup"] = {"scratch_removed": not directory.exists(), "query_active": len(runtime.queries.active),
                        "queries_closed": runtime.queries.closed, "reader_alive": runtime.knowledge._reader is not None and runtime.knowledge._reader.is_alive(),
                        "store_lock_closed": runtime.store.lock_file.closed}
    for provider in ("openai", "gemini"):
        pair = [s for s in samples if s["provider"] == provider and s["variant"] == "current"]
        if len({s["condition_fingerprint"] for s in pair}) != 1:
            raise ValueError("Method conditions differ")
    bound = sum(s["quote_upper_krw"] for s in samples)
    if len(samples) != 6 or bound > 200:
        raise ValueError("Prepared plan exceeds six calls or 200 KRW estimate")
    packet = {"version": "contest-live-rag-preparation-v2", "samples": samples, "provider_calls": 0,
        "ledger_path": str(selected_ledger), "ledger_file_identity": ledger_identity,
        "ledger_contract": "Existing selected file and identity must match at dispatch; no replacement/new ledger",
        "configuration": config, "configuration_sha256": hashlib.sha256(Path(configuration).read_bytes()).hexdigest(),
        "source_provenance": rag.source_provenance() if hasattr(rag, "source_provenance") else None,
        "dispatch_source": dispatch_provenance(),
        "max_calls": 6, "batch_estimate_upper_krw": bound, "max_input_tokens": 20000, "max_output_tokens": 1024,
        "whole_acceptance_transfer": False, "execution_status": "prepared_only",
        "scope": "fixed actual retrieval/authorized-inventory snapshot reading experiment, not whole Agent query workflow or final dev run"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2)
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("collect")
    get.add_argument("--output", type=Path, required=True, help="New run subdirectory under local-rag-completion")
    get.add_argument("--conditions", type=Path, required=True, help="Nonempty pre-execution conditions document")
    get.add_argument("--recovered-live", type=Path)
    get.add_argument("--retrieval-history", type=Path)
    get.add_argument("--business-history", type=Path)
    prepare = sub.add_parser("prepare-live")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--configuration", type=Path, default=rag.ROOT / "data/samples/live-read-defaults.json")
    prepare.add_argument("--ledger-path", type=Path, help="Existing shared ledger; defaults to this checkout data/local/model-budget.sqlite3")
    live = sub.add_parser("collect-live")
    live.add_argument("--prepared", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--configuration", type=Path, default=rag.ROOT / "data/samples/live-read-defaults.json")
    live.add_argument("--target-commit", required=True)
    live.add_argument("--prepared-sha256", required=True)
    live.add_argument("--ledger-path", type=Path, help="Must match the prepared existing shared ledger path and identity")
    score = sub.add_parser("score")
    score.add_argument("--packet", type=Path, default=OUTPUT_ROOT / "evidence.json")
    score.add_argument("--review", type=Path, default=OUTPUT_ROOT / "independent-review.json")
    score.add_argument("--output", type=Path, default=OUTPUT_ROOT / "semantic-summary.json")
    args = parser.parse_args(argv)
    if args.command == "prepare-live":
        packet = asyncio.run(prepare_live_samples(args.output, args.configuration, ledger_path=args.ledger_path))
        print(json.dumps({"provider_calls": 0, "samples": len(packet["samples"]), "estimate_upper_krw": packet["batch_estimate_upper_krw"]}))
    elif args.command == "collect-live":
        packet = asyncio.run(collect_live_samples(args.prepared, args.output, configuration=args.configuration,
            target_commit=args.target_commit, packet_sha256=args.prepared_sha256, ledger_path=args.ledger_path))
        print(json.dumps({"status": packet["execution_status"], "provider_calls": packet["provider_calls"], "stop_reason": packet["stop_reason"]}))
        return 0 if packet["execution_status"] == "completed" else 2
    elif args.command == "collect":
        packet = asyncio.run(collect(args.output, conditions=args.conditions, recovered_live=args.recovered_live,
                                    retrieval_history=args.retrieval_history, business_history=args.business_history))
        print(json.dumps({"rows": len(packet["rows"]), "recovery": len(packet["recovery"]), "provider_calls": 0}))
    else:
        result = validate_review(read(bounded(args.packet)), read(bounded(args.review)))
        save_new(args.output, result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
