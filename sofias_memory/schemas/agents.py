"""Public schemas for the Agent Management API (SM-802, ADR-0014).

Field-level validation reuses the SM-801 domain primitives exclusively --
no regex/length limit/normalization rule is ever re-implemented here.

``display_name`` and ``instructions`` deliberately do NOT declare a Pydantic
``max_length`` on the raw field: ``display_name`` is trimmed by the domain
validator before its length is checked, and ``instructions`` is newline-
normalized (which can only shorten it) before its length is checked -- a
Pydantic ``max_length`` on the *raw*, pre-transformation value would reject
some values that are valid once transformed (mirrors
``schemas/skills.py``'s own ``procedure`` field, which carries the identical
comment for the identical reason). ``description`` applies no
transformation at all, so its Pydantic ``min_length``/``max_length`` and the
domain validator's bound always agree.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sofias_memory.domain import (
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_NAME_MAX_LENGTH,
    AgentStatus,
    normalize_validate_agent_instructions,
    validate_agent_description,
    validate_agent_display_name,
    validate_agent_name,
)
from sofias_memory.schemas.common import JSONValue

AGENT_PAGE_DEFAULT_LIMIT = 50
AGENT_PAGE_MAX_LIMIT = 100


class AgentCreateRequest(BaseModel):
    """Create a durable Agent Profile. ``name`` already existing, in any
    status, is a conflict -- never an upsert."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        max_length=AGENT_NAME_MAX_LENGTH,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Portable, immutable, globally unique logical identity.",
    )
    display_name: str | None = Field(
        default=None,
        description="Mutable human label, trimmed; non-empty when present.",
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=AGENT_DESCRIPTION_MAX_LENGTH,
    )
    instructions: str | None = Field(
        default=None,
        description=(
            "Opaque declarative text, never interpreted/executed by Sofias "
            "Memory. 1..65536 characters after CRLF/CR -> LF normalization; "
            "the raw request size is not itself bounded here because "
            "normalization can only shorten it."
        ),
    )
    metadata: dict[str, JSONValue] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name_field(cls, value: str) -> str:
        return validate_agent_name(value)

    @field_validator("display_name")
    @classmethod
    def validate_display_name_field(cls, value: str | None) -> str | None:
        return validate_agent_display_name(value)

    @field_validator("description")
    @classmethod
    def validate_description_field(cls, value: str | None) -> str | None:
        return validate_agent_description(value)

    @field_validator("instructions")
    @classmethod
    def validate_instructions_field(cls, value: str | None) -> str | None:
        return normalize_validate_agent_instructions(value)


class AgentUpdateRequest(BaseModel):
    """PATCH payload. Only ``display_name``/``description``/``instructions``/
    ``metadata`` are mutable; ``name`` and ``status`` are never accepted here
    (rejected by ``extra=\"forbid\"``) -- ``status`` changes only via
    archive/restore. Explicit ``null`` clears a text field; ``metadata`` is
    replaced wholesale and must never be null when supplied."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, description="Explicit null clears it.")
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=AGENT_DESCRIPTION_MAX_LENGTH,
        description="Explicit null clears it.",
    )
    instructions: str | None = Field(default=None, description="Explicit null clears it.")
    metadata: dict[str, JSONValue] | None = Field(
        default=None,
        description=(
            "Replaces the Agent metadata object entirely (no deep merge). "
            "Must not be null when provided."
        ),
    )

    @field_validator("display_name")
    @classmethod
    def validate_display_name_field(cls, value: str | None) -> str | None:
        return validate_agent_display_name(value)

    @field_validator("description")
    @classmethod
    def validate_description_field(cls, value: str | None) -> str | None:
        return validate_agent_description(value)

    @field_validator("instructions")
    @classmethod
    def validate_instructions_field(cls, value: str | None) -> str | None:
        return normalize_validate_agent_instructions(value)

    @model_validator(mode="after")
    def require_at_least_one_field_and_reject_null_metadata(self) -> AgentUpdateRequest:
        fields_set = self.model_fields_set
        if not fields_set:
            raise ValueError("at least one field must be provided")
        if "metadata" in fields_set and self.metadata is None:
            raise ValueError("metadata must not be null")
        return self


class AgentResult(BaseModel):
    """Full Agent Profile management detail -- create, get, PATCH, archive,
    and restore all use this one shape. ``agent_uuid`` is the public
    serialization of ``Agent.id``; there is no separate ``agent_uuid``
    column."""

    model_config = ConfigDict(extra="forbid")

    agent_uuid: UUID
    name: str
    display_name: str | None
    description: str | None
    instructions: str | None
    metadata: dict[str, JSONValue]
    status: AgentStatus
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class AgentListItem(BaseModel):
    """Lightweight management-list item -- progressive disclosure. Never
    includes ``instructions`` or ``metadata``; a caller who wants either
    must read ``GET /agents/{agent_uuid}`` explicitly."""

    model_config = ConfigDict(extra="forbid")

    agent_uuid: UUID
    name: str
    display_name: str | None
    description: str | None
    status: AgentStatus
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class AgentListResult(BaseModel):
    """Paginated Agent list."""

    model_config = ConfigDict(extra="forbid")

    items: list[AgentListItem]
    limit: int
    offset: int
    total: int
