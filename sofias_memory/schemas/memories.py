"""Public schemas for Native Cognitive Memory Create/Get (SM-1002, ADR-0016,
Feature Contract v0.7.0 Native Cognitive Memory).

Every normalization/validation rule here delegates to the SM-1001 domain
primitives in ``sofias_memory.domain`` -- this module never re-implements
content/scope/confidence/provenance grammar. Pydantic ``Field`` metadata is
kept to plain type shape only (never a duplicated length/regex constraint),
so the domain module remains the single source of truth for every limit.

``MemoryItemResult``/``MemoryProvenanceResult`` are deliberately
lifecycle-safe: every cognitive-content field is nullable so the exact same
response shape can represent a future SUPERSEDED/FORGOTTEN tombstone
(SM-1004) without a breaking redesign, even though SM-1002 only ever
produces ACTIVE items.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import field_validator as pydantic_field_validator

from sofias_memory.domain import (
    CognitiveMemoryLifecycle,
    CognitiveMemoryOriginKind,
    CognitiveMemoryType,
    normalize_cognitive_memory_content,
    normalize_cognitive_memory_scope,
    normalize_cognitive_memory_timestamp,
    validate_cognitive_memory_confidence,
    validate_cognitive_memory_external_ref,
    validate_cognitive_memory_provenance_origin_requirements,
    validate_cognitive_memory_source_system,
    validate_cognitive_memory_validity_window,
)


class MemoryProvenanceCreateRequest(BaseModel):
    """Caller-supplied cognitive provenance for one Create (Feature Contract
    SS 7). External UUID/reference fields are opaque values, never
    cross-database foreign keys."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str = Field(description="Canonical system slug, e.g. 'sofias-assistant'.")
    conversation_uuid: UUID | None = Field(default=None, description="Opaque external reference.")
    turn_uuid: UUID | None = Field(default=None, description="Opaque external reference.")
    task_uuid: UUID | None = Field(default=None, description="Opaque external reference.")
    confirmation_ref: str | None = Field(
        default=None,
        description="Opaque external evidence that a confirmation workflow already occurred.",
    )
    source_ref: str | None = Field(default=None, description="Opaque external reference.")
    observed_at: datetime | None = Field(
        default=None, description="UTC timestamp; must be timezone-aware."
    )

    @pydantic_field_validator("source_system")
    @classmethod
    def _validate_source_system(cls, value: str) -> str:
        return validate_cognitive_memory_source_system(value)

    @pydantic_field_validator("confirmation_ref", "source_ref")
    @classmethod
    def _validate_external_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_cognitive_memory_external_ref(value)

    @pydantic_field_validator("observed_at")
    @classmethod
    def _normalize_observed_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return normalize_cognitive_memory_timestamp(value)


class MemoryCreateRequest(BaseModel):
    """``POST /api/v1/memories`` request body (Feature Contract SS 12)."""

    model_config = ConfigDict(extra="forbid")

    memory_type: CognitiveMemoryType
    scope: str = Field(description="'global' or 'project:<key>'.")
    content: str = Field(description="Cognitive content, 1..16384 Unicode characters.")
    confidence: float | None = Field(
        default=None,
        description="Nullable, [0,1]; required when origin_kind is 'inferred'.",
    )
    valid_from: datetime | None = Field(default=None, description="Inclusive; UTC.")
    valid_until: datetime | None = Field(default=None, description="Exclusive; UTC.")
    provenance: MemoryProvenanceCreateRequest

    @pydantic_field_validator("scope")
    @classmethod
    def _normalize_scope(cls, value: str) -> str:
        return normalize_cognitive_memory_scope(value)

    @pydantic_field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return normalize_cognitive_memory_content(value)

    @pydantic_field_validator("valid_from", "valid_until")
    @classmethod
    def _normalize_validity_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return normalize_cognitive_memory_timestamp(value)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> MemoryCreateRequest:
        validate_cognitive_memory_confidence(
            self.confidence, origin_kind=self.provenance.origin_kind
        )
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=self.provenance.origin_kind,
            turn_uuid=self.provenance.turn_uuid,
            task_uuid=self.provenance.task_uuid,
            source_ref=self.provenance.source_ref,
            observed_at=self.provenance.observed_at,
        )
        validate_cognitive_memory_validity_window(self.valid_from, self.valid_until)
        return self


class MemoryProvenanceResult(BaseModel):
    """Public provenance shape -- lifecycle-safe: every field beyond
    ``origin_kind``/``source_system`` is nullable so a future FORGOTTEN scrub
    returns the same shape with those fields ``null``, never a different
    response schema."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str
    conversation_uuid: UUID | None
    turn_uuid: UUID | None
    task_uuid: UUID | None
    confirmation_ref: str | None
    source_ref: str | None
    observed_at: datetime | None


class MemoryItemResult(BaseModel):
    """Public ``MemoryItem`` shape -- already compatible with future
    SUPERSEDED/FORGOTTEN states: every cognitive-content field is nullable.
    Embedding is never exposed."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID
    memory_type: CognitiveMemoryType
    scope: str | None
    content: str | None
    lifecycle: CognitiveMemoryLifecycle
    confidence: float | None
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime
    superseded_at: datetime | None
    superseded_by: UUID | None
    forgotten_at: datetime | None
    provenance: MemoryProvenanceResult
