"""Bounded read-only Agent loop contracts; authority stays on the server."""

from typing import Any, Literal

from pydantic import Field, model_validator

from contracts.models import Contract, Identifier


class AgentQuery(Contract):
    run_id: Identifier
    goal: Literal["current_state", "my_vehicle", "regulation"]
    query: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def regulation_needs_query(self):
        self.query = self.query.strip()
        if self.goal == "regulation" and not self.query:
            raise ValueError("A regulation query is required")
        return self


class ToolCall(Contract):
    call_id: Identifier
    name: Identifier
    arguments: dict[str, Any]


class LiveAgentQuery(AgentQuery):
    # Only a server-configured comparison provider may be selected by HTTP.
    # Model, price, credentials, and budget remain outside user/model input.
    provider: Literal["auto", "openai", "gemini"] = "auto"


class Finish(Contract):
    status: Literal["completed", "needs_review"]
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    answer: str = Field(default="", max_length=2000)


class ModelTurn(Contract):
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=8)
    finish: Finish | None = None

    @model_validator(mode="after")
    def exactly_one_action(self):
        if (self.tool_calls is None) == (self.finish is None):
            raise ValueError("Exactly one of tool_calls or finish is required")
        if self.tool_calls is not None and not self.tool_calls:
            raise ValueError("Empty tool_calls are not a turn")
        return self
