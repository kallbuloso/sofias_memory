"""Entity-mention-specific PostgreSQL repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus, SourceStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    Source,
)


class EntityMentionRepository:
    """Persistence operations for chunk-level entity evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, mention: EntityMention) -> EntityMention:
        self._session.add(mention)
        await self._session.flush()
        return mention

    async def exists_for_entity_chunk(self, *, entity_id: UUID, chunk_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(EntityMention.id)
                .where(
                    EntityMention.entity_id == entity_id,
                    EntityMention.chunk_id == chunk_id,
                )
                .limit(1)
            )
            is not None
        )

    async def list_for_entities(self, *, entity_ids: list[UUID]) -> list[EntityMention]:
        if not entity_ids:
            return []

        result = await self._session.scalars(
            select(EntityMention)
            .where(EntityMention.entity_id.in_(entity_ids))
            .order_by(EntityMention.id)
        )
        return list(result)

    async def list_active_entities_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Entity]:
        if not chunk_ids:
            return []

        statement = (
            select(Entity)
            .join(EntityMention, EntityMention.entity_id == Entity.id)
            .join(Chunk, EntityMention.chunk_id == Chunk.id)
            .join(Document, Chunk.document_id == Document.id)
            .join(Source, Chunk.source_id == Source.id)
            .join(Dataset, Chunk.dataset_id == Dataset.id)
            .where(
                Chunk.id.in_(chunk_ids),
                Dataset.id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.dataset_id == Dataset.id,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
                Chunk.dataset_id == Dataset.id,
                Chunk.generation == Dataset.active_generation,
                Chunk.is_active.is_(True),
                Document.dataset_id == Dataset.id,
                Document.generation == Dataset.active_generation,
                Document.is_active.is_(True),
                Source.dataset_id == Dataset.id,
                Source.status == SourceStatus.ACTIVE,
            )
            .order_by(Entity.id)
        )
        result = await self._session.scalars(statement)
        entities_by_id = {entity.id: entity for entity in result}
        return [entities_by_id[entity_id] for entity_id in sorted(entities_by_id)]
