"""Local retrieval/reference comparison and opt-in supplemental business runs.

Whole documents and actual Runtime keyword retrieval share the same experimental
reader. Expected answers are loaded only by the evaluator, after execution.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from agent.tools import validate_session
from backend.auth import ApiError, Session
from agent.autonomous import MockOperationsAdapter
from contracts.autonomous import AutonomousControl, AutonomousDecision
from backend.knowledge import DEFAULT_MANIFEST, encoded, sha, terms
from backend.runtime import Runtime
from contracts.knowledge import KnowledgeManifest, Manual
from simulator.world import FACILITY, advance, initial_world, public_state

ARTIFACT_ROOT = ROOT / "artifacts/acceptance/rag"
INPUT_PATH = ROOT / "tests/scenarios/acceptance-inputs.json"
EXPECTED_PATH = ROOT / "tests/expected/acceptance-cases.json"
LEGACY_EXPECTED = ROOT / "tests/expected/rag/knowledge-cases.json"
NOW = "2026-10-02T00:00:00.000000Z"
READER_VERSION = "deterministic-group-reader-v1"
# Fixed before outcomes: SIM-0 source JSON is 4,272 characters in total.
# This cap counts complete serialized reference context, including metadata.
CONTEXT_CAP = 32000
READER_GROUP_CAP = 4
READER_CHAR_CAP = 6000
METHODS = ("keyword_rag", "small_whole_document")
NOT_RUN = {
    "manual": "No predeclared manual input trace or business-action evaluator",
    "fixed_rule": "No independent business rule/action evaluation contract",
    "mock_agent": "Existing mock omits topic and has no whole-document injection path",
}
# A supplemental, predeclared execution experiment. These inputs and manual
# operations are not the frozen R08 question slots and never change their score.
BUSINESS_METHODS = ("prerecorded_manual", "fixed_rule", "mock_operations_agent")
BUSINESS_SPEC = "local-business-comparison-v1"
BUSINESS_CASES = tuple(
    {"id": f"{scenario}-{stage}", "scenario": scenario, "fixture": fixture,
     "seed": 94001 + n, "ticks": ticks, "manual_action": action, "fault": None}
    for n, (scenario, fixture) in enumerate((
        ("s1a", "s1a-foundation-v1"), ("s1b", "s1b-blocked-v1"),
        ("s1c", "s1c-overlap-v1")))
    for stage, ticks, action in (("observed", 60, "notify"), ("early", 0, "hold"))
) + (
    {"id": "s1a-document-withdrawal", "scenario": "s1a", "fixture": "s1a-foundation-v1",
     "seed": 94004, "ticks": 60, "manual_action": "notify", "fault": "document"},
    {"id": "s1a-session-expiry", "scenario": "s1a", "fixture": "s1a-foundation-v1",
     "seed": 94005, "ticks": 60, "manual_action": "notify", "fault": "session"},
    {"id": "s1a-same-key", "scenario": "s1a", "fixture": "s1a-foundation-v1",
     "seed": 94006, "ticks": 60, "manual_action": "notify", "fault": "same_key"},
)


class BusinessClock(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)


def fixed_business_world(world):
    """One paused capture at the declared wall clock; sim timestamps stay intact."""
    world = deepcopy(world)
    def capture_time(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("observed_at", "received_at"):
                    value[key] = NOW
                else:
                    capture_time(item)
        elif isinstance(value, list):
            for item in value:
                capture_time(item)
    capture_time(world["observation"])
    capture_time(world["observation_history"])
    return world
FROZEN = {
    "inputs": "d9992061c08952d3d6cce3f1dbc1cfbbc705d0f102705b6b61bceae15e5f1527",
    "expected": "bc3daea810ff386c7053036dbec8f66b94304fcf33805f18da5bba293e0f4272",
    "legacy_expected": "2aed53684523c59b54d9dfb69e89eb5dd46067af0f7f02809558e77b0b723219",
    "legacy_manifest": "3e80c3f12024f4d44b2255845b8f5199ed9e1dd247bae277f168a43e31e6cf20",
    "sim0_manifest": "2dd634aa4d0891c01d7b4d2bd8b90a471f068f8ee65e3a86a4d4d19f87c414e5",
}
# Legacy public queries are copied from the adopted compatibility profile.
# Do not give the legacy expected file to any execution/reader function.
LEGACY_QUERIES = (
    ("통로 차단 이동 요청과 미응답", "parking_order"),
    ("길막 이동", "parking_order"),
    ("영업 종료 안내방송", "announcement"),
    ("차주 수락 응답 후속 확인", "user_guidance"),
    ("우주선 연료세금", None),
)


@dataclass(frozen=True)
class Question:
    query: str
    topic: str | None


def read_frozen(path, key):
    raw = path.read_bytes()
    # Manifest pins identify Git LF bytes across Windows checkouts. Normalize
    # only CRLF in these two pins; execution/index/provenance still use raw bytes.
    pinned = raw.replace(b"\r\n", b"\n") if key in ("legacy_manifest", "sim0_manifest") else raw
    if hashlib.sha256(pinned).hexdigest() != FROZEN[key]:
        raise ValueError(f"Frozen {key} changed; do not silently recalibrate")
    return json.loads(raw)


def manifest_for(corpus):
    if corpus not in ("sim0", "legacy"):
        raise ValueError("Unknown corpus")
    return DEFAULT_MANIFEST.with_name("manifest-sim0.json") if corpus == "sim0" else DEFAULT_MANIFEST


def preflight_manifest(corpus):
    path = manifest_for(corpus)
    raw = read_frozen(path, corpus + "_manifest")
    manifest = KnowledgeManifest.model_validate(raw)
    instant = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    for item in (manifest.policy, *manifest.documents):
        start = datetime.fromisoformat(item.effective_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(item.retired_at.replace("Z", "+00:00")) if item.retired_at else None
        if not (start <= instant and (end is None or instant < end)):
            raise ValueError("Fixed UTC outside manifest interval")
    sizes = []
    for meta in manifest.documents:
        source = (path.parent / meta.file).resolve(strict=True)
        if source.parent != path.parent.resolve():
            raise ValueError("Manifest source escaped approved directory")
        content = source.read_bytes()
        if sha(content) != meta.content_digest:
            raise ValueError("Frozen manual digest mismatch")
        Manual.model_validate_json(content)
        sizes.append({"document_id": meta.document_id, "chars": len(content.decode("utf-8")),
                      "bytes": len(content), "digest": meta.content_digest})
    return manifest, sizes


def load_execution_plan():
    declaration = read_frozen(INPUT_PATH, "inputs")["rag_comparison"]
    current = deepcopy(declaration["questions"])
    legacy = [{"id": f"legacy-{i + 1}", "query": query, "topic": topic,
               "role": "test_operator", "fixture": "s1a-foundation-v1", "seed": 1}
              for i, (query, topic) in enumerate(LEGACY_QUERIES)]
    return declaration, {"sim0": current, "legacy": legacy}


def safe_artifact_path(path):
    target = Path(path).resolve()
    root = ARTIFACT_ROOT.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("Scratch/output must be a child of this worktree's RAG artifacts")
    return target


async def close_runtime(runtime):
    """No background services are started; still close all owners explicitly."""
    try:
        await runtime.queries.close()
        await asyncio.wait_for(runtime.autonomous.close(), timeout=2)
    finally:
        reader = runtime.knowledge._reader
        if reader is not None:
            reader.join(timeout=2)
        runtime.store.close()
    if runtime.queries.active or (reader is not None and reader.is_alive()):
        raise RuntimeError("Cleanup incomplete: query task or index reader still active")


@asynccontextmanager
async def prepared_runtime(directory, corpus, world, role):
    directory = safe_artifact_path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    runtime = None
    try:
        runtime = Runtime(directory / "db/parking.sqlite3")
        runtime.knowledge.clock = lambda: NOW
        default = runtime.knowledge.current_policy(FACILITY)
        if (default.policy_version, default.knowledge_release_id) != (2, "knowledge-demo-v2"):
            raise ValueError("Runtime default policy/release mismatch")
        manifest, _ = preflight_manifest(corpus)
        if corpus == "sim0":
            runtime.knowledge.activate(manifest_for(corpus))
        policy = runtime.knowledge.current_policy(FACILITY)
        if policy.model_dump() != manifest.policy.model_dump():
            raise ValueError("Activated DB policy differs from selected manifest")
        release = runtime.store.db.execute(
            "SELECT * FROM knowledge_releases WHERE facility_id=? AND knowledge_release_id=?",
            (FACILITY, policy.knowledge_release_id)).fetchone()
        if release["manifest_digest"] != sha(manifest_for(corpus).read_bytes()):
            raise ValueError("DB release manifest mismatch")
        index_version, index = runtime.knowledge._index(FACILITY, policy.knowledge_release_id)
        members = {tuple(row) for row in runtime.store.db.execute(
            "SELECT document_id,document_version FROM knowledge_release_documents "
            "WHERE facility_id=? AND knowledge_release_id=?", (FACILITY, policy.knowledge_release_id))}
        expected_members = {(doc.document_id, doc.document_version) for doc in manifest.documents}
        if members != expected_members or index_version != manifest.index_version:
            raise ValueError("DB membership/index version mismatch")
        runtime.world = deepcopy(world)
        runtime.store.commit(runtime.world, Runtime.event(runtime.world))
        username = {"owner": "demo-owner", "test_operator": "demo-operator", "driver": "demo-driver"}[role]
        session = Session(username, role, "synthetic-comparison", time.monotonic() + 300)
        validate_session(runtime, session)
        yield runtime, session
    finally:
        # Retain failed-cleanup scratch for inspection instead of deleting live handles.
        if runtime is not None:
            await close_runtime(runtime)
        if directory.exists():
            shutil.rmtree(safe_artifact_path(directory))


def snapshot(runtime, session, question):
    """Same prefilter and verified complete document inventory for both methods."""
    validate_session(runtime, session)
    runtime.ensure_run(runtime.world["run_id"])
    k = runtime.knowledge
    policy = k.current_policy(FACILITY)
    role, scope = k.scope(session.username)
    version, index = k._index(FACILITY, policy.knowledge_release_id)
    rows = k._eligible(FACILITY, policy.knowledge_release_id, role, k.clock())
    if not rows:
        raise ValueError("No eligible document context; not a negative retrieval answer")
    refs = [k._reference(row) for row in rows]
    by_document = defaultdict(set)
    for row in rows:
        by_document[(row["document_id"], row["document_version"])].add(row["reference_id"])
    for key, ids in by_document.items():
        source = runtime.store.db.execute(
            "SELECT content FROM knowledge_documents WHERE facility_id=? AND document_id=? AND document_version=?",
            (FACILITY, *key)).fetchone()[0]
        if ids != {c.reference_id for c in Manual.model_validate_json(source).chunks}:
            raise ValueError("Incomplete whole-document reference set")
    if any(ref["reference_id"] not in index for ref in refs):
        raise ValueError("Index missing eligible reference")
    state = {"facility_id": FACILITY, "run_id": runtime.world["run_id"],
             "principal_role": role, "principal": session.username, "question": question.query,
             "topic": question.topic, "current_observation_version": runtime.world["state_version"],
             "observation_digest": sha(encoded(public_state(runtime.world))),
             "policy": policy.model_dump(), "effective_wall_time": k.clock(),
             "scope": scope, "registry_scope": runtime.store.registry.scope_stamp(session.username),
             "document_scope": refs, "conflicts": [r["reference_id"] for r in rows if r["reviewed_conflict"]],
             "index_version": version, "model_version": READER_VERSION}
    return {"fingerprint": sha(encoded(state)), "condition": state, "references": refs}


def whole_document_context(state):
    if state["condition"]["conflicts"]:
        return {"status": "conflict", "reason_code": "reviewed_conflict", "references": []}
    refs = deepcopy(state["references"])
    if len(encoded(refs)) > CONTEXT_CAP:
        return {"status": "unavailable", "reason_code": "context_cap_exceeded", "references": []}
    return {"status": "context_ready", "references": refs,
            "serialized_characters": len(encoded(refs))}


def deterministic_reader(question, context):
    """Experimental generic lexical selector; no case ID, role, seed or oracle."""
    if context["status"] not in ("matched", "no_match", "context_ready") or context.get("reason_code"):
        return {"status": "error", "references": [], "reason": context.get("reason_code", context["status"])}
    groups = defaultdict(list)
    for ref in context["references"]:
        groups[ref["procedure_group_id"]].append(ref)
    query_terms = set(terms(question.query))
    ranked = []
    for group, members in groups.items():
        if question.topic is not None and not any(r["topic"] == question.topic for r in members):
            continue
        score = sum(len(query_terms & set(terms(r["title"] + " " + r["section"] + " " + r["excerpt"])))
                    for r in members)
        if score:
            ranked.append((score, group, members))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    selected = []
    for _, _, members in ranked[:READER_GROUP_CAP]:
        if sum(len(r["excerpt"]) for r in selected + members) > READER_CHAR_CAP:
            return {"status": "error", "references": [], "reason": "incomplete_context"}
        selected.extend(sorted(members, key=lambda ref: ref["reference_id"]))
    return {"status": "matched" if selected else "no_match", "references": deepcopy(selected)}


def verify_selected(selected, inventory):
    allowed = {ref["reference_id"]: ref for ref in inventory}
    groups = {r["procedure_group_id"] for r in selected}
    ids = {r["reference_id"] for r in selected}
    complete_ids = {r["reference_id"] for r in inventory if r["procedure_group_id"] in groups}
    return (ids == complete_ids and len(ids) == len(selected)
            and all(allowed.get(ref["reference_id"]) == ref for ref in selected))


async def execute_method(runtime, session, question, method, before_return=None):
    """Returns sanitized actuals only; expected data never enters this path."""
    total_start = time.perf_counter()
    state = snapshot(runtime, session, question)
    task = runtime.read_task(session, runtime.world["run_id"])
    prefilter_ms = (time.perf_counter() - total_start) * 1000
    started = time.perf_counter()
    if method == "keyword_rag":
        raw = await runtime.read_tool(session, "search_operating_knowledge", {
            "facility_id": FACILITY, "run_id": task.run_id,
            "query": question.query, "topic": question.topic}, task)
    elif method == "small_whole_document":
        raw = whole_document_context(state)
    else:
        raise ValueError("Unknown executable method")
    context_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    selected = deterministic_reader(question, raw)
    reader_ms = (time.perf_counter() - started) * 1000
    if before_return is not None:
        before_return(runtime, session)
    publication_start = time.perf_counter()
    after = snapshot(runtime, session, question)
    if state["fingerprint"] != after["fingerprint"]:
        raise ValueError("Context changed before publication")
    if not verify_selected(raw["references"], after["references"]):
        raise ValueError("Raw reference provenance/group completeness failure")
    if not verify_selected(selected["references"], after["references"]):
        raise ValueError("Selected reference provenance/group completeness failure")
    return {"status": "executed" if selected["status"] != "error" else "error",
            "condition_fingerprint": state["fingerprint"], "condition": state["condition"],
            "raw_context": raw, "reader_selected": selected,
            "citation_structure": {"checked": len(selected["references"]), "valid": len(selected["references"]),
                                   "rate": 1.0 if selected["references"] else None},
            "calls": {"tool": task.tool_calls, "retrieval": task.retrieval_calls,
                      "mock_adapter_turn": 0, "provider": 0},
            "wall_latency_ms": {"context": prefilter_ms + context_ms,
                                "prefilter_snapshot": prefilter_ms,
                                "assembly_or_retrieval": context_ms, "reader": reader_ms,
                                "publication_recheck": (time.perf_counter() - publication_start) * 1000,
                                "total": (time.perf_counter() - total_start) * 1000}}


async def execute_plan(plan, scratch, repeat=1):
    if repeat not in (1, 3):
        raise ValueError("repeat must be 1 or 3; repeats are not new query slots")
    scratch = safe_artifact_path(scratch)
    scratch.mkdir(parents=True, exist_ok=False)
    results = []
    try:
        for corpus, slots in plan.items():
            preflight_manifest(corpus)
            for slot in slots:
                for attempt in range(1, repeat + 1):
                    world = initial_world(slot["seed"], slot["fixture"])
                    question = Question(slot["query"], slot["topic"])
                    pair = []
                    for method in METHODS:
                        row = {"corpus": corpus, "slot": slot["id"], "attempt": attempt,
                               "method": method, "fixture": slot["fixture"], "seed": slot["seed"],
                               "role": slot["role"], "question": slot["query"], "topic": slot["topic"]}
                        setup_start = time.perf_counter()
                        directory = scratch / uuid4().hex
                        try:
                            async with prepared_runtime(directory, corpus, world, slot["role"]) as (runtime, session):
                                setup_ms = (time.perf_counter() - setup_start) * 1000
                                row.update(await execute_method(runtime, session, question, method))
                                row["wall_latency_ms"]["setup_index_build"] = setup_ms
                        except Exception as error:
                            # Do not retain an otherwise successful result if cleanup/revalidation failed.
                            row = {key: row[key] for key in ("corpus", "slot", "attempt", "method", "fixture",
                                                           "seed", "role", "question", "topic")}
                            row.update(status="error", error_type=type(error).__name__,
                                       error_code=getattr(error, "code", "comparison_unavailable"))
                        pair.append(row)
                    fingerprints = [row.get("condition_fingerprint") for row in pair]
                    same = all(fingerprints) and len(set(fingerprints)) == 1
                    for row in pair:
                        row["same_condition_pair"] = bool(same)
                        if row["status"] == "executed" and not same:
                            row["status"] = "error"
                            row["error_code"] = "pair_condition_mismatch"
                        results.append(row)
    finally:
        # Each runtime cleans its own child. Nonempty scratch means cleanup failed.
        if scratch.exists() and not any(scratch.iterdir()):
            scratch.rmdir()
    return results


def load_expectations():
    """Evaluator-only I/O. Never called until all method execution has finished."""
    expected = read_frozen(EXPECTED_PATH, "expected")["rag_reference_expectations"]
    legacy = read_frozen(LEGACY_EXPECTED, "legacy_expected")["cases"]
    sim0 = expected["sim0_current"]["groups"]
    resolved = {}
    for key, value in sim0.items():
        resolved[key] = deepcopy(sim0[value["same_expected_as"]] if "same_expected_as" in value else value)
        resolved[key].setdefault("status", "matched")
    old = {}
    for i, (minimal, full) in enumerate(zip(legacy, expected["legacy_fixture"]["cases"], strict=True)):
        if minimal["query"] != full["query"] or minimal["expected_status"] != full["status"]:
            raise ValueError("Legacy expected sources disagree")
        old[f"legacy-{i + 1}"] = deepcopy(full)
    return {"sim0": resolved, "legacy": old}


def required_group_hit(refs, expected):
    required = set(expected["required_references"])
    found = {ref["reference_id"] for ref in refs
             if ref["procedure_group_id"] == expected.get("group")
             and ref["document_id"] == expected.get("document_id")
             and ref["document_version"] == expected.get("document_version")}
    return bool(required) and required <= found


def evaluate(results, plan, expectations, repeat=1):
    """Missing/error attempts never become negative no_match successes."""
    evaluated = deepcopy(results)
    for row in evaluated:
        expected = expectations[row["corpus"]][row["slot"]]
        row["expected"] = deepcopy(expected)
        if row["status"] != "executed":
            row["quality_match"] = False
            continue
        selected = row["reader_selected"]
        positive = expected["status"] != "no_match"
        row["context_required_group_hit"] = required_group_hit(row["raw_context"]["references"], expected) if positive else None
        row["reader_required_group_hit"] = required_group_hit(selected["references"], expected) if positive else None
        row["quality_match"] = (selected["status"] == "matched" and row["reader_required_group_hit"]) if positive else (
            selected["status"] == "no_match" and not selected["references"])
    summary = []
    for corpus, slots in plan.items():
        positive_ids = {slot["id"] for slot in slots if expectations[corpus][slot["id"]]["status"] != "no_match"}
        for method in (*METHODS, *NOT_RUN):
            rows = [r for r in evaluated if r["corpus"] == corpus and r["method"] == method]
            by_slot = defaultdict(list)
            for row in rows:
                by_slot[row["slot"]].append(row)
            def every(slot_id, key):
                values = by_slot.get(slot_id, [])
                return len(values) == repeat and all(row.get(key) is True for row in values)
            executed_slots = sum(len(by_slot.get(s["id"], [])) == repeat and all(r["status"] == "executed" for r in by_slot.get(s["id"], [])) for s in slots)
            matched = sum(every(s["id"], "quality_match") for s in slots)
            negatives = {s["id"] for s in slots} - positive_ids
            checked = sum(r.get("citation_structure", {}).get("checked", 0) for r in rows)
            summary.append({"corpus": corpus, "method": method,
                "status": "not_run" if method in NOT_RUN else "partial_reference_comparison",
                "not_run_reason": NOT_RUN.get(method), "registered_slots": len(slots),
                "unique_questions": len({(s["query"], s["topic"], s["role"]) for s in slots}),
                "selected_slots": len(slots) if method in METHODS else 0,
                "executed_slots": executed_slots, "matched_slots": matched,
                "mismatched_slots": sum(not every(s["id"], "quality_match") and len(by_slot.get(s["id"], [])) == repeat
                    and all(r["status"] == "executed" for r in by_slot.get(s["id"], [])) for s in slots),
                "error_attempts": sum(r["status"] == "error" for r in rows),
                "not_run_slots": len(slots) - len(by_slot),
                "not_run_attempts": len(slots) * repeat - len(rows),
                "incomplete_slots": sum(0 < len(by_slot.get(s["id"], [])) < repeat for s in slots),
                "attempts": len(rows), "repeat": repeat,
                "coverage": executed_slots / len(slots),
                "required_group_recall": {"positive_slots": len(positive_ids),
                    "context_hits": sum(every(i, "context_required_group_hit") for i in positive_ids),
                    "reader_hits": sum(every(i, "reader_required_group_hit") for i in positive_ids)},
                "negative_cases": {"slots": len(negatives), "correct": sum(every(i, "quality_match") for i in negatives)},
                "citation_support": {"structural_checked": checked, "structural_rate": 1.0 if checked else None,
                                     "semantic_support": None},
                "allowed_action_fit": None, "held_count": None, "unsupported_answer_count": None,
                "unanswered_count": sum(r.get("reader_selected", {}).get("status") == "no_match" for r in rows),
                "calls_unobserved_attempts": sum("calls" not in r for r in rows),
                "calls": {name: (sum(r.get("calls", {}).get(name, 0) for r in rows)
                                 if name == "provider" or all("calls" in r for r in rows) else None)
                          for name in ("tool", "retrieval", "mock_adapter_turn", "provider")},
                "wall_latency_ms": [r["wall_latency_ms"] for r in rows if "wall_latency_ms" in r],
                "estimated_cost_krw": 0 if method in METHODS else None})
    return {"summary": summary, "attempts": evaluated}


def fixed_business_rule(context):
    """Independent bounded rule specification; no case ID or answer table."""
    analysis = context.get("analysis") or {}
    ready = analysis.get("support_status") == "supported" and bool(context.get("target_ref"))
    if context["scenario"] == "s1a":
        ready = ready and any(o.get("stationary_candidate") for o in analysis.get("metrics", {}).get("objects", []))
    else:
        ready = ready and analysis.get("violation_candidate") is True
    return "notify" if ready and not context.get("incident") else "hold"


def business_input_condition(context, runtime, session):
    """Exclude generated search IDs, retain the actual public decision input."""
    condition = {key: deepcopy(context[key]) for key in (
        "scenario", "trigger", "run_id", "state_version", "observation", "analysis",
        "policy", "target_ref", "target_zone", "incident", "notification", "command")}
    knowledge = deepcopy(context["knowledge"])
    knowledge.pop("retrieval_id", None)
    condition.update(knowledge=knowledge, role=session.role,
                     principal=session.username, clock=NOW,
                     scope=runtime.store.registry.scope_stamp(session.username),
                     limits={"tool_calls": 16, "wall_seconds": 30, "provider_calls": 0})
    return condition


async def execute_business_case(case, method, directory, world, before_decision=None):
    """Actual AutonomousService admission, search, apply and execution rechecks.

    Only the decision provider is replaced for the two baselines. All methods
    obtain genuine server-created search evidence; none receives the evaluator.
    No WebInbox delivery, synthetic user reaction, server or provider is started.
    """
    if method not in BUSINESS_METHODS:
        raise ValueError("Unknown business method")
    started = time.perf_counter()
    capture = {"adapter_turns": 0}
    world = fixed_business_world(world)
    async with prepared_runtime(directory, "sim0", world, "test_operator") as (runtime, session):
        runtime.business.clock = lambda: NOW
        before_world = deepcopy(public_state(runtime.world))
        body = AutonomousControl(run_id=world["run_id"], action="process", mode="mock", scenario=case["scenario"])
        key = "comparison-" + case["id"]

        class LocalAdapter:
            async def decide(self, context):
                capture["adapter_turns"] += 1
                condition = business_input_condition(context, runtime, session)
                capture["condition"] = condition
                capture["condition_fingerprint"] = sha(encoded(condition))
                capture["retrieval_id"] = context["knowledge"]["retrieval_id"]
                capture["search_status"] = context["knowledge"]["status"]
                capture["reference_ids"] = [r["reference_id"] for r in context["knowledge"]["references"]]
                if method == "mock_operations_agent":
                    decision = await MockOperationsAdapter().decide(context)
                else:
                    action = case["manual_action"] if method == "prerecorded_manual" else fixed_business_rule(context)
                    decision = AutonomousDecision(scenario=context["scenario"], action=action,
                        target_ref=context["target_ref"] if action == "notify" else None,
                        reason_code="LOCAL_COMPARISON", rationale="사전 고정한 무과금 비교 조작").model_dump()
                capture["decision"] = deepcopy(decision)
                if case["fault"] == "document":
                    runtime.knowledge.document_access(FACILITY, "manual-parking-order", "v2", status="withdrawn")
                elif case["fault"] == "session":
                    session.expires = 0
                if before_decision is not None:
                    await before_decision(runtime, session, context)
                return decision

            def result_metadata(self):
                return {"mode": "mock", "provider": None, "model_ref": method,
                        "model_call_count": 0, "cost_estimated_krw": 0}

        result, error, duplicate = None, None, None
        # Local, sequential experiment only. Restore the original product
        # factory even when the admitted operation fails or is cancelled.
        with patch("backend.autonomous.MockOperationsAdapter", LocalAdapter), \
             patch("simulator.spatial.datetime", BusinessClock), \
             patch("backend.operating_analysis.datetime", BusinessClock):
            try:
                result = await runtime.autonomous.control(session, body, key, lambda: validate_session(runtime, session))
                if case["fault"] == "same_key":
                    duplicate = await runtime.autonomous.control(session, body, key, lambda: validate_session(runtime, session))
            except ApiError as exc:
                error = {"status": exc.status, "code": exc.code}
        counts = {table: runtime.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("incidents", "plans", "notifications", "followups", "executions", "autonomous_jobs", "knowledge_retrievals", "delivery_attempts")}
        incidents = [dict(row) for row in runtime.store.db.execute(
            "SELECT run_id,primary_object_id,status FROM incidents ORDER BY rowid")]
        jobs = [dict(row) for row in runtime.store.db.execute("SELECT status FROM autonomous_jobs ORDER BY rowid")]
        notifications = [dict(row) for row in runtime.store.db.execute(
            "SELECT delivery_status,purpose FROM notifications ORDER BY rowid")]
        retrieval = runtime.store.db.execute("SELECT retrieval_id FROM knowledge_retrievals WHERE retrieval_id=?",
                                            (capture.get("retrieval_id", ""),)).fetchone()
        row = {"case": case["id"], "method": method, "status": "executed",
               "result": result, "error": error, "counts": counts, "incidents": incidents,
               "jobs": jobs, "notifications": notifications, **capture,
               "genuine_search_record": bool(retrieval),
               "duplicate_same_job": bool(result and duplicate and result["job_id"] == duplicate["job_id"]),
               "public_world_unchanged": before_world == public_state(runtime.world),
               "provider_calls": 0, "delivery_attempts": counts["delivery_attempts"],
               "wall_latency_ms": (time.perf_counter() - started) * 1000,
               "sim_time_ms": world["sim_time_ms"]}
    row["cleanup"] = {"scratch_removed": not directory.exists()}
    return row


def business_rubric():
    """Independent outcomes; only called after execution, never a model input."""
    return {case["id"]: {"action": "hold" if case["ticks"] == 0 else "notify",
            "status": "held" if case["ticks"] == 0 or case["fault"] in ("document", "session") else "accepted",
            "error_code": {"document": "JOB_CONTEXT_CHANGED", "session": "UNAUTHENTICATED"}.get(case["fault"]),
            "notifications": 0 if case["ticks"] == 0 or case["fault"] in ("document", "session") else 1,
            "requires_duplicate": case["fault"] == "same_key"} for case in BUSINESS_CASES}


def evaluate_business(rows, rubric):
    evaluated = deepcopy(rows)
    fingerprints = defaultdict(set)
    for row in evaluated:
        if "condition_fingerprint" in row:
            fingerprints[row["case"]].add(row["condition_fingerprint"])
    for row in evaluated:
        wanted = rubric[row["case"]]
        errors = []
        if row["status"] != "executed":
            errors.append("not_executed")
        else:
            action = row.get("decision", {}).get("action")
            status = (row.get("result") or {}).get("status", "held" if row.get("error") else None)
            actual_error = (row.get("error") or {}).get("code")
            checks = {
                "same_public_input": len(fingerprints[row["case"]]) == 1,
                "decision_action": action == wanted["action"], "execution_status": status == wanted["status"],
                "error_code": actual_error == wanted["error_code"],
                "notification_count": row["counts"]["notifications"] == wanted["notifications"],
                "actual_search": row.get("genuine_search_record") is True,
                "no_motion_claim": row.get("public_world_unchanged") is True,
                "no_early_closure": all(i["status"] not in ("resolved", "closed_no_issue", "closed_false_positive") for i in row["incidents"]),
                "no_provider": row["provider_calls"] == 0,
                "no_dispatch": row["delivery_attempts"] == 0,
                "dispatch_count_consistent": row["delivery_attempts"] == row["counts"]["delivery_attempts"],
                "persisted_job": row["counts"]["autonomous_jobs"] == len(row["jobs"]) == 1 and
                    row["jobs"][0]["status"] == ("completed" if wanted["status"] == "accepted" else "held"),
                "resources_closed": row.get("cleanup", {}).get("scratch_removed") is True,
                "same_key_reuses_job": not wanted["requires_duplicate"] or row["duplicate_same_job"],
            }
            if wanted["notifications"]:
                checks.update(active_incident=len(row["incidents"]) == 1 and row["incidents"][0]["status"] not in
                              ("resolved", "closed_no_issue", "closed_false_positive"),
                              one_followup=row["counts"]["followups"] == 1,
                              one_job=row["counts"]["autonomous_jobs"] == 1)
            else:
                checks["no_business_side_effect"] = all(row["counts"][name] == 0 for name in
                    ("incidents", "plans", "notifications", "followups", "executions"))
            errors.extend(key for key, ok in checks.items() if not ok)
            row["checks"] = checks
        row["mismatches"], row["matched"] = errors, not errors
    summary = [{"method": method, "registered_cases": len(rubric),
                "attempts": len([r for r in evaluated if r["method"] == method]),
                "matched": sum(r["matched"] for r in evaluated if r["method"] == method),
                "failed": sum(not r["matched"] for r in evaluated if r["method"] == method),
                "not_run": len(rubric) - len([r for r in evaluated if r["method"] == method]),
                "wall_latency_ms": [r["wall_latency_ms"] for r in evaluated if r["method"] == method and "wall_latency_ms" in r]}
               for method in BUSINESS_METHODS]
    return {"summary": summary, "attempts": evaluated}


async def run_business_comparison(output, repeat=1):
    output = safe_artifact_path(output)
    if output.exists():
        raise FileExistsError("Report already exists; choose a new output name")
    rows = []
    scratch = ARTIFACT_ROOT / "tmp" / uuid4().hex
    for case in BUSINESS_CASES:
        world = initial_world(case["seed"], case["fixture"])
        for _ in range(case["ticks"]):
            advance(world)
        for attempt in range(repeat):
            for method in BUSINESS_METHODS:
                path = scratch / case["id"] / str(attempt) / method
                try:
                    row = await execute_business_case(case, method, path, world)
                except Exception as exc:
                    row = {"case": case["id"], "method": method, "status": "error",
                           "reason": type(exc).__name__, "provider_calls": 0}
                row["attempt"] = attempt + 1
                rows.append(row)
    # Empty parent directories carry no open DB or reader handles.
    if scratch.exists() and not any(p.is_file() for p in scratch.rglob("*")):
        shutil.rmtree(safe_artifact_path(scratch))
    report = {"version": BUSINESS_SPEC, "source_provenance": source_provenance(),
              "contract": {"cases": deepcopy(BUSINESS_CASES), "clock": NOW, "repeat": repeat,
                           "method_order": list(BUSINESS_METHODS), "provider_budget": 0,
                           "execution_hook": "actual AutonomousService.control/process; genuine server keyword search",
                           "fixed_rule": "supported current candidate, no active incident -> notify; otherwise hold",
                           "manual": "prerecorded allowed operation trace; no measured human participant",
                           "whole_document_business": "not_run: no adopted whole-document execution evidence contract",
                           "cache": "fresh DB/index per method/case/attempt"},
              "overall_acceptance": {"passed": 0, "denominator": 194},
              "semantic_support": None, "provider_calls": 0, "server_starts": 0,
              "limits": ["Supplemental execution experiment, not frozen R08 or full T/V/R acceptance",
                         "No human speed, natural-language semantic quality, paid-model quality or superiority claim",
                         "Queued notification is not dispatch, browser receipt, movement or resolution",
                         "Delay/loss/restart/S2/S3 comparative experiments remain separate"],
              "cleanup": {"scratch_removed": not scratch.exists(), "scratch": str(scratch)}}
    # Each attempt is evaluated separately; repeats are not new cases.
    report["runs"] = [evaluate_business([r for r in rows if r["attempt"] == n], business_rubric())
                      for n in range(1, repeat + 1)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def source_provenance():
    """Bounded allowlist: no worktree-wide file scan or credential inspection."""
    names = ["scripts/compare_acceptance_rag.py", "tests/test_acceptance_rag_comparison.py",
             "code/backend/runtime.py", "code/backend/knowledge.py", "code/backend/registry.py",
             "code/backend/agent_queries.py", "code/backend/storage.py", "code/agent/tools.py",
             "code/contracts/knowledge.py", "code/simulator/world.py", "requirements.lock.txt",
             "code/backend/autonomous.py", "code/backend/business.py", "code/agent/autonomous.py",
             "code/contracts/autonomous.py", "code/backend/operating_analysis.py",
             "code/simulator/environment.py", "code/simulator/spatial.py"]
    for corpus in ("legacy", "sim0"):
        manifest, _ = preflight_manifest(corpus)
        path = manifest_for(corpus)
        names.append(path.relative_to(ROOT).as_posix())
        names.extend((path.parent / doc.file).relative_to(ROOT).as_posix()
                     for doc in manifest.documents)
    names = sorted(set(names))
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True, timeout=5).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal", "--", *names],
                            cwd=ROOT, check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
    return {"git_commit": head, "scoped_dirty": bool(status), "scoped_status": status,
            "scope": "Only listed source/manifest/manual/lock paths; other changes not inspected",
            "sha256": hashes, "python": sys.version.split()[0]}

async def run_comparison(output, repeat=1):
    output = safe_artifact_path(output)
    if output.exists():
        raise FileExistsError("Report already exists; choose a new output name")
    declaration, plan = load_execution_plan()
    sizes = {corpus: preflight_manifest(corpus)[1] for corpus in plan}
    scratch = ARTIFACT_ROOT / "tmp" / uuid4().hex
    actual = await execute_plan(plan, scratch, repeat)
    report = {"version": "acceptance-rag-comparison-v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "declared_metadata": declaration, "frozen_sha256": FROZEN, "source_provenance": source_provenance(),
              "frozen_sha256_policy": {
                  "legacy_manifest_and_sim0_manifest": "CRLF-to-LF bytes only",
                  "input_and_expected_pins": "raw bytes, strict",
                  "manual_content_digests": "raw bytes, strict",
                  "runtime_manifest_and_index_digests": "actual raw bytes",
                  "source_provenance_sha256": "actual raw bytes"},
              "contract": {"clock": NOW, "reader": READER_VERSION, "context_cap_chars": CONTEXT_CAP,
                           "reader_group_cap": READER_GROUP_CAP, "reader_excerpt_cap_chars": READER_CHAR_CAP,
                           "source_sizes": sizes, "repeat": repeat, "method_order": list(METHODS),
                           "cache": "fresh DB/index per method/slot/attempt; preflight warms both equally",
                           "legacy_profile": "compatibility only: test_operator/foundation/seed1",
                           "scope": "retrieval and structural references only; no LLM/business winner"},
              "overall_acceptance": {"passed": 0, "denominator": 194},
              "provider_calls": 0, "server_starts": 0, "business_execution": "not_run",
              "limits": ["No natural-language semantic citation support measured",
                         "No allowed business action or unsupported-answer quality measured",
                         "No actual LLM/model comparison or comparative winner",
                         "Structural citation checks do not authorize product execution",
                         "Current 9 slots include 2 recurring questions; only 7 unique",
                         "Legacy is a compatibility profile, not current final evaluation"],
              "cleanup": {"scratch_removed": not scratch.exists(), "scratch": str(scratch)}}
    report.update(evaluate(actual, plan, load_expectations(), repeat))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


S2S3_ROOT = ROOT / "artifacts/acceptance/s2s3"
S2S3_SPEC = "local-s2-s3-method-calibration-v1"
# Predeclared supplemental inputs. They never replace frozen final slots.
S2S3_CASES = tuple(
    {"id": f"s2-{fault or 'normal'}", "scenario": "s2", "fixture": "s2-crossing-v1",
     "seed": 6, "initial_tick_cap": 45, "fault": fault}
    for fault in (None, "search_unavailable", "decision_unavailable", "audio_failed", "visual_failed")
) + tuple(
    {"id": f"s3-{fault or 'normal'}", "scenario": "s3", "fixture": "s3-closing-v1",
     "seed": 3, "initial_ticks": 60, "fault": fault}
    for fault in (None, "broadcast_failed", "same_key", "restart")
)
S2S3_CONTRACT = {
    "version": S2S3_SPEC, "clock_epoch": NOW, "utc_per_tick_ms": 100,
    "clock_mode": "synthetic UTC progresses with declared ticks; real monotonic deadlines/latency separate",
    "repeat": 1, "methods": list(BUSINESS_METHODS),
    "cases": deepcopy(S2S3_CASES), "roles": {"s2": "test_operator", "s3": "owner"},
    "policy_release": "knowledge-sim0-v3", "provider_budget": 0,
    "s2_capture": "first supported public risk candidate within 45 ticks; then 1 channel-feedback tick",
    "s2_faults": "channel fault before tick1; search index read/decision failure only at first process; recovery new key at unchanged sim time",
    "s2_manual_trace": ["report", "recheck"],
    "s3_command": "문 닫아", "s3_clarification": "closing",
    "s3_manual_trace": ["clarify", "clarify", "announce", "announce", "restrict_entry", "hold"],
    "s3_failed_broadcast_trace": ["clarify", "clarify", "announce", "hold"],
    "s3_broadcast_feedback_ticks": 3, "s3_portal_request_ticks": 1, "s3_portal_followup_ticks": 70,
    "s3_faults": "playback fault before first announcement; duplicate first announcement before tick; restart after first acceptance before feedback",
    "restart": "close handles, reopen same DB, reauthenticate, observe recovery guard, explicit operator step",
    "same_input": "hash actual contexts/devices, normalize generated correlation UUIDs only; retain raw inputs",
    "unknown": "not_supported: public boolean fault injection cannot prescribe an unknown feedback",
    "final_acceptance": "calibration only; frozen final inputs/expected/194 unchanged",
}


def s2s3_path(path):
    target, root = Path(path).resolve(), S2S3_ROOT.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("S2/S3 artifacts must be children of this worktree's s2s3 directory")
    return target


def comparison_condition(context):
    """Retain semantic values; fresh runs use different generated correlation IDs."""
    return re.sub(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", "<generated>", encoded(context))


def s2s3_rule(context):
    """Independent rule over public analysis, confirmation and device feedback.

    Never copies the product's normalized action or uses fixture/fault/oracle.
    """
    if context["scenario"] == "s2":
        if context.get("incident"):
            return "recheck"
        analysis = context.get("analysis") or {}
        return "report" if analysis.get("support_status") == "supported" and analysis.get("violation_candidate") and context.get("target_ref") else "hold"
    command = context.get("command") or {}
    goal = command.get("normalized_goal") or {}
    if goal.get("confirmed") is not True:
        return "clarify"
    devices = context["public_devices"]
    broadcasts = [b for b in devices["broadcasts"] if b["message_id"] == "closing_notice"]
    if any(b["simulated_playback"] in ("pending", "failed", "unknown", "cancelled") for b in broadcasts):
        return "hold"
    played = {b["zone_id"] for b in broadcasts if b["receipt"] == "accepted" and b["simulated_playback"] == "played"}
    if not {"announcement-a", "announcement-b"} <= played:
        return "announce"
    entry = next(g for g in devices["gates"] if g["direction"] == "entry")
    return "hold" if entry["entry_policy"] == "deny" else "restrict_entry"


class S2S3Rig:
    """Actual ASGI routes, explicit ticks, no background loop or external model."""
    def __init__(self, directory, method, case):
        self.directory, self.method, self.case = s2s3_path(directory), method, case
        self.runtime = self.client = None
        self.trace, self.decisions = [], []
        self.sequence = 0
        self.clock_ms = 0
        self.decision_fault = False
        self.search_fault = False
        self.decision_offset = 0
        self.manual_trace = S2S3_CONTRACT["s2_manual_trace"] if case["scenario"] == "s2" else (
            S2S3_CONTRACT["s3_failed_broadcast_trace"] if case["fault"] == "broadcast_failed"
            else S2S3_CONTRACT["s3_manual_trace"])

    def wire_runtime(self, runtime):
        self.runtime = runtime
        runtime.knowledge.clock = runtime.business.clock = self.utc
        if runtime.knowledge.current_policy(FACILITY).knowledge_release_id != "knowledge-sim0-v3":
            runtime.knowledge.activate(manifest_for("sim0"))
        self.app.state.runtime = runtime
        from backend.auth import Auth
        self.app.state.auth = Auth(runtime.store)
        self.operator_token = None

    def utc(self):
        epoch = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        return (epoch + timedelta(milliseconds=self.clock_ms)).isoformat(timespec="microseconds").replace("+00:00", "Z")

    async def open(self):
        from backend.app import Settings, create_app
        from httpx import ASGITransport, AsyncClient
        self.directory.mkdir(parents=True, exist_ok=False)
        self.database = self.directory / "db/parking.sqlite3"
        self.app = create_app(Settings(database=self.database, test_control=True, background_ticks=False,
                                       origins=("http://testserver",), live_configuration=None))
        self.wire_runtime(Runtime(self.database))
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://testserver")
        await self.login()

    async def login(self):
        # The same operator grant is used for scenario setup and owner-scoped
        # work uses demo-owner. Both are published synthetic demo accounts.
        username = "demo-operator" if self.case["scenario"] == "s2" else "demo-owner"
        await self.request("POST", "/api/v1/auth/session", {"username": username, "password": "parking-demo-only"}, stage="login", record=False)
        self.session = self.app.state.auth.lookup(self.client.cookies.get("parking_session"))
        if self.session is None:
            raise RuntimeError("Synthetic login did not establish an authenticated session")

    async def request(self, verb, url, body=None, *, stage, key=None, operator=False, record=True):
        self.sequence += 1
        headers = {"origin": "http://testserver", "idempotency-key": key or f"s2s3-{self.sequence}"}
        if hasattr(self, "session"):
            headers["x-csrf-token"] = self.session.csrf
        if operator:
            if self.operator_token is None:
                self.operator_token, self.operator_session = self.app.state.auth.login("demo-operator", "parking-demo-only", "calibration-control")
            headers["cookie"] = "parking_session=" + self.operator_token
            session = self.operator_session
            headers["x-csrf-token"] = session.csrf
        response = await self.client.request(verb, url, json=body, headers=headers)
        data = response.json()
        if record:
            self.trace.append({"stage": stage, "http_status": response.status_code, "result": data,
                               "sim_time_ms": self.runtime.world["sim_time_ms"] if self.runtime.world else None})
        if response.status_code >= 400 and stage not in ("process", "pre_resume_guard"):
            raise ApiError(response.status_code, data.get("error", {}).get("code", "HTTP_FAILURE"), stage)
        return data

    async def step(self, count=1, params=None):
        for _ in range(count):
            self.clock_ms = self.runtime.world["sim_time_ms"] + 100
            await self.request("POST", f"/api/v1/test/runs/{self.run}/control",
                               {"action": "step", "action_params": params}, stage="step", operator=True)
            self.runtime.devices.reconcile()

    async def devices(self):
        return await self.request("GET", f"/api/v1/facilities/{FACILITY}/devices?run_id={self.run}", stage="devices")

    async def fault(self, channel, failed=True):
        await self.request("PUT", f"/api/v1/test/runs/{self.run}/device-faults",
                           {"channel": channel, "failed": failed, "expected_state_version": self.runtime.world["state_version"]},
                           stage="device_fault", operator=True)

    async def process(self, command_id=None, *, key=None, stage="process"):
        rig = self

        class Adapter:
            async def decide(self, context):
                augmented = deepcopy(context)
                augmented["public_devices"] = rig.runtime.public_devices(rig.runtime.world)
                entry = {"input": augmented, "condition_fingerprint": sha(comparison_condition(augmented)),
                         "decision": None, "provider_calls": 0}
                rig.decisions.append(entry)
                if rig.decision_fault:
                    raise ApiError(503, "DECISION_UNAVAILABLE", "Injected unavailable decision boundary")
                if rig.method == "mock_operations_agent":
                    decision = await MockOperationsAdapter().decide(augmented)
                else:
                    index = len(rig.decisions) - 1 - rig.decision_offset - int(rig.case["fault"] == "decision_unavailable" and len(rig.decisions) > 1)
                    action = rig.manual_trace[min(index, len(rig.manual_trace) - 1)] if rig.method == "prerecorded_manual" else s2s3_rule(augmented)
                    decision = AutonomousDecision(scenario=context["scenario"], action=action,
                        reason_code="SUPPLEMENTAL_COMPARISON", rationale="사전에 고정한 무과금 비교 판단").model_dump()
                entry["decision"] = deepcopy(decision)
                return decision

            def result_metadata(self):
                return {"mode": "mock", "provider": None, "model_ref": rig.method,
                        "model_call_count": 0, "cost_estimated_krw": 0}

        body = {"run_id": self.run, "action": "process", "mode": "mock", "scenario": self.case["scenario"], "command_id": command_id}
        with patch("backend.autonomous.MockOperationsAdapter", Adapter):
            if self.search_fault:
                with patch.object(self.runtime.knowledge, "_index", side_effect=OSError("injected index read failure")):
                    return await self.request("POST", "/api/v1/test/agent/operations", body, stage=stage, key=key)
            return await self.request("POST", "/api/v1/test/agent/operations", body, stage=stage, key=key)

    async def restart(self):
        before = {"devices": await self.devices(), "run": public_state(self.runtime.world)}
        await close_runtime(self.runtime)
        self.wire_runtime(Runtime(self.database))
        await self.login()
        after = {"devices": await self.devices(), "run": public_state(self.runtime.world)}
        self.trace.append({"stage": "restart", "before": before, "after": after})

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
        if self.runtime is not None:
            await close_runtime(self.runtime)
        if self.directory.exists():
            shutil.rmtree(s2s3_path(self.directory))


async def execute_s2s3_case(case, method, directory, *, recovery_probe=False):
    if method not in BUSINESS_METHODS:
        raise ValueError("Unknown supplemental method")
    if recovery_probe and (case["scenario"], case["fault"]) != ("s3", "broadcast_failed"):
        raise ValueError("Recovery probe requires the S3 playback failure case")
    rig = S2S3Rig(directory, method, case)
    row = {"case": case["id"], "method": method, "status": "executed", "provider_calls": 0}
    started = time.perf_counter()
    # Deterministic synthetic UTC must increase across public observations.
    # Actual monotonic deadline/authentication/latency clocks remain untouched.
    clocks = [patch(f"{module}.utc_now", rig.utc) for module in
              ("simulator.world", "simulator.environment", "backend.runtime", "backend.device_operations")]
    class CalibrationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime.fromisoformat(rig.utc().replace("Z", "+00:00"))
            return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)
    from contextlib import ExitStack
    try:
        with ExitStack() as stack:
            for fixed in clocks:
                stack.enter_context(fixed)
            stack.enter_context(patch("simulator.spatial.datetime", CalibrationClock))
            stack.enter_context(patch("backend.operating_analysis.datetime", CalibrationClock))
            await rig.open()
            created = await rig.request("POST", "/api/v1/test/runs", {"facility_id": FACILITY,
                "fixture_ref": case["fixture"], "config_ref": "sim0-v1", "seed": case["seed"]}, stage="create", operator=True)
            rig.run = created["run_id"]
            if case["scenario"] == "s2":
                if case["fault"] in ("audio_failed", "visual_failed"):
                    await rig.fault(case["fault"].split("_")[0])
                candidate = None
                for tick in range(1, case["initial_tick_cap"] + 1):
                    await rig.step()
                    options = [c for c in rig.runtime.operating_candidates("s2") if c["assessment"].get("support_status") == "supported" and c["assessment"].get("violation_candidate")]
                    if len(options) == 1:
                        candidate = options[0]
                        row["first_candidate_tick"] = tick
                        break
                if candidate is None:
                    row.update(status="not_observed", reason="no supported public risk within declared tick cap")
                else:
                    await rig.step()  # pending -> actual channel feedback, same for every method
                    row["capture_devices"] = await rig.devices()
                    rig.search_fault = case["fault"] == "search_unavailable"
                    rig.decision_fault = case["fault"] == "decision_unavailable"
                    row["first_result"] = await rig.process()
                    if rig.search_fault or rig.decision_fault:
                        rig.search_fault = rig.decision_fault = False
                        row["recovery_result"] = await rig.process()
                    row["final_devices"] = await rig.devices()
            else:
                await rig.step(case["initial_ticks"])
                command = await rig.request("POST", f"/api/v1/facilities/{FACILITY}/commands",
                    {"run_id": rig.run, "purpose": "operational_goal", "text": S2S3_CONTRACT["s3_command"],
                     "based_on_state_version": rig.runtime.world["state_version"]}, stage="command")
                cid = command["command_id"]
                await rig.process(cid)
                await rig.request("POST", f"/api/v1/commands/{cid}/clarify",
                    {"expected_resource_version": command["resource_version"], "goal": "closing"}, stage="clarify")
                await rig.process(cid)
                plan = await rig.request("GET", f"/api/v1/commands/{cid}/plan", stage="plan_preview")
                await rig.request("POST", f"/api/v1/commands/{cid}/confirm",
                    {"expected_resource_version": plan["command_version"]}, stage="confirm")
                if case["fault"] == "broadcast_failed":
                    await rig.fault("simulated_playback")
                for index in range(2):
                    result = await rig.process(cid, key=f"announcement-{index}")
                    if index == 0 and case["fault"] == "same_key":
                        row["duplicate_result"] = await rig.process(cid, key=f"announcement-{index}")
                        row["duplicate_original"] = deepcopy(result)
                    if index == 0 and case["fault"] == "restart":
                        await rig.restart()
                        row["pre_resume_guard"] = await rig.process(cid, stage="pre_resume_guard")
                    await rig.step(S2S3_CONTRACT["s3_broadcast_feedback_ticks"])
                    if case["fault"] == "broadcast_failed":
                        row["failure_result"] = await rig.process(cid)
                        break
                if case["fault"] != "broadcast_failed":
                    row["entry_result"] = await rig.process(cid)
                    row["entry_devices"] = await rig.devices()
                    for object_id in ("obj-car-s3-u", "obj-car-s3-w"):
                        await rig.step(S2S3_CONTRACT["s3_portal_request_ticks"], {"request_portal_attempt": object_id})
                    await rig.step(S2S3_CONTRACT["s3_portal_followup_ticks"])
                    row["followup_result"] = await rig.process(cid)
                if recovery_probe:
                    row["before_healing"] = await rig.devices()
                    await rig.fault("simulated_playback", False)
                    row["same_command_after_healing"] = await rig.process(cid)
                    # Preserve failed history; use an explicitly new, confirmed
                    # command after a declared synthetic cooldown. No retries
                    # are hidden in the product or in this experiment.
                    await rig.step(310)
                    rig.decision_offset = len(rig.decisions)
                    rig.manual_trace = S2S3_CONTRACT["s3_manual_trace"]
                    new = await rig.request("POST", f"/api/v1/facilities/{FACILITY}/commands",
                        {"run_id": rig.run, "purpose": "operational_goal", "text": S2S3_CONTRACT["s3_command"],
                         "based_on_state_version": rig.runtime.world["state_version"]}, stage="replan_command")
                    new_id = new["command_id"]
                    await rig.process(new_id)
                    await rig.request("POST", f"/api/v1/commands/{new_id}/clarify",
                        {"expected_resource_version": new["resource_version"], "goal": "closing"}, stage="replan_clarify")
                    await rig.process(new_id)
                    preview = await rig.request("GET", f"/api/v1/commands/{new_id}/plan", stage="replan_preview")
                    await rig.request("POST", f"/api/v1/commands/{new_id}/confirm",
                        {"expected_resource_version": preview["command_version"]}, stage="replan_confirm")
                    for index in range(2):
                        await rig.process(new_id, key=f"replan-announcement-{index}")
                        await rig.step(S2S3_CONTRACT["s3_broadcast_feedback_ticks"])
                    row["replan_entry_result"] = await rig.process(new_id)
                    row["replanned_command"] = await rig.request("GET", f"/api/v1/commands/{new_id}", stage="replan_result")
                    row["replanned_plan"] = await rig.request("GET", f"/api/v1/commands/{new_id}/plan", stage="replan_plan_result")
                row["final_devices"] = await rig.devices()
                row["command"] = await rig.request("GET", f"/api/v1/commands/{cid}", stage="command_result")
                row["plan"] = await rig.request("GET", f"/api/v1/commands/{cid}/plan", stage="plan_result")
            row["final_public_state"] = public_state(rig.runtime.world)
            row["counts"] = {name: rig.runtime.store.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                             for name in ("incidents", "notifications", "executions", "autonomous_jobs", "knowledge_retrievals")}
            row["executions"] = [dict(r) for r in rig.runtime.store.db.execute("SELECT tool_name,target_ref,status FROM executions ORDER BY rowid")]
            row["notifications"] = [dict(r) for r in rig.runtime.store.db.execute("SELECT purpose,delivery_status FROM notifications ORDER BY rowid")]
            row["retrievals"] = [json.loads(r[0]) for r in rig.runtime.store.db.execute("SELECT result_json FROM knowledge_retrievals ORDER BY rowid")]
    except Exception as exc:
        row.update(status="error", reason=type(exc).__name__, error_code=getattr(exc, "code", None), detail=str(exc))
    finally:
        await rig.close()
    row.update(trace=rig.trace, decisions=rig.decisions,
               wall_latency_ms=(time.perf_counter() - started) * 1000,
               cleanup={"scratch_removed": not Path(directory).exists()})
    return row


def s2s3_rubric():
    """Evaluation only. Execution never loads this outcome specification."""
    return {c["id"]: {"scenario": c["scenario"], "fault": c["fault"],
                       "allowed_actions": ["report", "recheck"] if c["scenario"] == "s2" else ["clarify", "announce", "restrict_entry", "hold"]}
            for c in S2S3_CASES}


def evaluate_s2s3(rows, rubric):
    evaluated = deepcopy(rows)
    fingerprints = defaultdict(list)
    for row in evaluated:
        fingerprints[row["case"]].append([d["condition_fingerprint"] for d in row["decisions"]])
    for row in evaluated:
        wanted = rubric[row["case"]]
        checks = {"executed": row["status"] == "executed", "provider_zero": row["provider_calls"] == 0,
                  "cleanup": row["cleanup"]["scratch_removed"],
                  "same_input": len({encoded(f) for f in fingerprints[row["case"]]}) == 1,
                  "allowed_actions": all(d["decision"] is None or d["decision"]["action"] in wanted["allowed_actions"] for d in row["decisions"])}
        if row["status"] == "executed":
            devices = row["final_devices"]
            if wanted["scenario"] == "s2":
                alarm = devices["alarms"][0]
                channels = {"visual": "on", "audio": "on"}
                if wanted["fault"] in ("audio_failed", "visual_failed"):
                    channels[wanted["fault"].split("_")[0]] = "failed"
                checks.update(independent_alarm=alarm["claim_count"] > 0 and all(alarm[k] == v for k, v in channels.items()),
                              one_report=sum(n["purpose"] == "owner_report" for n in row["notifications"]) == 1,
                              no_device_business_action=not any(e["tool_name"] in ("play_announcement", "set_entry_policy") for e in row["executions"]))
                if wanted["fault"] == "search_unavailable":
                    checks["real_search_unavailable"] = row["decisions"][0]["input"]["knowledge"]["status"] == "unavailable" and not row["decisions"][0]["input"]["knowledge"]["references"]
                    checks["search_recovered"] = row["decisions"][-1]["input"]["knowledge"]["status"] == "matched"
                if wanted["fault"] == "decision_unavailable":
                    checks["decision_failed"] = row["first_result"].get("error", {}).get("code") == "DECISION_UNAVAILABLE" and row["decisions"][0]["decision"] is None
                    checks["decision_recovered"] = row["recovery_result"].get("status") == "accepted"
            else:
                entry = next(g for g in devices["gates"] if g["direction"] == "entry")
                exit_gate = next(g for g in devices["gates"] if g["direction"] == "exit")
                checks["formal_confirmation"] = any(t["stage"] == "confirm" and t["http_status"] == 200 for t in row["trace"])
                checks["exit_open"] = exit_gate["physical_state"] == "open"
                if wanted["fault"] == "broadcast_failed":
                    checks.update(failed_playback=any(b["simulated_playback"] == "failed" for b in devices["broadcasts"]),
                                  entry_held=entry["entry_policy"] == "allow" and row["failure_result"].get("status") == "held",
                                  no_early_success=row["command"]["aggregate_status"] != "succeeded")
                else:
                    object_ids = {o["object_id"] for o in row["final_public_state"]["snapshot"]["objects"]}
                    checks.update(both_played={b["zone_id"] for b in devices["broadcasts"] if b["receipt"] == "accepted" and b["simulated_playback"] == "played"} == {"announcement-a", "announcement-b"},
                                  entry_denied=entry["entry_policy"] == "deny", entrant_retained="obj-car-s3-u" in object_ids,
                                  outbound_absent="obj-car-s3-w" not in object_ids,
                                  completed=row["command"]["aggregate_status"] == "succeeded" and row["plan"]["status"] == "completed",
                                  no_duplicate_broadcast=sum(e["tool_name"] == "play_announcement" for e in row["executions"]) == 2)
                    if wanted["fault"] == "same_key":
                        checks["same_key_replayed"] = row["duplicate_result"] == row["duplicate_original"]
                    if wanted["fault"] == "restart":
                        restart = next(t for t in row["trace"] if t["stage"] == "restart")
                        checks["durable_pending_broadcast"] = restart["before"]["devices"]["broadcasts"] == restart["after"]["devices"]["broadcasts"]
                        checks["explicit_recovery"] = restart["after"]["run"]["recovery_required"] and row["pre_resume_guard"].get("error", {}).get("code") == "RECOVERY_REQUIRED"
        row["checks"] = checks
        row["mismatches"] = [k for k, ok in checks.items() if not ok]
        row["matched"] = not row["mismatches"]
    summary = [{"method": method, "conditions": len(rubric), "matched": sum(r["matched"] for r in evaluated if r["method"] == method),
                "failed": sum(not r["matched"] and r["status"] != "not_observed" for r in evaluated if r["method"] == method),
                "not_observed": sum(r["status"] == "not_observed" for r in evaluated if r["method"] == method)} for method in BUSINESS_METHODS]
    return {"summary": summary, "attempts": evaluated, "failed": sum(s["failed"] for s in summary)}


async def run_s2s3_comparison(output):
    output = s2s3_path(output)
    if output.exists():
        raise FileExistsError("Report already exists; choose a new output name")
    preflight_manifest("sim0")
    scratch = S2S3_ROOT / "tmp" / uuid4().hex
    rows = [await execute_s2s3_case(case, method, scratch / case["id"] / method)
            for case in S2S3_CASES for method in BUSINESS_METHODS]
    if scratch.exists() and not any(p.is_file() for p in scratch.rglob("*")):
        shutil.rmtree(s2s3_path(scratch))
    report = {"version": S2S3_SPEC, "contract": deepcopy(S2S3_CONTRACT), "source_provenance": source_provenance(),
              "evaluation": evaluate_s2s3(rows, s2s3_rubric()), "provider_calls": 0, "server_starts": 0,
              "overall_acceptance": {"passed": 0, "denominator": 194},
              "semantic_support": {"status": "not_observed", "reason": "No natural-language answer/claim evidence set or human adjudications supplied"},
              "unsupported": [{"condition": "prescribed_unknown_feedback", "status": "not_supported", "reason": S2S3_CONTRACT["unknown"]}],
              "limits": ["One synthetic calibration per method/condition; no superiority or human-speed claim",
                         "Independent S2 warning is common safety infrastructure, not Agent performance",
                         "Queued owner report does not prove dispatch/receipt; simulated playback is not browser audio",
                         "Failed playback is preserved; channel healing/replanning is not claimed",
                         "Structural references do not establish semantic support; rubric needs actual answers, returned excerpts and human claim judgments"],
              "cleanup": {"scratch_removed": not scratch.exists()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, choices=(1, 3), default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--business-only", action="store_true", help="Separate prerecorded/rule/mock business experiment; no R08 score change")
    mode.add_argument("--business-s2-s3", action="store_true", help="Opt-in supplemental S2/S3 ASGI calibration; no final score change")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    args.output = args.output or (S2S3_ROOT if args.business_s2_s3 else ARTIFACT_ROOT) / "outputs" / (uuid4().hex + ".json")
    if args.business_s2_s3:
        if args.repeat != 1:
            parser.error("S2/S3 calibration declares one attempt per condition")
        output = args.output
        report = asyncio.run(run_s2s3_comparison(output))
        print(json.dumps({"output": str(output), "summary": report["evaluation"]["summary"],
                          "cleanup": report["cleanup"]}, ensure_ascii=False))
        return 1 if report["evaluation"]["failed"] or not report["cleanup"]["scratch_removed"] else 0
    if args.business_only:
        report = asyncio.run(run_business_comparison(args.output, args.repeat))
        failed = sum(s["failed"] for run in report["runs"] for s in run["summary"])
        print(json.dumps({"output": str(args.output.resolve()), "provider_calls": 0,
                          "overall_acceptance": report["overall_acceptance"], "failed_attempts": failed,
                          "cleanup": report["cleanup"]}, ensure_ascii=False))
        return 1 if failed or not report["cleanup"]["scratch_removed"] else 0
    report = asyncio.run(run_comparison(args.output, args.repeat))
    print(json.dumps({"output": str(args.output.resolve()), "provider_calls": 0,
                      "overall_acceptance": report["overall_acceptance"],
                      "error_attempts": sum(s["error_attempts"] for s in report["summary"]),
                      "cleanup": report["cleanup"]}, ensure_ascii=False))
    return 1 if any(s["error_attempts"] for s in report["summary"]) or not report["cleanup"]["scratch_removed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
