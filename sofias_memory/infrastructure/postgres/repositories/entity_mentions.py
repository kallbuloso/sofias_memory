"""Entity-mention-specific PostgreSQL repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import EntityMention


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
