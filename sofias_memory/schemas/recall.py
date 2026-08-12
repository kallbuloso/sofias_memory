"""Public request and response schemas for chunk and RAG recall."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecallMode = Literal["chunks", "rag", "summaries", "graph", "hybrid", "triplets"]


class RecallFilters(BaseModel):
    """Supported fixed filters for PostgreSQL chunk retrieval."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[UUID] = Field(default_factory=list)
    created_after: datetime | None = None
    created_before: datetime | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_date_range(self) -> RecallFilters:
        if (
            self.created_after is not None
            and self.created_before is not None
            and self.created_after > self.created_before
        ):
            raise ValueError("created_after must be before or equal to created_before")
        return self


class RecallRequest(BaseModel):
    """Recall a ranked chunk context, optionally with a grounded RAG answer."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    datasets: list[str] = Field(default_factory=lambda: ["main"], min_length=1)
    mode: RecallMode = "rag"
    top_k: int | None = Field(default=None, ge=1)
    only_context: bool = False
    include_references: bool = True
    session_id: str | None = Field(default=None, max_length=255)
    filters: RecallFilters = Field(default_factory=RecallFilters)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped

    @field_validator("datasets")
    @classmethod
    def normalize_datasets(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            slug = value.strip()
            if not slug:
                raise ValueError("dataset must not be blank")
            if slug not in seen:
                normalized.append(slug)
                seen.add(slug)
        if not normalized:
            raise ValueError("at least one dataset is required")
        return normalized


class RecallContextItem(BaseModel):
    """One retrieved chunk returned as raw recall context."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    dataset_id: UUID
    source_id: UUID
    source_name: str
    document_id: UUID
    chunk_ordinal: int
    text: str
    start_char: int
    end_char: int
    score: float


class RecallReference(BaseModel):
    """Deterministic provenance reference for a retrieved chunk."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_name: str
    document_id: UUID
    chunk_id: UUID
    chunk_ordinal: int
    quote: str
    start_char: int
    end_char: int
    score: float
    url: str | None


class RecallResult(BaseModel):
    """Recall output with raw context, optional answer, and audit identity."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    mode: str
    answer: str | None
    context: list[RecallContextItem]
    references: list[RecallReference]
    entities: list[object] = Field(default_factory=list)
    relations: list[object] = Field(default_factory=list)
    timings_ms: dict[str, int]
