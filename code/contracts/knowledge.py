from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.models import Contract, Identifier, UtcTimestamp

Topic = Literal["parking_order", "entry_exit", "announcement", "user_guidance"]
Role = Literal["owner", "driver", "test_operator"]

def canonical_time(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="microseconds").replace("+00:00", "Z")


class KnowledgeQuery(Contract):
    facility_id: Identifier
    run_id: Identifier
    query: str = Field(min_length=1, max_length=500)
    topic: Topic | None = None
    zone_id: Identifier | None = None

    @field_validator("query", mode="before")
    @classmethod
    def trimmed_query(cls, value):
        if not isinstance(value, str):
            raise ValueError("String query required")
        value = value.strip()
        if not value:
            raise ValueError("Nonempty query required")
        return value


class PolicyQuery(Contract):
    facility_id: Identifier
    zone_id: Identifier | None = None
    policy_version: int | None = Field(default=None, ge=1, strict=True)


class KnowledgeRequirement(Contract):
    tool_name: Identifier
    purpose: Identifier
    required_topics: list[Topic]


class ExecutionRules(Contract):
    # Adopted synthetic connection-test values, not real facility policy.
    allowed_tools: list[Identifier] = Field(min_length=1)
    delivery_max_attempts: int = Field(ge=1, le=2, strict=True)
    delivery_timeout_wall_ms: int = Field(ge=1, le=10000, strict=True)
    contact_max_sequence: int = Field(ge=1, le=2, strict=True)
    contact_interval_wall_ms: int = Field(ge=1, strict=True)
    response_timeout_wall_ms: int = Field(ge=1, strict=True)
    overall_timeout_wall_ms: int = Field(ge=1, strict=True)
    spatial_followup_sim_ms: int = Field(ge=1, strict=True)
    followup_max_attempts: int = Field(ge=1, le=3, strict=True)


class OperatingPolicy(Contract):
    facility_id: Identifier
    policy_version: int = Field(ge=1, strict=True)
    knowledge_release_id: Identifier
    effective_at: UtcTimestamp
    retired_at: UtcTimestamp | None = None
    knowledge_requirements: list[KnowledgeRequirement] = Field(min_length=1)
    mode: Literal["synthetic_demo"] = "synthetic_demo"
    execution_rules: ExecutionRules | None = None

    _times = field_validator("effective_at", "retired_at")(canonical_time)

    @model_validator(mode="after")
    def valid_policy(self):
        if self.retired_at is not None and self.retired_at <= self.effective_at:
            raise ValueError("Empty policy interval")
        pairs = [(r.tool_name, r.purpose) for r in self.knowledge_requirements]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Ambiguous action-purpose mapping")
        return self


class ManualChunk(Contract):
    reference_id: Identifier
    section: Identifier
    topic: Topic
    procedure_group_id: Identifier
    content: str = Field(min_length=1, max_length=20000)


class Manual(Contract):
    chunks: list[ManualChunk] = Field(min_length=1, max_length=100)


class ManifestDocument(Contract):
    document_id: Identifier
    document_version: Identifier
    facility_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    source_ref: Identifier
    file: str
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_status: Literal["draft", "approved", "withdrawn"]
    allowed_roles: list[Role] = Field(min_length=1)
    effective_at: UtcTimestamp
    retired_at: UtcTimestamp | None = None
    reviewed_conflict: bool = False

    _times = field_validator("effective_at", "retired_at")(canonical_time)

    @model_validator(mode="after")
    def valid_document(self):
        if self.retired_at is not None and self.retired_at <= self.effective_at:
            raise ValueError("Empty document interval")
        return self


class KnowledgeManifest(Contract):
    schema_version: Literal["0.1-draft"] = "0.1-draft"
    facility_id: Identifier
    knowledge_release_id: Identifier
    index_version: Literal["keyword-v1"] = "keyword-v1"
    mode: Literal["synthetic_demo"]
    documents: list[ManifestDocument] = Field(min_length=1, max_length=20)
    policy: OperatingPolicy


class KnowledgeReference(Contract):
    reference_id: Identifier
    document_id: Identifier
    document_version: Identifier
    title: str
    section: str
    topic: Topic
    procedure_group_id: Identifier
    excerpt: str
    content_digest: str
    chunk_digest: str
    effective_at: UtcTimestamp
    retired_at: UtcTimestamp | None = None


class KnowledgeResult(Contract):
    schema_version: Literal["0.1-draft"] = "0.1-draft"
    retrieval_id: Identifier
    facility_id: Identifier
    run_id: Identifier
    status: Literal["matched", "no_match", "conflict", "unavailable"]
    policy_version: int | None = Field(default=None, ge=1, strict=True)
    knowledge_release_id: Identifier | None = None
    evaluated_at: UtcTimestamp
    retrieved_at: UtcTimestamp
    index_version: str | None = None
    references: list[KnowledgeReference] = Field(default_factory=list, max_length=100)
    reason_code: str | None = None

    @model_validator(mode="after")
    def status_matches_evidence(self):
        if (self.status == "matched") != bool(self.references):
            raise ValueError("Only matched results carry evidence")
        if self.status == "matched" and (not self.policy_version or not self.knowledge_release_id or not self.index_version):
            raise ValueError("Matched result requires policy and release")
        return self


class KnowledgeEvidence(Contract):
    retrieval_id: Identifier
    reference_ids: list[Identifier] = Field(min_length=1, max_length=100)

    @field_validator("reference_ids")
    @classmethod
    def unique_references(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate evidence reference")
        return value
