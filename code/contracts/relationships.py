"""Synthetic relationship management requests. No real identity data is accepted."""
from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Contract, Identifier


Alias = str
Reason = str


class CustomerCreate(Contract):
    display_alias: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class CustomerChange(Contract):
    expected_version: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=200)
    display_alias: str | None = Field(default=None, min_length=1, max_length=64)
    active: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.display_alias is None and self.active is None:
            raise ValueError("At least one change is required")
        return self


class VehicleCreate(Contract):
    display_alias: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class VehicleChange(CustomerChange):
    pass


class VehicleUserChange(Contract):
    expected_version: int = Field(ge=0, strict=True)
    user_id: Identifier | None = None
    reason: str = Field(min_length=1, max_length=200)


class ObjectMappingChange(Contract):
    run_id: Identifier
    expected_version: int = Field(ge=0, strict=True)
    registered_vehicle_id: Identifier | None = None
    mapping_status: Literal["verified", "uncertain", "unmapped"]
    mapping_source: Literal["demo_config", "reviewed"]
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def verified_requires_vehicle(self):
        if self.mapping_status == "verified" and (not self.registered_vehicle_id or self.mapping_source != "reviewed"):
            raise ValueError("Verified management mapping requires a reviewed vehicle")
        if self.mapping_status == "unmapped" and self.registered_vehicle_id is not None:
            raise ValueError("Unmapped relation cannot name a vehicle")
        return self


class PersonMappingChange(Contract):
    run_id: Identifier
    expected_version: int = Field(ge=0, strict=True)
    user_id: Identifier | None = None
    status: Literal["proposed", "uncertain", "verified", "unmapped"]
    source: Literal["demo_config", "reviewed"]
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def confirmed_requires_review(self):
        if self.status == "verified" and (self.source != "reviewed" or not self.user_id):
            raise ValueError("Verified person relation requires reviewed customer")
        if self.status == "unmapped" and self.user_id is not None:
            raise ValueError("Unmapped relation cannot name a customer")
        return self
