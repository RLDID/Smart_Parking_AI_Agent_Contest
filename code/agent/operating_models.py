"""Bounded live recommendations; execution authority stays in the business service."""
import asyncio
import json
import time

from agent.loop import ModelFailure


_PRIVATE_KEYS = {"actors", "seed", "fixture_ref", "expected", "future_path", "password",
                 "csrf", "authorization", "api_key", "token", "internal_state"}


def public_context(value):
    count = 0
    def visit(item, depth):
        nonlocal count
        count += 1
        if depth > 32 or count > 10000:
            raise ModelFailure("MODEL_CONTEXT_REJECTED")
        if isinstance(item, dict):
            if _PRIVATE_KEYS & {str(key).lower() for key in item}:
                raise ModelFailure("MODEL_CONTEXT_REJECTED")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
    visit(value, 0)
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 60000:
            raise ModelFailure("MODEL_CONTEXT_REJECTED")
    except (ValueError, TypeError, RecursionError):
        raise ModelFailure("MODEL_CONTEXT_REJECTED") from None


class LiveOperationsAdapter:
    def __init__(self, models, check_context):
        self.models = models
        self.route = models.select_route("auto")
        self.adapter = models.adapter(self.route["provider"], check_context)
        self.created_at = time.monotonic()
        self.model_calls = 0
        self.lock = asyncio.Lock()

    async def decide(self, context):
        public_context(context)
        model_context = {key: value for key, value in context.items() if key != "scenario"}
        async with self.lock:
            remaining = 28 - (time.monotonic() - self.created_at)
            if remaining <= 0 or self.model_calls >= 4:
                raise ModelFailure("MODEL_CALL_LIMIT")
            self.model_calls += 1
            timeout = min(remaining, self.adapter.settings.timeout_seconds)
            try:
                turn = await asyncio.wait_for(self.adapter.next_turn({
                    "request": {"goal": "operations", "context": model_context},
                    "allowed_tools": [], "tool_results": []}), timeout)
            except TimeoutError:
                raise ModelFailure("MODEL_TIMEOUT") from None
            finish = turn.get("finish") if isinstance(turn, dict) else None
            if not finish or finish.get("status") != "completed":
                raise ModelFailure("MODEL_INVALID")
            answer = finish.get("answer")
            if not isinstance(answer, str) or len(answer.encode()) > 4096:
                raise ModelFailure("MODEL_INVALID")
            try:
                decision = json.loads(answer)
            except (ValueError, TypeError, RecursionError):
                raise ModelFailure("MODEL_INVALID") from None
            if not isinstance(decision, dict):
                raise ModelFailure("MODEL_INVALID")
            if context.get("scenario") is not None:
                if "scenario" in decision and decision["scenario"] != context["scenario"]:
                    raise ModelFailure("MODEL_INVALID")
                decision["scenario"] = context["scenario"]
            # The caller validates the canonical AutonomousDecision before any write.
            return decision

    def result_metadata(self):
        return self.adapter.result_metadata() | self.route | {"model_calls": self.model_calls}
