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

    query: str = Field(
        min_length=1,
        max_length=4000,
        description="The question or search text to retrieve context for.",
        examples=["What component does Project Aurora use?"],
    )
    datasets: list[str] = Field(
        default_factory=lambda: ["main"],
        min_length=1,
        description="Dataset slugs to search. Results are combined across all listed datasets.",
    )
    mode: RecallMode = Field(
        default="rag",
        description=(
            "'chunks': ranked vector-search chunks. 'summaries': document summaries. "
            "'graph': entity/relation subgraph traversal seeded from the query. "
            "'triplets': authoritative entity/relation triplets. 'hybrid': "
            "rank-fused vector+graph retrieval. 'rag': hybrid retrieval plus a "
            "generated, provenance-backed answer (the only mode that calls the LLM)."
        ),
    )
    top_k: int | None = Field(
        default=None, ge=1, description="Maximum number of context items to return."
    )
    only_context: bool = Field(
        default=False,
        description="If true, skip answer generation even in mode=rag and return only context.",
    )
    include_references: bool = Field(
        default=True, description="Include provenance references alongside the context."
    )
    session_id: str | None = Field(
        default=None,
        max_length=255,
        description="Optional caller-supplied session identifier, stored for correlation only.",
    )
    filters: RecallFilters = Field(
        default_factory=RecallFilters,
        description="Optional fixed filters applied to chunk retrieval.",
    )

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


class RecallSummaryContextItem(BaseModel):
    """One retrieved document summary returned as recall context."""

    model_config = ConfigDict(extra="forbid")

    summary_id: UUID
    dataset_id: UUID
    source_id: UUID
    source_name: str
    document_id: UUID
    text: str
    score: float
    url: str | None


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


class RecallEntity(BaseModel):
    """Authoritative entity returned by graph recall."""

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    name: str
    entity_type: str
    description: str
    score: float


class RecallRelation(BaseModel):
    """Authoritative relation/triplet returned by graph recall."""

    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    predicate: str
    description: str
    confidence: float
    score: float
    evidence: str | None = None
    evidence_chunk_id: UUID | None = None


class RecallResult(BaseModel):
    """Recall output with raw context, optional answer, and audit identity."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID = Field(
        description="Durable identifier for this query, used for provenance/feedback."
    )
    mode: str = Field(description="The recall mode that was actually executed.")
    answer: str | None = Field(
        description=(
            "LLM-generated answer (mode=rag only, unless only_context=true). Null otherwise."
        )
    )
    context: list[RecallContextItem | RecallSummaryContextItem] = Field(
        description="Ranked retrieved context items backing the answer, if any."
    )
    references: list[RecallReference] = Field(
        description="Deterministic provenance references for each retrieved chunk."
    )
    entities: list[RecallEntity] = Field(
        default_factory=list, description="Entities returned by graph/triplets/hybrid modes."
    )
    relations: list[RecallRelation] = Field(
        default_factory=list, description="Relations returned by graph/triplets/hybrid modes."
    )
    timings_ms: dict[str, int] = Field(description="Per-stage timing breakdown in milliseconds.")
