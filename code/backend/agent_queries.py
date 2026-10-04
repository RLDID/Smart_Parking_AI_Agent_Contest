"""Bounded mock and opt-in live reads. No action tools or autonomous service grant."""
import asyncio
from copy import deepcopy
import sqlite3
import time

from pydantic import Field, ValidationError

from agent.loop import LoopLimits, MockReadAdapter, ModelFailure, run_read_loop
from agent.tools import call_read_tool, validate_session
from backend.auth import ApiError
from backend.knowledge import transaction
from backend.business import encoded
from contracts.models import Contract
from contracts.agent_loop import LiveAgentQuery
from simulator.world import FACILITY, digest, public_state


class EmptyArguments(Contract):
    pass


class SearchArguments(Contract):
    query: str = Field(min_length=1, max_length=500)


class QueryService:
    def __init__(self, runtime, adapter_factory=MockReadAdapter, live_models=None):
        self.runtime = runtime
        self.adapter_factory = adapter_factory  # Trusted test injection, never HTTP input.
        self.live_models = live_models
        self.active = {}
        self.closed = False

    def stamp(self, session, request, authenticate):
        if self.closed:
            raise ApiError(503, "AGENT_UNAVAILABLE", "조회 작업이 종료됐습니다.")
        authenticate()
        r = self.runtime
        validate_session(r, session)
        r.ensure_run(request.run_id)
        if r.world.get("replay_state") is not None and isinstance(request, LiveAgentQuery):
            raise ApiError(409, "REPLAY_MODE", "기록 재생 중에는 새 유료 모델을 호출하지 않습니다.")
        if r.failure:
            raise ApiError(503, "STATE_UNAVAILABLE", "현재 저장 상태를 확인하세요.")
        value = {"scope": r.store.registry.scope_stamp(session.username),
                 "run_id": request.run_id, "state_version": r.world["state_version"]}
        if isinstance(request, LiveAgentQuery) and self.live_models is not None:
            value["model_configuration"] = self.live_models.configuration.model_dump(mode="json")
        if request.goal == "regulation":
            try:
                policy = r.knowledge.current_policy(FACILITY)
                value["policy"] = [policy.policy_version, policy.knowledge_release_id]
            except (ValueError, OSError, sqlite3.Error):
                value["policy"] = None
            value["documents"] = [tuple(row) for row in r.store.db.execute(
                "SELECT document_id,document_version,content_digest,allowed_roles_json,approval_status,effective_at,retired_at,reviewed_conflict "
                "FROM knowledge_documents WHERE facility_id=? ORDER BY document_id,document_version", (FACILITY,))]
        return digest(value)

    async def execute(self, session, request, key, authenticate):
        r = self.runtime
        pair = (session.username, key)
        async with r.lock:
            stamp = self.stamp(session, request, authenticate)
            live = isinstance(request, LiveAgentQuery)
            route = None
            if live:
                if self.live_models is None:
                    raise ApiError(503, "MODEL_NOT_CONFIGURED", "실제 조회 설정으로 로컬 프로그램을 시작하세요.")
                if r.world["run_status"] != "paused":
                    raise ApiError(409, "LIVE_REQUIRES_PAUSED", "API 비교 시험은 가상 실행을 일시정지한 뒤 진행하세요.")
            action = "live_read_query" if live else "mock_read_query"
            fingerprint, old = r.business.key(session.username, key, action, request.model_dump())
            if old:
                if old["context_stamp"] != stamp:
                    raise ApiError(409, "QUERY_CONTEXT_CHANGED", "이전 조회의 권한·상태·근거가 바뀌었습니다. 새 조회를 시작하세요.")
                if old.get("pending"):
                    if pair not in self.active:
                        raise ApiError(409, "QUERY_RECONCILIATION_REQUIRED", "이 요청은 중단·미확인 상태입니다. 같은 키로 유료 호출을 다시 시작하지 않습니다.")
                else:
                    self.validate_answer(session, old["response"], time.monotonic() + 2)
                    if self.stamp(session, request, authenticate) != stamp:
                        raise ApiError(409, "QUERY_CONTEXT_CHANGED", "재조회 중 권한·문맥이 바뀌었습니다.")
                    return deepcopy(old["response"])
            if pair in self.active:
                if self.active[pair][0] != fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 키에 다른 조회를 보낼 수 없습니다.")
                job = self.active[pair][1]
                deadline = self.active[pair][2]
            else:
                if len(self.active) >= 2 or any(user == session.username for user, _ in self.active):
                    raise ApiError(429, "AGENT_QUEUE_LIMIT", "진행 중인 조회가 끝난 뒤 다시 요청하세요.")
                task = r.read_task(session, request.run_id)
                if live:
                    try:
                        route = self.live_models.select_route(request.provider)
                    except ModelFailure as error:
                        raise ApiError(503, error.reason_code, "제공자 설정과 새 프로세스의 키 등록 상태를 확인하세요.") from None
                    # Persist admission before any paid dispatch. On cancellation,
                    # context failure or restart, this key cannot reissue a charge.
                    with transaction(r.store.db):
                        r.business.save_key(session.username, key, fingerprint,
                            {"context_stamp": stamp, "pending": True, "route": route})
                job = asyncio.create_task(self.run(session, request, key, fingerprint, stamp, task, authenticate, route))
                job.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
                deadline = task.created_at + 30
                self.active[pair] = (fingerprint, job, deadline)
        # Disconnect does not create a replacement job. All callers share its
        # bounded result; authentication is checked again after awaiting it.
        result = await asyncio.shield(job)
        async with r.lock:
            if self.stamp(session, request, authenticate) != stamp:
                raise ApiError(409, "QUERY_CONTEXT_CHANGED", "조회 도중 문맥이 바뀌었습니다.")
            self.validate_answer(session, result, deadline)
            if time.monotonic() >= deadline:
                raise ApiError(429, "TASK_LIMIT", "조회 결과 전달 전에 작업 한도에 도달했습니다.")
            if self.stamp(session, request, authenticate) != stamp:
                raise ApiError(409, "QUERY_CONTEXT_CHANGED", "결과 전달 전에 현재 문맥을 확인하세요.")
            return deepcopy(result)

    def validate_answer(self, session, response, deadline):
        r = self.runtime
        # A cached read is not an exemption from document expiry, withdrawal,
        # current audience or index validation. Never replay expired excerpts.
        for item in response["tool_results"]:
            if item["name"] != "search_operating_knowledge" or item["result"].get("status") != "matched":
                continue
            found = item["result"]
            try:
                policy = r.knowledge.current_policy(FACILITY)
                version, _ = r.knowledge._index(FACILITY, policy.knowledge_release_id, expires=deadline)
                eligible = {row["reference_id"]: r.knowledge._reference(row)
                            for row in r.knowledge._eligible(FACILITY, policy.knowledge_release_id, session.role, r.knowledge.clock())}
            except (ValueError, OSError, sqlite3.Error, TimeoutError, KeyError):
                raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", "최종 문서 근거를 확인할 수 없습니다.") from None
            if (version != found["index_version"] or policy.policy_version != found["policy_version"]
                    or any(eligible.get(ref["reference_id"]) != ref for ref in found["references"])):
                raise ApiError(409, "KNOWLEDGE_CHANGED", "현재 유효한 문서 근거로 다시 조회하세요.")

    async def run(self, session, request, key, fingerprint, stamp, task, authenticate, route=None):
        r = self.runtime
        async def check():
            async with r.lock:
                if self.stamp(session, request, authenticate) != stamp:
                    raise ApiError(409, "QUERY_CONTEXT_CHANGED", "조회 도중 권한·관측·문서가 바뀌었습니다.")

        allowed = {"get_parking_state", "get_my_vehicles"}
        if request.goal == "regulation":
            allowed = {"search_operating_knowledge"}
        elif request.goal == "current_state" and session.role in ("owner", "test_operator"):
            allowed.add("analyze_spatial_context")

        async def call(name, arguments):
            async with r.lock:
                if self.stamp(session, request, authenticate) != stamp:
                    raise ApiError(409, "QUERY_CONTEXT_CHANGED", "도구 호출 전에 현재 문맥을 확인하세요.")
                if name not in allowed:
                    raise ApiError(403, "FORBIDDEN", "이 조회의 허용 도구가 아닙니다.")
                try:
                    model = SearchArguments if name == "search_operating_knowledge" else EmptyArguments
                    args = model.model_validate(arguments)
                except ValidationError:
                    raise ApiError(422, "INVALID_TOOL_INPUT", "조회 도구 인자를 확인하세요.") from None
                if name == "search_operating_knowledge":
                    return call_read_tool(r, session, name, {"facility_id": FACILITY, "run_id": request.run_id,
                        "query": args.query}, task)
                task.consume(session, name)
                if name == "get_parking_state":
                    return r.store.registry.project(session.username, public_state(r.world), r.world["sim_time_ms"])
                if name == "get_my_vehicles":
                    return {"facility_id": FACILITY, "vehicles": r.store.registry.vehicles(session.username)}
                return r.business.analysis().model_dump()

        try:
            deadline = task.created_at + 30
            loop_seconds = min(28, deadline - time.monotonic() - 2)
            if loop_seconds <= 0:
                raise ApiError(429, "TASK_LIMIT", "최종 근거 확인 시간을 포함한 작업 한도입니다.")
            live = isinstance(request, LiveAgentQuery)
            try:
                adapter = self.live_models.adapter(route["provider"], check) if live else self.adapter_factory()
            except ModelFailure as error:
                raise ApiError(503, error.reason_code, "제공자 키와 설정을 확인하세요.") from None
            model_timeout = self.live_models.configuration.providers[route["provider"]].timeout_seconds if live else 5
            response = await run_read_loop(request, adapter, call,
                check_context=check, allowed_tools=frozenset(allowed),
                limits=LoopLimits(wall_seconds=loop_seconds, model_timeout_seconds=model_timeout),
                result_metadata=adapter.result_metadata if live else None)
            if live:
                response.update(route)
            async with r.lock:
                if self.stamp(session, request, authenticate) != stamp:
                    raise ApiError(409, "QUERY_CONTEXT_CHANGED", "최종 결과의 문맥을 다시 확인하세요.")
                self.validate_answer(session, response, deadline)
                if time.monotonic() >= deadline:
                    raise ApiError(429, "TASK_LIMIT", "최종 확인 중 작업 한도에 도달했습니다.")
                if self.stamp(session, request, authenticate) != stamp:
                    raise ApiError(409, "QUERY_CONTEXT_CHANGED", "최종 확인 중 문맥이 바뀌었습니다.")
                response["elapsed_ms"] = max(0, round((time.monotonic() - task.created_at) * 1000))
                with transaction(r.store.db):
                    saved = {"context_stamp": stamp, "response": response}
                    if live:
                        changed = r.store.db.execute("UPDATE business_requests SET response_json=? "
                            "WHERE facility_id=? AND requester_ref=? AND key=? AND argument_hash=?",
                            (encoded(saved), FACILITY, session.username, key, fingerprint)).rowcount
                        if changed != 1:
                            raise ApiError(409, "QUERY_CONTEXT_CHANGED", "유료 조회의 접수 기록을 확인하세요.")
                    else:
                        r.business.save_key(session.username, key, fingerprint, saved)
                    r.business.audit(session.username, "live_read_query" if live else "mock_read_query", request.run_id, request.run_id,
                        response["status"], response["reason_code"])
                return response
        finally:
            async with r.lock:
                self.active.pop((session.username, key), None)

    async def close(self):
        self.closed = True
        jobs = [entry[1] for entry in self.active.values()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
