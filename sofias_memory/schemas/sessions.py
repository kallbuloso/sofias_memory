"""Public schemas for Session management (SM-602, ADR-0012)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sofias_memory.domain import SessionStatus, normalize_session_id
from sofias_memory.schemas.common import JSONValue

SESSION_PAGE_DEFAULT_LIMIT = 50
SESSION_PAGE_MAX_LIMIT = 100
SESSION_NAME_MAX_LENGTH = 120


def _strip_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be empty")
    return stripped


class SessionCreateRequest(BaseModel):
    """Explicitly create a Session. Distinct from the lazy get-or-create
    path used elsewhere: a `session_id` that already exists is a conflict,
    never a silent upsert."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied external session identifier. If omitted, "
            "the server generates a UUID and uses its textual form both as "
            "session_uuid and session_id."
        ),
    )
    name: str | None = Field(
        default=None,
        max_length=SESSION_NAME_MAX_LENGTH,
        description="Optional human-readable Session name.",
    )
    metadata: dict[str, JSONValue] = Field(
        default_factory=dict,
        description="Arbitrary caller-supplied metadata stored with the Session.",
    )

    @field_validator("session_id")
    @classmethod
    def normalize_session_id_field(cls, value: str | None) -> str | None:
        return normalize_session_id(value)

    @field_validator("name")
    @classmethod
    def strip_name_field(cls, value: str | None) -> str | None:
        return _strip_name(value)


class SessionUpdateRequest(BaseModel):
    """PATCH payload. Only `name` and `metadata` are mutable; `session_id`
    and `status` are never accepted here (rejected by `extra="forbid"`)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        max_length=SESSION_NAME_MAX_LENGTH,
        description="Replaces the Session name. Explicit null clears it.",
    )
    metadata: dict[str, JSONValue] | None = Field(
        default=None,
        description=(
            "Replaces the Session metadata object entirely (no deep merge). "
            "Must not be null when provided."
        ),
    )

    @field_validator("name")
    @classmethod
    def strip_name_field(cls, value: str | None) -> str | None:
        return _strip_name(value)

    @model_validator(mode="after")
    def require_at_least_one_field_and_reject_null_metadata(self) -> SessionUpdateRequest:
        fields_set = self.model_fields_set
        if "name" not in fields_set and "metadata" not in fields_set:
            raise ValueError("at least one of name or metadata must be provided")
        if "metadata" in fields_set and self.metadata is None:
            raise ValueError("metadata must not be null")
        return self


class SessionResult(BaseModel):
    """Session metadata returned by the public API.

    ``Session.key`` is never exposed under its internal name -- only as
    ``session_id``.
    """

    model_config = ConfigDict(extra="forbid")

    session_uuid: UUID
    session_id: str
    name: str | None
    status: SessionStatus
    metadata: dict[str, JSONValue]
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class SessionListResult(BaseModel):
    """Paginated Session list."""

    model_config = ConfigDict(extra="forbid")

    items: list[SessionResult]
    limit: int
    offset: int
    total: int
