"""Public schemas for remember text ingest."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.schemas.common import JSONValue


class RememberTextRequest(BaseModel):
    """Direct text remember request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    content: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1)
    mode: str = Field(default="ingest")
    wait: bool = Field(default=True)
    force: bool = Field(default=False)

    @field_validator("dataset", "name", "session_id")
    @classmethod
    def strip_non_content_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped


class RememberTextResult(BaseModel):
    """Result returned after synchronous remember ingest."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["succeeded"]
    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    content_hash: str = Field(min_length=64, max_length=64)
    chunks: int
    entities: int
    relations: int
    deduplicated: bool
