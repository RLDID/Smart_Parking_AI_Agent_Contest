"""A bounded read-only loop for mock or opt-in paid models, without action authority."""

import asyncio
from dataclasses import dataclass
import json
import math
import time
from typing import Any
from weakref import WeakSet

from pydantic import ValidationError

from contracts.agent_loop import AgentQuery, ModelTurn


_MAX_MODEL_CALLS = 4
_MAX_TOOL_CALLS = 16
_MAX_WALL_SECONDS = 30.0
_MODEL_BYTES = 16 * 1024
_TOOL_BYTES = 32 * 1024
_MEMORY_BYTES = 64 * 1024
_FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "username", "user", "role", "session", "token", "auth", "authorization",
    "mode", "model_ref", "cost", "cost_actual_usd", "budget", "channel",
    "recipient_ref", "requester_ref", "policy_version", "knowledge_release_id",
    "facility_id", "run_id",
})
# If a callback refuses cancellation, do not create further jobs until it exits.
# Read callbacks have no mutation authority. Their eventual result is discarded.
_abandoned_jobs: WeakSet[asyncio.Task] = WeakSet()


@dataclass(frozen=True)
class LoopLimits:
    max_model_calls: int = _MAX_MODEL_CALLS
    max_tool_calls: int = _MAX_TOOL_CALLS
    wall_seconds: float = _MAX_WALL_SECONDS
    model_timeout_seconds: float = 5.0
    tool_timeout_seconds: float = 3.0

    def __post_init__(self):
        if (type(self.max_model_calls) is not int or not 1 <= self.max_model_calls <= _MAX_MODEL_CALLS
                or type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= _MAX_TOOL_CALLS):
            raise ValueError("Invalid loop count limit")
        for value, cap in ((self.wall_seconds, _MAX_WALL_SECONDS),
                           (self.model_timeout_seconds, _MAX_WALL_SECONDS),
                           (self.tool_timeout_seconds, _MAX_WALL_SECONDS)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= cap:
                raise ValueError("Invalid loop time limit")


def _json_size(value: Any, maximum: int) -> int:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("Non-JSON value") from error
    if size > maximum:
        raise ValueError("JSON limit exceeded")
    return size


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(not isinstance(key, str) or key.lower() in _FORBIDDEN_ARGUMENT_KEYS or _has_forbidden_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


async def _bounded(awaitable, seconds: float):
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            task.cancel()
            _abandoned_jobs.add(task)
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise TimeoutError
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        _abandoned_jobs.add(task)
        task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
        raise


def _reason_for_knowledge(status: str) -> str:
    return {"unavailable": "KNOWLEDGE_UNAVAILABLE", "conflict": "KNOWLEDGE_CONFLICT",
            "no_match": "KNOWLEDGE_REQUIRED"}.get(status, "KNOWLEDGE_REQUIRED")


class MockReadAdapter:
    """Deterministic test adapter: the caller explicitly selects a read goal."""

    async def next_turn(self, model_input: dict) -> dict:
        request = AgentQuery.model_validate(model_input["request"])
        allowed = set(model_input["allowed_tools"])
        previous = model_input["tool_results"]
        if previous:
            if request.goal == "regulation":
                result = previous[-1]["result"]
                if not isinstance(result, dict) or result.get("status") != "matched":
                    status = result.get("status", "") if isinstance(result, dict) else ""
                    return {"finish": {"status": "needs_review", "reason_code": _reason_for_knowledge(status)}}
            return {"finish": {"status": "completed", "reason_code": "READ_COMPLETED"}}
        tools = {
            "current_state": ("get_parking_state",) + (("analyze_spatial_context",)
                                                      if "analyze_spatial_context" in allowed else ()),
            "my_vehicle": ("get_my_vehicles", "get_parking_state"),
            "regulation": ("search_operating_knowledge",),
        }[request.goal]
        if any(name not in allowed for name in tools):
            return {"finish": {"status": "needs_review", "reason_code": "TOOL_NOT_ALLOWED"}}
        return {"tool_calls": [
            {"call_id": f"read-{index + 1}", "name": name,
             "arguments": {"query": request.query} if name == "search_operating_knowledge" else {}}
            for index, name in enumerate(tools)
        ]}


class ModelFailure(RuntimeError):
    """Safe admission/provider reason; never contains a raw provider response."""

    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


async def run_read_loop(request: AgentQuery, adapter, call_tool, *, check_context,
                        allowed_tools: frozenset[str], limits: LoopLimits | None = None,
                        result_metadata=None) -> dict:
    """Run server-allowed reads; recheck authority on both sides of every await."""
    if not isinstance(request, AgentQuery) or not isinstance(allowed_tools, frozenset):
        raise TypeError("Server must provide a validated query and frozen tool set")
    limits = limits or LoopLimits()
    if not isinstance(limits, LoopLimits):
        raise TypeError("Server must provide LoopLimits")
    started = time.monotonic()
    deadline = started + limits.wall_seconds
    results: list[dict] = []
    seen: set[str] = set()
    model_calls = 0
    tool_calls = 0
    reason = "MODEL_LIMIT"
    status = "needs_review"
    answer = ""

    async def result():
        nonlocal status, reason
        await check_context()
        if time.monotonic() >= deadline:
            status, reason = "needs_review", "WALL_LIMIT"
        output = {"mode": "mock", "model_ref": "mock-read-v1", "status": status,
                  "reason_code": reason, "model_calls": model_calls, "tool_calls": tool_calls,
                  "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
                  "cost_actual_usd": 0, "tool_results": results}
        if result_metadata is not None:
            output.update(result_metadata())  # Trusted server adapter, not model output.
        if answer:
            output["answer"] = answer
        try:
            _json_size(output, _MEMORY_BYTES)
        except ValueError:
            # The response envelope also counts toward the memory limit. Do not
            # return a truncated set of references as a completed read.
            output.update(status="needs_review", reason_code="RESULT_LIMIT", tool_results=[])
            output.pop("answer", None)
            _json_size(output, _MEMORY_BYTES)
        return output

    if any(not isinstance(name, str) or not name for name in allowed_tools):
        raise TypeError("Invalid server tool allowlist")
    await check_context()
    for _ in range(limits.max_model_calls):
        if any(not job.done() for job in _abandoned_jobs):
            reason = "PENDING_CALLBACK"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "WALL_LIMIT"
            break
        await check_context()
        model_input = {"request": request.model_dump(exclude={"provider"}), "allowed_tools": sorted(allowed_tools),
                       "tool_results": results}
        try:
            _json_size(model_input, _MEMORY_BYTES)
            model_calls += 1
            raw = await _bounded(adapter.next_turn(model_input), min(remaining, limits.model_timeout_seconds))
            if isinstance(raw, str):
                if len(raw.encode("utf-8")) > _MODEL_BYTES:
                    raise ValueError("Model turn too large")
                raw = json.loads(raw)
            _json_size(raw, _MODEL_BYTES)
            turn = ModelTurn.model_validate(raw)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = "MODEL_TIMEOUT"
            break
        except ModelFailure as error:
            reason = error.reason_code
            break
        except (ValueError, TypeError, ValidationError, json.JSONDecodeError):
            reason = "MODEL_INVALID"
            break
        except Exception:
            reason = "MODEL_ERROR"
            break
        await check_context()
        if turn.finish:
            required = {
                "current_state": {"get_parking_state"} | ({"analyze_spatial_context"}
                                                          if "analyze_spatial_context" in allowed_tools else set()),
                "my_vehicle": {"get_my_vehicles", "get_parking_state"},
                "regulation": {"search_operating_knowledge"},
            }[request.goal]
            observed = {item["name"] for item in results}
            if turn.finish.status == "completed" and not results:
                reason = "READ_NOT_PERFORMED"
            elif turn.finish.status == "completed" and not required.issubset(observed):
                reason = "READ_INCOMPLETE"
            elif (turn.finish.status == "completed" and request.goal == "regulation"
                  and any(item["name"] == "search_operating_knowledge"
                          and isinstance(item["result"], dict)
                          and item["result"].get("status") != "matched" for item in results)):
                reason = "KNOWLEDGE_REQUIRED"
            else:
                status, reason = turn.finish.status, turn.finish.reason_code
                answer = turn.finish.answer
            break
        if model_calls == limits.max_model_calls:
            reason = "MODEL_LIMIT"
            break
        for call in turn.tool_calls or []:
            if tool_calls >= limits.max_tool_calls:
                reason = "TOOL_LIMIT"
                return await result()
            tool_calls += 1
            if call.call_id in seen:
                reason = "DUPLICATE_CALL_ID"
                return await result()
            seen.add(call.call_id)
            if call.name not in allowed_tools:
                reason = "TOOL_NOT_ALLOWED"
                return await result()
            if _has_forbidden_key(call.arguments):
                reason = "TOOL_INPUT_INVALID"
                return await result()
            try:
                _json_size(call.arguments, _MODEL_BYTES)
            except ValueError:
                reason = "TOOL_INPUT_INVALID"
                return await result()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "WALL_LIMIT"
                return await result()
            await check_context()
            try:
                value = await _bounded(call_tool(call.name, call.arguments), min(remaining, limits.tool_timeout_seconds))
                _json_size(value, _TOOL_BYTES)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                reason = "TOOL_TIMEOUT"
                return await result()
            except (ValueError, TypeError):
                reason = "TOOL_INVALID"
                return await result()
            except Exception:
                reason = "TOOL_ERROR"
                return await result()
            await check_context()
            try:
                _json_size(results + [{"call_id": call.call_id, "name": call.name, "result": value}], _MEMORY_BYTES)
            except ValueError:
                reason = "RESULT_LIMIT"
                return await result()
            results.append({"call_id": call.call_id, "name": call.name, "result": value})
    return await result()
