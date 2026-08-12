"""Public schemas for cognify."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CognifyRequest(BaseModel):
    """Synchronous cognify request for pending sources."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    source_ids: list[UUID] | None = Field(default=None)
    rebuild: bool = Field(default=False)
    wait: bool = Field(default=True)

    @field_validator("dataset")
    @classmethod
    def strip_dataset(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dataset must not be empty")
        return stripped


class CognifyResult(BaseModel):
    """Result returned after synchronous chunking and embedding."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["succeeded"]
    dataset_id: UUID
    generation: int
    sources_processed: int
    chunks: int
    entities: int
    relations: int
