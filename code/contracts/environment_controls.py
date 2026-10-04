from typing import Literal

from pydantic import Field

from contracts.models import Contract


class DeviceFaultInput(Contract):
    expected_state_version: int = Field(ge=0, strict=True)
    channel: Literal["visual", "audio", "simulated_playback", "gate"]
    failed: bool = Field(strict=True)


class S2ReactionInput(Contract):
    expected_state_version: int = Field(ge=0, strict=True)
    mode: Literal["brake_on_alarm", "no_response"]
    delay_ms: int = Field(default=0, ge=0, le=3000, strict=True)
