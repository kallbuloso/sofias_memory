"""Public schemas for Skill management + immutable SkillRevision API (SM-702, ADR-0013).

Field-level validation reuses the SM-701 domain primitives exclusively --
no regex/length limit is ever re-implemented here. Pydantic ``Field``
constraints below exist only to make limits visible in the generated
OpenAPI document; the domain primitive called from each ``field_validator``
remains the semantically authoritative check.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import (
    COMPATIBILITY_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    SKILL_NAME_MAX_LENGTH,
    SkillStatus,
    canonicalize_tags,
    validate_compatibility,
    validate_declared_tools,
    validate_description,
    validate_metadata,
    validate_procedure,
    validate_skill_name,
)

SKILL_PAGE_DEFAULT_LIMIT = 50
SKILL_PAGE_MAX_LIMIT = 100
SKILL_REVISION_PAGE_DEFAULT_LIMIT = 50
SKILL_REVISION_PAGE_MAX_LIMIT = 100


class _SkillRevisionContentFields(BaseModel):
    """Shared revisionable-content fields for create/create-revision
    requests. Never instantiated directly -- both concrete request models
    inherit from it so the same seven ``field_validator``\\ s (each a thin
    wrapper calling the SM-701 domain primitive) are defined exactly once."""

    description: str = Field(
        min_length=1,
        max_length=DESCRIPTION_MAX_LENGTH,
        description="What the Skill does and when to use it.",
    )
    procedure: str = Field(
        min_length=1,
        description=(
            "Markdown procedure body. 1..65536 characters after CRLF/CR -> LF "
            "newline normalization; the raw request size is not itself bounded "
            "here because normalization can only shorten it."
        ),
    )
    license: str | None = Field(default=None)
    compatibility: str | None = Field(default=None, max_length=COMPATIBILITY_MAX_LENGTH)
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="map<string,string>. Must never contain the reserved SKILL.md "
        "transport key 'sofias-memory.tags' -- use tags=[...] instead.",
    )
    tags: list[str] = Field(default_factory=list)
    declared_tools: list[str] = Field(
        default_factory=list,
        description="Descriptive-only hint, never an authorization grant.",
    )

    @field_validator("description")
    @classmethod
    def validate_description_field(cls, value: str) -> str:
        return validate_description(value)

    @field_validator("procedure")
    @classmethod
    def validate_procedure_field(cls, value: str) -> str:
        return validate_procedure(value)

    @field_validator("compatibility")
    @classmethod
    def validate_compatibility_field(cls, value: str | None) -> str | None:
        return validate_compatibility(value)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_field(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_metadata(value)

    @field_validator("tags")
    @classmethod
    def canonicalize_tags_field(cls, value: list[str]) -> list[str]:
        return canonicalize_tags(value)

    @field_validator("declared_tools")
    @classmethod
    def validate_declared_tools_field(cls, value: list[str]) -> list[str]:
        return validate_declared_tools(value)


class SkillCreateRequest(_SkillRevisionContentFields):
    """Create a Skill and its first SkillRevision atomically (Feature
    Contract SS 4.4). ``name`` already existing, in any status, is a
    conflict -- never an upsert, never resolved by content hash."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        max_length=SKILL_NAME_MAX_LENGTH,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Portable, immutable, globally unique logical identity.",
    )

    @field_validator("name")
    @classmethod
    def validate_name_field(cls, value: str) -> str:
        return validate_skill_name(value)


class SkillRevisionCreateRequest(_SkillRevisionContentFields):
    """Create a new SkillRevision on an already-identified Skill. ``name``
    is never accepted here -- it comes from the target Skill and
    participates internally in the content hash. The caller never chooses
    the revision ordinal."""

    model_config = ConfigDict(extra="forbid")


class SkillUpdateRequest(BaseModel):
    """PATCH payload. Strictly administrative: the only mutable field is
    ``current_revision`` (rollback). Never creates a new revision, never
    copies or re-embeds content."""

    model_config = ConfigDict(extra="forbid")

    current_revision: int = Field(
        ge=1,
        description="Target revision integer, must already exist for this Skill.",
    )


class SkillResult(BaseModel):
    """Skill metadata returned by the public API -- create, get, list
    items, PATCH, archive, and restore all use this one shape. Never
    includes ``procedure``, ``resolution_embedding``, ``content_sha256``,
    or the internal SkillRevision UUID."""

    model_config = ConfigDict(extra="forbid")

    skill_uuid: UUID
    name: str
    description: str
    status: SkillStatus
    current_revision: int
    compatibility: str | None
    tags: list[str]
    declared_tools: list[str]
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class SkillListResult(BaseModel):
    """Paginated Skill list."""

    model_config = ConfigDict(extra="forbid")

    items: list[SkillResult]
    limit: int
    offset: int
    total: int


class SkillRevisionResult(BaseModel):
    """Full SkillRevision detail -- the only shape that ever includes
    ``procedure``. Addressed by ``skill_uuid`` + ``revision`` (the
    per-Skill monotonic integer); the internal SkillRevision UUID and
    ``resolution_embedding`` are never exposed."""

    model_config = ConfigDict(extra="forbid")

    skill_uuid: UUID
    revision: int
    description: str
    procedure: str
    license: str | None
    compatibility: str | None
    metadata: dict[str, str]
    tags: list[str]
    declared_tools: list[str]
    content_sha256: str
    created_at: datetime


class SkillRevisionListItem(BaseModel):
    """Lightweight per-revision metadata for the revision list -- never
    includes ``procedure`` or ``metadata``, keeping the listing bounded
    regardless of any single revision's content size."""

    model_config = ConfigDict(extra="forbid")

    skill_uuid: UUID
    revision: int
    description: str
    license: str | None
    compatibility: str | None
    tags: list[str]
    declared_tools: list[str]
    content_sha256: str
    created_at: datetime


class SkillRevisionListResult(BaseModel):
    """Paginated SkillRevision list, ordered by ``revision`` ascending."""

    model_config = ConfigDict(extra="forbid")

    items: list[SkillRevisionListItem]
    limit: int
    offset: int
    total: int


class SkillImportRequest(BaseModel):
    """Standalone ``SKILL.md`` import payload (Feature Contract SS 12.4).
    ``content`` is parsed by
    :func:`sofias_memory.interoperability.skill_md.parse_skill_markdown`,
    which converges to the exact same domain validation the structured
    create/create-revision requests use -- no separate limits are declared
    here. No multipart upload, filesystem path, URL import, zip, or format
    autodetection: standalone ``SKILL.md`` text is the only accepted shape."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, description="Standalone SKILL.md document text.")


class SkillExportResult(BaseModel):
    """Standalone ``SKILL.md`` export payload (Feature Contract SS 12.5).
    ``content_sha256`` is the persisted ``SkillRevision``'s semantic
    content hash -- it is **not** a digest of ``content``'s bytes; the two
    are never asserted equal (Feature Contract SS 12.7)."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["skill_md"]
    content: str
    content_sha256: str
