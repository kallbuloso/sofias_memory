"""Relation-evidence-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sofias_memory.domain import DatasetStatus, SourceStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    Relation,
    RelationEvidence,
    Source,
)
from sofias_memory.schemas.recall import RecallFilters


@dataclass(frozen=True)
class RecalledRelationEvidence:
    """Authoritative relation evidence snapshot for recall references."""

    relation_id: UUID
    chunk_id: UUID
    quote: str
    confidence: float
    dataset_id: UUID
    source_id: UUID
    source_name: str
    source_url: str | None
    document_id: UUID
    chunk_ordinal: int
    start_char: int
    end_char: int


class RelationEvidenceRepository:
    """Persistence operations for exact chunk evidence supporting relations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: RelationEvidence) -> RelationEvidence:
        self._session.add(evidence)
        await self._session.flush()
        return evidence

    async def exists_for_relation_chunk(self, *, relation_id: UUID, chunk_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(RelationEvidence.relation_id)
                .where(
                    RelationEvidence.relation_id == relation_id,
                    RelationEvidence.chunk_id == chunk_id,
                )
                .limit(1)
            )
            is not None
        )

    async def list_for_relations(self, *, relation_ids: list[UUID]) -> list[RelationEvidence]:
        if not relation_ids:
            return []

        result = await self._session.scalars(
            select(RelationEvidence)
            .where(RelationEvidence.relation_id.in_(relation_ids))
            .order_by(RelationEvidence.relation_id, RelationEvidence.chunk_id)
        )
        return list(result)

    async def list_active_relations_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Relation]:
        if not chunk_ids:
            return []

        source_entity = aliased(Entity)
        target_entity = aliased(Entity)
        statement = (
            select(Relation)
            .join(RelationEvidence, RelationEvidence.relation_id == Relation.id)
            .join(Chunk, RelationEvidence.chunk_id == Chunk.id)
            .join(Document, Chunk.document_id == Document.id)
            .join(Source, Chunk.source_id == Source.id)
            .join(Dataset, Chunk.dataset_id == Dataset.id)
            .join(source_entity, Relation.source_entity_id == source_entity.id)
            .join(target_entity, Relation.target_entity_id == target_entity.id)
            .where(
                Chunk.id.in_(chunk_ids),
                Dataset.id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Relation.dataset_id == Dataset.id,
                Relation.generation == Dataset.active_generation,
                Relation.is_active.is_(True),
                source_entity.dataset_id == Dataset.id,
                source_entity.generation == Dataset.active_generation,
                source_entity.is_active.is_(True),
                target_entity.dataset_id == Dataset.id,
                target_entity.generation == Dataset.active_generation,
                target_entity.is_active.is_(True),
                Chunk.dataset_id == Dataset.id,
                Chunk.generation == Dataset.active_generation,
                Chunk.is_active.is_(True),
                Document.dataset_id == Dataset.id,
                Document.generation == Dataset.active_generation,
                Document.is_active.is_(True),
                Source.dataset_id == Dataset.id,
                Source.status == SourceStatus.ACTIVE,
            )
            .order_by(Relation.id)
        )
        result = await self._session.scalars(statement)
        relations_by_id = {relation.id: relation for relation in result}
        return [relations_by_id[relation_id] for relation_id in sorted(relations_by_id)]

    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
        filters: RecallFilters,
    ) -> list[RecalledRelationEvidence]:
        if not dataset_ids or not relation_ids:
            return []

        statement = (
            select(
                RelationEvidence.relation_id,
                RelationEvidence.chunk_id,
                RelationEvidence.quote,
                RelationEvidence.confidence,
                Chunk.dataset_id,
                Chunk.source_id,
                Source.name.label("source_name"),
                Source.original_uri,
                Chunk.document_id,
                Chunk.ordinal,
                Chunk.start_char,
                Chunk.end_char,
            )
            .join(Relation, RelationEvidence.relation_id == Relation.id)
            .join(Chunk, RelationEvidence.chunk_id == Chunk.id)
            .join(Document, Chunk.document_id == Document.id)
            .join(Source, Chunk.source_id == Source.id)
            .join(Dataset, Chunk.dataset_id == Dataset.id)
            .where(
                RelationEvidence.relation_id.in_(relation_ids),
                Chunk.dataset_id.in_(dataset_ids),
                Relation.dataset_id == Dataset.id,
                Dataset.status == DatasetStatus.ACTIVE,
                Relation.generation == Dataset.active_generation,
                Relation.is_active.is_(True),
                Chunk.generation == Dataset.active_generation,
                Chunk.is_active.is_(True),
                Document.dataset_id == Dataset.id,
                Document.generation == Dataset.active_generation,
                Document.is_active.is_(True),
                Source.dataset_id == Dataset.id,
                Source.status == SourceStatus.ACTIVE,
            )
            .order_by(
                RelationEvidence.relation_id,
                RelationEvidence.confidence.desc(),
                Chunk.id,
            )
        )
        if filters.source_ids:
            statement = statement.where(Source.id.in_(filters.source_ids))
        if filters.created_after is not None:
            statement = statement.where(Source.created_at >= filters.created_after)
        if filters.created_before is not None:
            statement = statement.where(Source.created_at <= filters.created_before)
        if filters.metadata:
            statement = statement.where(Source.metadata_.contains(filters.metadata))

        result = await self._session.execute(statement)
        return [
            RecalledRelationEvidence(
                relation_id=row.relation_id,
                chunk_id=row.chunk_id,
                quote=row.quote,
                confidence=row.confidence,
                dataset_id=row.dataset_id,
                source_id=row.source_id,
                source_name=row.source_name,
                source_url=row.original_uri,
                document_id=row.document_id,
                chunk_ordinal=row.ordinal,
                start_char=row.start_char,
                end_char=row.end_char,
            )
            for row in result
        ]
