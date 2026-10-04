"""Server-internal read tools. No HTTP route or model-supplied security context."""
from dataclasses import dataclass, field
import time
import sqlite3

from pydantic import ValidationError

from backend.auth import ApiError
from backend.knowledge import LIMITS
from contracts.knowledge import KnowledgeQuery, PolicyQuery
from simulator.world import FACILITY, MAP

TOOL_INPUTS = {"get_operating_policy": PolicyQuery, "search_operating_knowledge": KnowledgeQuery}


@dataclass
class ReadTask:
    # Created by the server after authentication, never deserialized from a tool
    # request. All retries share this object and consume the same budget.
    username: str
    role: str
    run_id: str
    created_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    retrieval_calls: int = 0

    def consume(self, session, name):
        if (session.username, session.role) != (self.username, self.role):
            raise ApiError(403, "FORBIDDEN", "다른 주체의 작업을 사용할 수 없습니다.")
        if time.monotonic() - self.created_at >= 30 or self.tool_calls >= 16:
            raise ApiError(429, "TASK_LIMIT", "작업 한도에 도달했습니다.")
        self.tool_calls += 1
        if name == "search_operating_knowledge":
            if self.retrieval_calls >= LIMITS["calls_per_task"]:
                raise ApiError(429, "RETRIEVAL_LIMIT", "작업의 검색 한도에 도달했습니다.")
            self.retrieval_calls += 1


def validate_session(runtime, session):
    if session.expires <= time.monotonic() or runtime.store.registry.role(session.username) != session.role:
        raise ApiError(401, "UNAUTHENTICATED", "현재 인증을 다시 확인하세요.")


def call_read_tool(runtime, session, name, arguments, task):
    """Runtime holds its lock before entering this synchronous adapter."""
    validate_session(runtime, session)
    task.consume(session, name)
    if name not in TOOL_INPUTS:
        raise ApiError(422, "UNKNOWN_TOOL", "등록된 조회 도구를 확인하세요.")
    try:
        query = TOOL_INPUTS[name].model_validate(arguments)
    except ValidationError:
        raise ApiError(422, "INVALID_INPUT", "조회 도구 인자를 확인하세요.") from None
    if query.facility_id != FACILITY:
        raise ApiError(404, "NOT_FOUND", "시설을 찾을 수 없습니다.")
    if query.zone_id is not None and query.zone_id not in {z["zone_id"] for z in MAP["zones"]}:
        raise ApiError(404, "NOT_FOUND", "지도 구역을 찾을 수 없습니다.")
    runtime.ensure_run(task.run_id)
    if name == "search_operating_knowledge":
        if query.run_id != task.run_id:
            raise ApiError(404, "RUN_NOT_FOUND", "현재 작업의 실행 회차를 확인하세요.")
        return runtime.knowledge.search(session.username, query).model_dump()
    # The current slice deliberately does not expose historic policy audit reads.
    try:
        policy = runtime.knowledge.current_policy(FACILITY)
    except (ValueError, OSError, sqlite3.Error):
        raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", "현재 운영 정책을 확인할 수 없습니다.") from None
    if query.policy_version is not None and query.policy_version != policy.policy_version:
        raise ApiError(409, "KNOWLEDGE_CHANGED", "현재 정책 버전을 조회하세요.")
    return policy.model_dump()
