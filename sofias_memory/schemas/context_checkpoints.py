"""Public schemas for ContextCheckpoints (SM-CTX-002, Contract v1 Contextual
Project Memory SS 9/10/23-34/64, handoff 10 SS 22/48-58/78).

Scope/``context_key`` validation reuses the Episode schema validators, so the
shared validation handler returns ``INVALID_SCOPE``/``INVALID_CONTEXT_KEY`` (422).

Input and result contracts are deliberately separate (handoff 10 SS 78):
``ContextCheckpointCreateRequest.current_state`` is required, while
``ContextCheckpointResult.current_state`` is nullable because a FORGOTTEN
tombstone (and, after SM-CTX-005, a propagation-INVALIDATED checkpoint)
legitimately has none. Support/Evidence belongs to SM-CTX-004 and is not
accepted (``extra="forbid"``). ``invalidation_reason`` is persisted for audit but
is not part of the Contract SS 24 public model, so it is never exposed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as pydantic_field_validator

from sofias_memory.domain import (
    DEFAULT_CONTEXT_KEY,
    CognitiveMemoryOriginKind,
    ContextCheckpointLifecycle,
    normalize_context_statement_text,
)
from sofias_memory.schemas.contextual_evidence import SupportRequest, SupportResult
from sofias_memory.schemas.episodes import ContextKey, ContextualScope

CHECKPOINT_PAGE_DEFAULT_LIMIT = 50
CHECKPOINT_PAGE_MAX_LIMIT = 100


def _optional_text(value: str | None, field: str) -> str | None:
    return None if value is None else normalize_context_statement_text(value, field=field)


class ContextStatementCreateRequest(BaseModel):
    """One caller-supplied ContextStatement. ``statement_id`` is server-generated
    and never accepted."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Required, non-blank. Normalized (CRLF -> LF, edges trimmed).")
    support: SupportRequest | None = Field(
        default=None,
        description=(
            "Optional statement-level Evidence support (SM-CTX-004). Omitted means no declared "
            "dependency. Never inherited on supersede."
        ),
    )

    @pydantic_field_validator("text")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return normalize_context_statement_text(value, field="text")


class ContextCheckpointProvenanceCreateRequest(BaseModel):
    """Caller-supplied checkpoint provenance (Contract v1 SS 10). External refs
    are opaque values -- never foreign keys, never a Session owner."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str = Field(description="Required, non-blank system identifier.")
    conversation_ref: str | None = Field(default=None, description="Opaque external reference.")
    turn_ref: str | None = Field(default=None, description="Opaque external reference.")

    @pydantic_field_validator("source_system")
    @classmethod
    def _normalize_source_system(cls, value: str) -> str:
        return normalize_context_statement_text(value, field="source_system")

    @pydantic_field_validator("conversation_ref")
    @classmethod
    def _normalize_conversation_ref(cls, value: str | None) -> str | None:
        return _optional_text(value, "conversation_ref")

    @pydantic_field_validator("turn_ref")
    @classmethod
    def _normalize_turn_ref(cls, value: str | None) -> str | None:
        return _optional_text(value, "turn_ref")


class ContextCheckpointSnapshotFields(BaseModel):
    """Complete checkpoint snapshot shared by Create and Supersede (Contract v1
    SS 25/27.2/31.2). Never a partial update: omitted optionals mean *absent*
    in the new revision, never inherited. Collection order is semantic."""

    model_config = ConfigDict(extra="forbid")

    scope: ContextualScope = Field(description="Canonical 'project:<key>'. 'global' is rejected.")
    context_key: ContextKey = Field(
        default=DEFAULT_CONTEXT_KEY,
        description="Context Stream key. Omitted resolves to 'main' (returned explicitly).",
    )
    objective: ContextStatementCreateRequest | None = None
    current_state: ContextStatementCreateRequest = Field(
        description="Required: where this work currently stands."
    )
    approved_decisions: list[ContextStatementCreateRequest] = Field(default_factory=list)
    pending_work: list[ContextStatementCreateRequest] = Field(default_factory=list)
    open_items: list[ContextStatementCreateRequest] = Field(default_factory=list)
    blockers: list[ContextStatementCreateRequest] = Field(default_factory=list)
    next_action: ContextStatementCreateRequest | None = None
    provenance: ContextCheckpointProvenanceCreateRequest


class ContextCheckpointCreateRequest(ContextCheckpointSnapshotFields):
    """``POST /api/v1/context/checkpoints`` body (Contract v1 SS 25/27).

    Creates a new ACTIVE checkpoint only when the Context Stream currently has
    no ACTIVE checkpoint."""


class ContextCheckpointSupersedeRequest(ContextCheckpointSnapshotFields):
    """``POST /api/v1/context/checkpoints/{checkpoint_id}/supersede`` body
    (Contract v1 SS 31.2): the complete replacement snapshot. ``scope`` and the
    resolved ``context_key`` (default ``main``) must equal the target's stream;
    ``current_state`` stays required."""


class ContextCheckpointInvalidateRequest(BaseModel):
    """``POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate`` body."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(description="Required, non-blank; persisted for audit, never returned.")

    @pydantic_field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return normalize_context_statement_text(value, field="reason")


class ContextStatementResult(BaseModel):
    """Public ContextStatement shape in SM-CTX-002 (support arrives in SM-CTX-004)."""

    model_config = ConfigDict(extra="forbid")

    statement_id: UUID
    text: str
    support: SupportResult | None = None


class ContextCheckpointProvenanceResult(BaseModel):
    """Public provenance shape; external refs are ``null`` on a tombstone."""

    model_config = ConfigDict(extra="forbid")

    origin_kind: CognitiveMemoryOriginKind
    source_system: str
    conversation_ref: str | None
    turn_ref: str | None


class ContextCheckpointResult(BaseModel):
    """Public ContextCheckpoint shape (Contract v1 SS 24). On a FORGOTTEN
    tombstone every statement is absent (``null``/``[]``) while ``scope``,
    ``context_key`` and ``revision`` are always kept."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: UUID
    scope: str
    context_key: str
    revision: int
    lifecycle: ContextCheckpointLifecycle
    objective: ContextStatementResult | None
    current_state: ContextStatementResult | None
    approved_decisions: list[ContextStatementResult]
    pending_work: list[ContextStatementResult]
    open_items: list[ContextStatementResult]
    blockers: list[ContextStatementResult]
    next_action: ContextStatementResult | None
    provenance: ContextCheckpointProvenanceResult
    created_at: datetime
    superseded_at: datetime | None
    superseded_by: UUID | None
    invalidated_at: datetime | None
    forgotten_at: datetime | None


class ContextCheckpointListResult(BaseModel):
    """Bounded keyset-paginated listing (Contract v1 SS 30/64). Within one
    stream: ``revision DESC, checkpoint_id ASC``; across streams:
    ``context_key ASC`` first. ``next_cursor`` is opaque and ``null`` on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[ContextCheckpointResult]
    limit: int
    next_cursor: str | None
