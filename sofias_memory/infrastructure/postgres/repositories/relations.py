"""Relation-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Relation


class RelationRepository:
    """Persistence operations for canonical directed relations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, relation: Relation) -> Relation:
        self._session.add(relation)
        await self._session.flush()
        return relation

    async def get_active_by_identity(
        self,
        *,
        source_entity_id: UUID,
        target_entity_id: UUID,
        predicate: str,
        generation: int,
    ) -> Relation | None:
        result = await self._session.scalar(
            select(Relation).where(
                Relation.source_entity_id == source_entity_id,
                Relation.target_entity_id == target_entity_id,
                Relation.predicate == predicate,
                Relation.generation == generation,
                Relation.is_active.is_(True),
            )
        )
        return cast(Relation | None, result)
