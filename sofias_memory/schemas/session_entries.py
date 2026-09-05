"""Public schemas for SessionEntry append/read and Session query history (SM-603)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import normalize_session_entry_external_id
from sofias_memory.schemas.common import JSONValue

SESSION_ENTRY_PAGE_DEFAULT_LIMIT = 50
SESSION_ENTRY_PAGE_MAX_LIMIT = 100
SESSION_ENTRY_CONTENT_MAX_LENGTH = 65_536

SESSION_QUERY_PAGE_DEFAULT_LIMIT = 50
SESSION_QUERY_PAGE_MAX_LIMIT = 100


class SessionEntryCreateRequest(BaseModel):
    """Append a SessionEntry. `external_id`, when present, makes the append
    a safe replay target -- see `SessionEntryService` for the admission/
    replay decision. Without it, every request creates a new row."""

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied correlation/idempotency identity, unique "
            "within this Session. Enables safe replay of this exact append."
        ),
    )
    role: str = Field(
        min_length=1,
        description=(
            "Open-ended contextual label (e.g. user/assistant/agent/tool). "
            "Never mapped to a privileged LLM provider role."
        ),
    )
    content: str = Field(
        min_length=1,
        max_length=SESSION_ENTRY_CONTENT_MAX_LENGTH,
        description="Entry text content.",
    )
    metadata: dict[str, JSONValue] = Field(
        default_factory=dict,
        description="Arbitrary caller-supplied metadata stored with the entry.",
    )

    @field_validator("external_id")
    @classmethod
    def normalize_external_id_field(cls, value: str | None) -> str | None:
        return normalize_session_entry_external_id(value)


class SessionEntryResult(BaseModel):
    """SessionEntry metadata returned by the public API."""

    model_config = ConfigDict(extra="forbid")

    entry_id: UUID
    session_uuid: UUID
    external_id: str | None
    role: str
    content: str
    metadata: dict[str, JSONValue]
    created_at: datetime


class SessionEntryListResult(BaseModel):
    """Paginated SessionEntry list."""

    model_config = ConfigDict(extra="forbid")

    items: list[SessionEntryResult]
    limit: int
    offset: int
    total: int


class SessionQuerySummaryResult(BaseModel):
    """Lightweight Query projection for Session history -- not the full
    Query Provenance shape. `query_text`/`answer` may be null depending on
    persistence configuration."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    dataset_ids: list[UUID]
    mode: str
    query_text: str | None
    answer: str | None
    model: str | None
    created_at: datetime


class SessionQueryListResult(BaseModel):
    """Paginated Session query-history list."""

    model_config = ConfigDict(extra="forbid")

    items: list[SessionQuerySummaryResult]
    limit: int
    offset: int
    total: int
