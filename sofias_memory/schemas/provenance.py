"""Public request and response schemas for the read-only provenance API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sofias_memory.domain import SourceKind, SourceStatus


class ProvenanceEntity(BaseModel):
    """Authoritative entity linked to a source through a chunk mention."""

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    name: str
    entity_type: str
    description: str


class ProvenanceRelationSummary(BaseModel):
    """Authoritative relation supported by evidence within one source."""

    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    predicate: str
    confidence: float


class SourceProvenanceDocument(BaseModel):
    """Authoritative document derived from a source, without full text."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    title: str
    language: str
    chunk_count: int


class SourceProvenanceChunk(BaseModel):
    """Bounded chunk snippet used for source-level provenance."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    ordinal: int
    quote: str
    start_char: int
    end_char: int


class SourceProvenanceResult(BaseModel):
    """Bounded, authoritative lineage for one source."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    dataset_id: UUID
    kind: SourceKind
    name: str
    mime_type: str
    original_uri: str | None
    byte_size: int
    status: SourceStatus
    storage_available: bool
    documents: list[SourceProvenanceDocument]
    chunks: list[SourceProvenanceChunk]
    entities: list[ProvenanceEntity]
    relations: list[ProvenanceRelationSummary]


class RelationEvidenceItem(BaseModel):
    """One authoritative evidence chunk supporting a relation."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_name: str
    document_id: UUID
    chunk_id: UUID
    chunk_ordinal: int
    quote: str
    start_char: int
    end_char: int
    confidence: float
    url: str | None


class RelationProvenanceResult(BaseModel):
    """Bounded, authoritative evidence trail for one relation."""

    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    dataset_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    predicate: str
    description: str
    confidence: float
    evidence: list[RelationEvidenceItem]


class QueryProvenanceReference(BaseModel):
    """One reference persisted by a past query, hydrated against current state."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    document_id: UUID
    chunk_id: UUID
    chunk_ordinal: int
    score: float
    available: bool
    quote: str | None
    source_name: str | None


class SessionContextProvenanceItem(BaseModel):
    """One SessionEntry used in this Query's RAG generation, safely
    re-hydrated and scoped to the Query's own Session. Ordered exactly as
    ``Query.session_context_entry_ids`` (oldest -> newest)."""

    model_config = ConfigDict(extra="forbid")

    entry_id: UUID
    role: str | None
    content: str | None
    available: bool


class QueryProvenanceResult(BaseModel):
    """Audit metadata and safely re-hydrated references for one past query."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    query_text: str | None
    dataset_ids: list[UUID]
    mode: str
    answer: str | None
    model: str | None
    created_at: datetime
    references: list[QueryProvenanceReference]
    session_uuid: UUID | None
    session_context: list[SessionContextProvenanceItem]
