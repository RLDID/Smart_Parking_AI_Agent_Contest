"""Explicit, key-free pricing and cost limits for an eventual model adapter."""

from typing import Literal

from pydantic import Field

from contracts.models import Contract


_RATE = r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$"
_KEY = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class BudgetLimits(Contract):
    total_krw: int | None = Field(default=None, gt=0, le=10_000_000, strict=True)
    daily_krw: int | None = Field(default=None, gt=0, le=10_000_000, strict=True)


class TokenPricing(Contract):
    provider: str = Field(min_length=1, max_length=64, pattern=_KEY)
    model: str = Field(min_length=1, max_length=128, pattern=_KEY)
    input_krw_per_million: str = Field(pattern=_RATE)
    output_krw_per_million: str = Field(pattern=_RATE)


class TokenQuote(Contract):
    request_key: str = Field(min_length=1, max_length=128, pattern=_KEY)
    input_tokens: int = Field(ge=1, le=2_000_000, strict=True)
    # Includes all billable output tokens, including reasoning tokens.
    max_output_tokens: int = Field(ge=1, le=2_000_000, strict=True)
    pricing: TokenPricing


class BudgetReservation(Contract):
    request_key: str
    provider: str
    model: str
    day_kst: str
    state: Literal["reserved", "dispatched", "unknown", "settled", "cancelled"]
    reserved_krw: int = Field(ge=0)
    actual_krw: int | None = Field(default=None, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)


class BudgetSnapshot(Contract):
    day_kst: str
    total_limit_krw: int | None
    daily_limit_krw: int | None
    total_spent_krw: int
    total_pending_krw: int
    daily_spent_krw: int
    daily_pending_krw: int
    unknown_count: int
