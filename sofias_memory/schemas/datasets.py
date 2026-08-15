"""Public schemas for dataset management."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import DatasetStatus, SourceKind, SourceStatus
from sofias_memory.schemas.common import JSONValue

DATASET_PAGE_DEFAULT_LIMIT = 50
DATASET_PAGE_MAX_LIMIT = 100
DATASET_NAME_MAX_LENGTH = 120
DATASET_SLUG_MAX_LENGTH = 120


class DatasetCreateRequest(BaseModel):
    """Create a new dataset namespace."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=DATASET_NAME_MAX_LENGTH)
    description: str | None = Field(default=None)
    slug: str | None = Field(default=None, min_length=1, max_length=DATASET_SLUG_MAX_LENGTH)

    @field_validator("name", "description", "slug")
    @classmethod
    def strip_text_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped


class DatasetRenameRequest(BaseModel):
    """Rename an existing dataset."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=DATASET_NAME_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped


class DatasetResult(BaseModel):
    """Dataset metadata returned by the public API."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    name: str
    slug: str
    description: str | None
    status: DatasetStatus
    active_generation: int
    created_at: datetime
    updated_at: datetime


class DatasetListResult(BaseModel):
    """Paginated dataset list."""

    model_config = ConfigDict(extra="forbid")

    items: list[DatasetResult]
    limit: int
    offset: int
    total: int


class DatasetSourceResult(BaseModel):
    """Public source metadata for a dataset."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    dataset_id: UUID
    kind: SourceKind
    name: str
    mime_type: str
    original_uri: str | None
    content_sha256: str = Field(min_length=64, max_length=64)
    normalized_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    byte_size: int
    metadata: dict[str, JSONValue]
    status: SourceStatus
    version: int
    created_at: datetime
    updated_at: datetime


class DatasetSourcesResult(BaseModel):
    """Paginated source list for a dataset."""

    model_config = ConfigDict(extra="forbid")

    items: list[DatasetSourceResult]
    limit: int
    offset: int
    total: int


class DatasetSourcesStats(BaseModel):
    """Source counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    total: int
    active: int


class DatasetDocumentsStats(BaseModel):
    """Document counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    total: int
    active: int


class DatasetChunksStats(BaseModel):
    """Chunk counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    total: int
    active: int


class DatasetEntitiesStats(BaseModel):
    """Entity counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    active_current_generation: int


class DatasetRelationsStats(BaseModel):
    """Relation counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    active_current_generation: int


class DatasetSummariesStats(BaseModel):
    """Summary counters for a dataset."""

    model_config = ConfigDict(extra="forbid")

    total: int


class DatasetStatsResult(BaseModel):
    """Stable PostgreSQL-backed operational dataset counters."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    active_generation: int
    sources: DatasetSourcesStats
    documents: DatasetDocumentsStats
    chunks: DatasetChunksStats
    entities: DatasetEntitiesStats
    relations: DatasetRelationsStats
    summaries: DatasetSummariesStats
