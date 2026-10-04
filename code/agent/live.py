"""Opt-in read-only model comparisons with one persistent shared cost ledger."""
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent.budget import BudgetError, BudgetLedger
from agent.loop import ModelFailure
from contracts.budget import BudgetLimits, TokenPricing, TokenQuote
from contracts.models import Contract


class ProviderConfiguration(Contract):
    pricing: TokenPricing
    max_output_tokens: int = Field(default=1024, ge=256, le=4096, strict=True)
    timeout_seconds: float = Field(default=8, gt=0, le=10)


class LiveConfiguration(Contract):
    schema_version: Literal["live-read-v1"] = "live-read-v1"
    pricing_checked_on: str = Field(default="", max_length=10)
    pricing_notes: str = Field(default="", max_length=500)
    pricing_sources: list[str] = Field(default_factory=list, max_length=4)
    limits: BudgetLimits
    providers: dict[Literal["openai", "gemini"], ProviderConfiguration]
    primary_provider: Literal["openai", "gemini"] = "openai"
    fallback_provider: Literal["openai", "gemini"] = "gemini"

    @model_validator(mode="after")
    def bounded_comparison(self):
        if (not self.providers
                or (self.limits.total_krw is not None and self.limits.daily_krw is not None
                    and self.limits.daily_krw > self.limits.total_krw)):
            raise ValueError("Providers are required and daily limit cannot exceed total limit")
        if any(name != settings.pricing.provider for name, settings in self.providers.items()):
            raise ValueError("Provider and pricing names must match")
        if self.primary_provider == self.fallback_provider:
            raise ValueError("Primary and fallback providers must differ")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item.pricing.model)
               for item in self.providers.values()):
            raise ValueError("Model identifiers cannot include paths")
        return self

    @classmethod
    def read(cls, path):
        raw = Path(path).read_bytes()
        if len(raw) > 16_384:
            raise ValueError("Comparison configuration is too large")
        return cls.model_validate_json(raw)


class LiveModels:
    def __init__(self, configuration, ledger_path, *, client_factory=None):
        if client_factory is None:
            from agent.providers import ProviderClient
            client_factory = ProviderClient
        self.configuration = LiveConfiguration.model_validate(configuration)
        self.ledger = BudgetLedger(Path(ledger_path).resolve())
        self.client_factory = client_factory
        # A different budget policy cannot silently start a second admission rule.
        self.ledger.snapshot(self.configuration.limits, self.now())
        self.ledger.recover_dispatched()

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def client(self, provider):
        settings = self.configuration.providers.get(provider)
        if settings is None:
            raise ModelFailure("MODEL_NOT_CONFIGURED")
        return self.client_factory(provider, settings.pricing.model,
            settings.max_output_tokens, settings.timeout_seconds)

    def public_status(self):
        return {"mode": "live", "routing": {
            "primary_provider": self.configuration.primary_provider,
            "fallback_provider": self.configuration.fallback_provider,
            "fallback_policy": "pre_dispatch_configuration_only"}, "providers": [
            {"provider": name, "model_ref": settings.pricing.model,
             "available": self.client(name).credentials_ready()}
            for name, settings in self.configuration.providers.items()],
            "budget": self.ledger.snapshot(self.configuration.limits, self.now()).model_dump(),
            "cost_basis": "buffered_token_estimate_not_invoice"}

    def select_route(self, requested="auto"):
        """Choose once before admission; never retry a possibly billed request."""
        if requested != "auto":
            if not self.client(requested).credentials_ready():
                raise ModelFailure("MODEL_KEY_UNAVAILABLE")
            return {"provider": requested, "model_ref": self.configuration.providers[requested].pricing.model,
                    "routing_mode": "explicit", "fallback_reason": None}
        primary = self.configuration.primary_provider
        reason = None
        try:
            if not self.client(primary).credentials_ready():
                raise ModelFailure("MODEL_KEY_UNAVAILABLE")
        except ModelFailure as error:
            if error.reason_code not in {"MODEL_NOT_CONFIGURED", "MODEL_KEY_UNAVAILABLE"}:
                raise
            reason = error.reason_code
        selected = self.configuration.fallback_provider if reason else primary
        if not self.client(selected).credentials_ready():
            raise ModelFailure("MODEL_KEY_UNAVAILABLE")
        return {"provider": selected, "model_ref": self.configuration.providers[selected].pricing.model,
                "routing_mode": "auto", "fallback_reason": reason}

    def adapter(self, provider, check_context):
        client = self.client(provider)
        if not client.credentials_ready():
            raise ModelFailure("MODEL_KEY_UNAVAILABLE")
        return LiveReadAdapter(self, provider, client, check_context)


class LiveReadAdapter:
    def __init__(self, models, provider, client, check_context):
        self.models, self.provider, self.client = models, provider, client
        self.check_context = check_context
        self.settings = models.configuration.providers[provider]
        self.reservations = []
        self.input_tokens = self.output_tokens = 0

    async def next_turn(self, model_input):
        if not self.client.credentials_ready():
            raise ModelFailure("MODEL_KEY_UNAVAILABLE")
        quote = TokenQuote(request_key="model:" + uuid4().hex,
            input_tokens=self.client.input_token_bound(model_input),
            max_output_tokens=self.settings.max_output_tokens, pricing=self.settings.pricing)
        try:
            reservation = self.models.ledger.reserve(quote, self.models.configuration.limits, self.models.now())
        except BudgetError:
            raise ModelFailure("BUDGET_LIMIT") from None
        self.reservations.append(reservation)
        dispatched = False
        try:
            # Recheck authority/freshness after SQLite admission and immediately
            # before dispatch. A cancelled/unstarted call frees only its reservation.
            await self.check_context()
            try:
                self.reservations[-1] = self.models.ledger.mark_dispatched(quote.request_key)
            except BudgetError:
                raise ModelFailure("BUDGET_LIMIT") from None
            dispatched = True
            reply = await self.client.complete(model_input)
            if reply.input_tokens is None or reply.output_tokens is None:
                raise ModelFailure("MODEL_USAGE_UNKNOWN")
            reservation = self.models.ledger.settle(quote.request_key, reply.input_tokens,
                reply.output_tokens, self.models.now())
            self.reservations[-1] = reservation
            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
            if reply.input_tokens > quote.input_tokens or reply.output_tokens > quote.max_output_tokens:
                raise ModelFailure("MODEL_USAGE_EXCEEDS_QUOTE")
            if reply.error_code or reply.turn is None:
                raise ModelFailure("MODEL_OUTPUT_LIMIT" if reply.error_code == "MODEL_OUTPUT_LIMIT" else "MODEL_INVALID")
            return reply.turn
        except BaseException as error:
            # A sent timeout/cancellation/malformed usage may still be billed.
            # Preserve it across restarts; never retry or release by elapsed time.
            if self.reservations[-1].state != "settled":
                self.reservations[-1] = (self.models.ledger.mark_unknown(quote.request_key)
                    if dispatched else self.models.ledger.cancel_unstarted(quote.request_key))
            from agent.providers import ProviderError
            if isinstance(error, ProviderError):
                raise ModelFailure(error.code) from None
            raise

    def result_metadata(self):
        unresolved = {"reserved", "dispatched", "unknown"}
        unknown = any(item.state in unresolved for item in self.reservations)
        sent = any(item.state == "settled" for item in self.reservations)
        return {"mode": "live", "provider": self.provider,
            "model_ref": self.settings.pricing.model, "cost_actual_usd": None,
            "cost_estimated_krw": sum(item.actual_krw or 0 for item in self.reservations),
            "cost_pending_krw": sum(item.reserved_krw for item in self.reservations if item.state in unresolved),
            "usage_status": "unknown" if unknown else "known" if sent else "not_sent",
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cost_basis": "buffered_token_estimate_not_invoice"}
