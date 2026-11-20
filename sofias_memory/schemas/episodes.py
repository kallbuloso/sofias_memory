"""Public schemas for contextual Episodes (SM-CTX-001, Contract v1
Contextual Project Memory SS 10/14-22/64).

Every normalization rule delegates to ``sofias_memory.domain``. Scope,
``context_key`` and ``kind`` failures are raised as Pydantic custom errors
(``invalid_scope``/``invalid_context_key``/``invalid_episode_kind``) so the
shared validation handler can return the stable Contract codes
``INVALID_SCOPE``/``INVALID_CONTEXT_KEY``/``INVALID_EPISODE_KIND`` (422).

``support``/Evidence belongs to SM-CTX-004 and is not accepted here
(``extra="forbid"``). ``EpisodeResult`` is lifecycle-safe: every semantic
field is nullable so a FORGOTTEN tombstone uses the same shape with those
fields ``null``. The embedding is never exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic import field_validator as pydantic_field_validator
from pydantic_core import PydanticCustomError

from sofias_memory.domain import (
    CognitiveMemoryOriginKind,
    EpisodeKind,
    EpisodeLifecycle,
    InvalidContextKeyError,
    InvalidContextualScopeError,
    InvalidEpisodeKindError,
    normalize_contextual_scope,
    normalize_episode_occurred_at,
    normalize_episode_text,
    parse_episode_kind,
    validate_cognitive_memory_confidence,
    validate_context_key,
)
from sofias_memory.schemas.common import utc_now

EPISODE_PAGE_DEFAULT_LIMIT = 50
EPISODE_PAGE_MAX_LIMIT = 100


def _scope(value: str) -> str:
    try:
        return normalize_contextual_scope(value)
    except InvalidContextualScopeError as error:
        raise PydanticCustomError("invalid_scope", "{reason}", {"reason": error.reason}) from error


def _context_key(value: str) -> str:
    try:
        return validate_context_key(value)
    except InvalidContextKeyError as error:
        raise PydanticCustomError(
            "invalid_context_key", "{reason}", {"reason": error.reason}
        ) from error


def _kind(value: object) -> EpisodeKind:
    try:
        return parse_episode_kind(value)
    except InvalidEpisodeKindError as error:
        raise PydanticCustomError(
            "invalid_episode_kind", "{reason}", {"reason": error.reason}
        ) from error


def _kind_or_none(value: object) -> EpisodeKind | None:
    return None if value is None else _kind(value)


ContextualScope = Annotated[str, AfterValidator(_scope)]
"""Canonical ``project:<key>`` (Cognitive Memory grammar, ``global`` rejected)."""

ContextKey = Annotated[str, AfterValidator(_context_key)]
"""Contract v1 SS 9.1 grammar; exact comparison."""

EpisodeKindValue = Annotated[EpisodeKind, BeforeValidator(_kind)]
EpisodeKindFilter = Annotated[EpisodeKind | None, BeforeValidator(_kind_or_none)]


def _optional_text(value: str | None, field: str) -> str | None:
    return None if value is None else normalize_episode_text(value, field=field)


class EpisodeProvenanceCreateRequest(BaseModel):
    """Caller-supplied Episode provenance (Contract v1 SS 10). External refs
    are opaque values -- never foreign keys, never Provider Session IDs."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str = Field(description="Required, non-blank system identifier.")
    conversation_ref: str | None = Field(default=None, description="Opaque external reference.")
    turn_ref: str | None = Field(default=None, description="Opaque external reference.")

    @pydantic_field_validator("source_system")
    @classmethod
    def _normalize_source_system(cls, value: str) -> str:
        return normalize_episode_text(value, field="source_system")

    @pydantic_field_validator("conversation_ref")
    @classmethod
    def _normalize_conversation_ref(cls, value: str | None) -> str | None:
        return _optional_text(value, "conversation_ref")

    @pydantic_field_validator("turn_ref")
    @classmethod
    def _normalize_turn_ref(cls, value: str | None) -> str | None:
        return _optional_text(value, "turn_ref")


class EpisodeCreateRequest(BaseModel):
    """``POST /api/v1/episodes`` request body (Contract v1 SS 17.1)."""

    model_config = ConfigDict(extra="forbid")

    scope: ContextualScope = Field(description="Canonical 'project:<key>'. 'global' is rejected.")
    context_key: ContextKey | None = Field(
        default=None,
        description=(
            "Optional. Omitted/null means a project-wide Episode -- never defaulted to 'main'."
        ),
    )
    kind: EpisodeKindValue
    summary: str = Field(description="Required, non-blank.")
    situation: str | None = Field(default=None, description="Optional, non-blank when present.")
    reason: str | None = Field(default=None, description="Optional, non-blank when present.")
    outcome: str | None = Field(default=None, description="Optional, non-blank when present.")
    occurred_at: datetime = Field(
        description=(
            "When it happened (caller-supplied, timezone-aware, normalized to UTC). May be in "
            "the past; must not be in the future beyond a small clock-skew tolerance. "
            "Independent of the server-generated created_at."
        )
    )
    confidence: float | None = Field(
        default=None, description="Nullable, [0,1]; required when origin_kind is 'inferred'."
    )
    provenance: EpisodeProvenanceCreateRequest

    @pydantic_field_validator("summary")
    @classmethod
    def _normalize_summary(cls, value: str) -> str:
        return normalize_episode_text(value, field="summary")

    @pydantic_field_validator("situation")
    @classmethod
    def _normalize_situation(cls, value: str | None) -> str | None:
        return _optional_text(value, "situation")

    @pydantic_field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str | None) -> str | None:
        return _optional_text(value, "reason")

    @pydantic_field_validator("outcome")
    @classmethod
    def _normalize_outcome(cls, value: str | None) -> str | None:
        return _optional_text(value, "outcome")

    @pydantic_field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        return normalize_episode_occurred_at(value, now=utc_now())

    @model_validator(mode="after")
    def _validate_confidence(self) -> EpisodeCreateRequest:
        validate_cognitive_memory_confidence(
            self.confidence, origin_kind=self.provenance.origin_kind
        )
        return self


class EpisodeInvalidateRequest(BaseModel):
    """``POST /api/v1/episodes/{episode_id}/invalidate`` body (Contract v1 SS 20)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(description="Required, non-blank; persisted for audit.")
    replacement_episode_id: UUID | None = Field(
        default=None,
        description=(
            "Optional existing, non-forgotten Episode of the same Project Scope. Never created "
            "automatically."
        ),
    )

    @pydantic_field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return normalize_episode_text(value, field="reason")


class EpisodeProvenanceResult(BaseModel):
    """Public provenance shape; external refs are ``null`` on a tombstone."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str
    conversation_ref: str | None
    turn_ref: str | None


class EpisodeResult(BaseModel):
    """Public Episode shape (Contract v1 SS 14/18). Lifecycle-safe: on a
    FORGOTTEN tombstone every semantic field is ``null``."""

    model_config = ConfigDict(extra="forbid")

    episode_id: UUID
    scope: str | None
    context_key: str | None
    kind: EpisodeKind | None
    summary: str | None
    situation: str | None
    reason: str | None
    outcome: str | None
    occurred_at: datetime | None
    lifecycle: EpisodeLifecycle
    confidence: float | None
    provenance: EpisodeProvenanceResult
    created_at: datetime
    invalidated_at: datetime | None
    forgotten_at: datetime | None


class EpisodeListResult(BaseModel):
    """Bounded, keyset-paginated listing (Contract v1 SS 19/64): ordered
    ``occurred_at DESC, episode_id ASC``. ``next_cursor`` is opaque and
    ``null`` on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[EpisodeResult]
    limit: int
    next_cursor: str | None
