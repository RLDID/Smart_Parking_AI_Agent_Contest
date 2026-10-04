"""Read-loop budgets and authority checks, without a paid model or live server."""

import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from agent.loop import LoopLimits, MockReadAdapter, run_read_loop
from contracts.agent_loop import AgentQuery


ALL = frozenset({"get_parking_state", "analyze_spatial_context", "get_my_vehicles",
                 "search_operating_knowledge"})


class Turns:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.inputs = []

    async def next_turn(self, model_input):
        self.inputs.append(model_input)
        return self.turns.pop(0)


def query(goal="current_state"):
    return AgentQuery(run_id="run-current", goal=goal,
                      query="  운영 규정  " if goal == "regulation" else "")


def call(call_id="one", name="get_parking_state", arguments=None):
    return {"call_id": call_id, "name": name, "arguments": arguments or {}}


def run(adapter=None, *, request=None, callback=None, context=None, allowed=ALL, limits=None):
    checks = []
    seen = []

    async def check_context():
        checks.append("checked")
        if context:
            return await context()

    async def call_tool(name, args):
        seen.append((name, args))
        return await callback(name, args) if callback else {"run_id": "run-current", "state": "observed"}

    result = asyncio.run(run_read_loop(request or query(), adapter or MockReadAdapter(), call_tool,
                                       check_context=check_context, allowed_tools=allowed,
                                       limits=limits))
    return result, checks, seen


def test_result_envelope_limit_is_bounded_even_when_partial_results_fit():
    async def large_result(name, arguments):
        return "x" * 32630
    adapter = Turns({"tool_calls": [call("a"), call("b")]},
                    {"finish": {"status": "completed", "reason_code": "READ_COMPLETED"}})
    result, _, _ = run(adapter, request=AgentQuery(run_id="r", goal="current_state"),
                      callback=large_result, allowed=frozenset({"get_parking_state"}))
    assert len(adapter.inputs) == 2
    assert len(json.dumps(adapter.inputs[1], ensure_ascii=False, separators=(",", ":")).encode()) <= 65536
    assert result["model_calls"] == 2 and result["tool_calls"] == 2
    assert result["status"] == "needs_review" and result["reason_code"] == "RESULT_LIMIT"
    assert result["tool_results"] == []
    assert len(json.dumps(result).encode()) <= 65536


def test_contract_and_trusted_limits_reject_authority_and_unbounded_values():
    with pytest.raises(ValidationError):
        AgentQuery(run_id="r", goal="regulation", query=" \t")
    with pytest.raises(ValidationError):
        AgentQuery(run_id="r", goal="current_state", role="owner")
    assert query("regulation").query == "운영 규정"
    for kwargs in ({"max_model_calls": 5}, {"max_tool_calls": 17}, {"wall_seconds": 31},
                   {"max_tool_calls": 0}, {"model_timeout_seconds": float("nan")},
                   {"tool_timeout_seconds": True}):
        with pytest.raises(ValueError):
            LoopLimits(**kwargs)


def test_mock_reads_current_state_and_scoped_vehicle_with_results_in_next_turn():
    for goal, names in (("current_state", ["get_parking_state", "analyze_spatial_context"]),
                        ("my_vehicle", ["get_my_vehicles", "get_parking_state"])):
        adapter = MockReadAdapter()
        result, checks, seen = run(adapter, request=query(goal))
        assert result["status"] == "completed"
        assert result["mode"] == "mock" and result["cost_actual_usd"] == 0
        assert result["model_calls"] == 2 and result["tool_calls"] == 2
        assert [n for n, _ in seen] == names
        assert len(checks) >= 2 * (result["model_calls"] + result["tool_calls"]) + 1


def test_driver_current_state_uses_only_scoped_state_when_spatial_is_not_allowed():
    result, _, seen = run(request=query("current_state"),
                          allowed=frozenset({"get_parking_state"}))
    assert result["status"] == "completed"
    assert [name for name, _ in seen] == ["get_parking_state"]


def test_regulation_requires_matched_current_result_and_only_forwards_query():
    async def matched(name, args):
        assert name == "search_operating_knowledge"
        assert args == {"query": "운영 규정"}
        return {"status": "matched", "references": ["public-ref"]}

    result, _, seen = run(request=query("regulation"), callback=matched)
    assert result["status"] == "completed" and len(seen) == 1

    async def unavailable(name, args):
        return {"status": "unavailable", "references": []}

    result, _, _ = run(request=query("regulation"), callback=unavailable)
    assert (result["status"], result["reason_code"]) == ("needs_review", "KNOWLEDGE_UNAVAILABLE")
    result, _, seen = run(request=query("regulation"), allowed=frozenset())
    assert result["reason_code"] == "TOOL_NOT_ALLOWED" and seen == []


@pytest.mark.parametrize("bad,reason", [
    ("not-json", "MODEL_INVALID"),
    ({"tool_calls": [call(name="notify_vehicle_user")]}, "TOOL_NOT_ALLOWED"),
    ({"tool_calls": [call(arguments={"role": "owner"})]}, "TOOL_INPUT_INVALID"),
    ({"tool_calls": [call(arguments={"run_id": "other"})]}, "TOOL_INPUT_INVALID"),
    ({"tool_calls": [call(), call()]}, "DUPLICATE_CALL_ID"),
    ({"finish": {"status": "completed", "reason_code": "DONE"}}, "READ_NOT_PERFORMED"),
    ({"tool_calls": [call()], "finish": {"status": "completed", "reason_code": "DONE"}}, "MODEL_INVALID"),
    ({"tool_calls": [call(arguments={"x": float("nan")})]}, "MODEL_INVALID"),
])
def test_invalid_turns_fail_closed(bad, reason):
    result, _, seen = run(Turns(bad))
    assert result["status"] == "needs_review" and result["reason_code"] == reason
    assert len(seen) <= 1


def test_call_ids_unique_across_turns_and_attempts_consume_budget():
    adapter = Turns({"tool_calls": [call()]}, {"tool_calls": [call()]})
    result, _, seen = run(adapter)
    assert result["reason_code"] == "DUPLICATE_CALL_ID" and result["tool_calls"] == 2
    assert len(seen) == 1

    adapter = Turns({"tool_calls": [call("1"), call("2")]})
    result, _, seen = run(adapter, limits=LoopLimits(max_tool_calls=1))
    assert result["reason_code"] == "TOOL_LIMIT" and len(seen) == 1


def test_second_turn_sees_actual_tool_result_but_cannot_complete_partial_goal():
    adapter = Turns({"tool_calls": [call()]},
                    {"finish": {"status": "completed", "reason_code": "READ_COMPLETED"}})
    result, _, _ = run(adapter)
    assert adapter.inputs[1]["tool_results"][0]["result"]["state"] == "observed"
    assert result["status"] == "needs_review" and result["reason_code"] == "READ_INCOMPLETE"


def test_fourth_model_turn_cannot_request_fifth_turn_or_tools():
    adapter = Turns(*[{"tool_calls": [call(str(n))]} for n in range(4)])
    result, _, seen = run(adapter)
    assert result["reason_code"] == "MODEL_LIMIT"
    assert result["model_calls"] == 4 and result["tool_calls"] == 3 and len(seen) == 3


def test_sixteen_tool_attempts_are_the_hard_ceiling_without_retry():
    first = {"tool_calls": [call(str(index)) for index in range(1, 9)]}
    second = {"tool_calls": [call(str(index)) for index in range(9, 17)]}
    third = {"tool_calls": [call("17")]}
    result, _, seen = run(Turns(first, second, third))
    assert result["reason_code"] == "TOOL_LIMIT"
    assert result["tool_calls"] == 16 and len(seen) == 16


def test_model_or_tool_error_and_size_limits_fail_closed():
    class BrokenModel:
        async def next_turn(self, model_input):
            raise RuntimeError("synthetic model failure")

    result, _, seen = run(BrokenModel())
    assert result["reason_code"] == "MODEL_ERROR" and seen == []

    async def broken_tool(name, args):
        raise RuntimeError("synthetic tool failure")

    result, _, _ = run(Turns({"tool_calls": [call()]}), callback=broken_tool)
    assert result["status"] == "needs_review" and result["reason_code"] == "TOOL_ERROR"

    result, _, seen = run(Turns(json.dumps({"finish": {"status": "completed",
                                                       "reason_code": "READ_COMPLETED",
                                                       "padding": "x" * 17000}})))
    assert result["reason_code"] == "MODEL_INVALID" and seen == []

    async def oversized_tool(name, args):
        return {"payload": "x" * 33000}

    result, _, _ = run(Turns({"tool_calls": [call()]}), callback=oversized_tool)
    assert result["reason_code"] == "TOOL_INVALID" and result["tool_results"] == []


def test_context_change_after_model_and_tool_raises_without_private_return():
    class Revoked(Exception):
        pass

    model = Turns({"tool_calls": [call()]})
    n = 0

    async def after_model():
        nonlocal n
        n += 1
        if n == 3:
            raise Revoked

    with pytest.raises(Revoked):
        run(model, context=after_model)
    assert model.inputs

    async def after_tool():
        nonlocal n
        n += 1
        if n == 5:
            raise Revoked

    n = 0
    with pytest.raises(Revoked):
        run(Turns({"tool_calls": [call()]}), context=after_tool)


def test_model_and_tool_timeout_do_not_accept_late_results():
    async def scenario():
        released = asyncio.Event()
        called = []

        class SlowModel:
            async def next_turn(self, model_input):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    await released.wait()
                return {"tool_calls": [call()]}

        async def tool(name, args):
            called.append(name)
            return {}

        async def context():
            return None

        start = time.monotonic()
        result = await run_read_loop(query(), SlowModel(), tool, check_context=context,
                                     allowed_tools=ALL,
                                     limits=LoopLimits(model_timeout_seconds=.01, wall_seconds=.2))
        assert time.monotonic() - start < .15
        assert result["reason_code"] == "MODEL_TIMEOUT" and not called
        # An ignored cancellation blocks a new callback while the old one is pending.
        again = await run_read_loop(query(), MockReadAdapter(), tool, check_context=context,
                                    allowed_tools=ALL)
        assert again["reason_code"] == "PENDING_CALLBACK"
        released.set()
        await asyncio.sleep(.01)
        ready = await run_read_loop(query(), MockReadAdapter(), tool, check_context=context,
                                    allowed_tools=ALL)
        assert ready["status"] == "completed"

        tool_released = asyncio.Event()

        async def slow_tool(name, args):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await tool_released.wait()
            return {"late": "PRIVATE"}

        timed = await run_read_loop(query(), MockReadAdapter(), slow_tool, check_context=context,
                                    allowed_tools=ALL,
                                    limits=LoopLimits(tool_timeout_seconds=.01, wall_seconds=.2))
        assert timed["reason_code"] == "TOOL_TIMEOUT" and timed["tool_results"] == []
        tool_released.set()
        await asyncio.sleep(.01)

    asyncio.run(scenario())


def test_cancellation_propagates_and_does_not_dispatch_later_tools():
    async def scenario():
        began = asyncio.Event()
        calls = []

        async def tool(name, args):
            calls.append(name)
            began.set()
            await asyncio.sleep(60)

        async def context():
            return None

        task = asyncio.create_task(run_read_loop(query(), MockReadAdapter(), tool,
                                                 check_context=context, allowed_tools=ALL))
        await began.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert len(calls) == 1

    asyncio.run(scenario())


def test_final_context_recheck_that_outlasts_wall_limit_cannot_complete():
    async def scenario():
        checks = 0

        async def context():
            nonlocal checks
            checks += 1
            if checks == 7:
                await asyncio.sleep(.03)

        async def tool(name, args):
            return {"state": "current"}

        result = await run_read_loop(query(), MockReadAdapter(), tool, check_context=context,
                                     allowed_tools=frozenset({"get_parking_state"}),
                                     limits=LoopLimits(wall_seconds=.02))
        assert result["status"] == "needs_review"
        assert result["reason_code"] == "WALL_LIMIT"

    asyncio.run(scenario())
