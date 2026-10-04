"""Explicit test-only consumer policy; does not describe real customer behavior."""
from typing import Literal

from pydantic import Field

from contracts.models import Contract


class SyntheticUserPolicy(Contract):
    mode: Literal["manual", "will_move", "acknowledged", "cannot_move", "question", "silent"] = "manual"
    response_delay_ms: int = Field(default=0, ge=0, le=60000, strict=True)
    movement_delay_ms: int = Field(default=0, ge=0, le=60000, strict=True)


class SyntheticUserInput(SyntheticUserPolicy):
    expected_state_version: int = Field(ge=0, strict=True)
