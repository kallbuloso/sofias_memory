"""Entity-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Entity


class EntityRepository:
    """Persistence operations for canonical entities."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entity: Entity) -> Entity:
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def get_active_by_canonical_key(
        self,
        *,
        dataset_id: UUID,
        canonical_key: str,
    ) -> Entity | None:
        result = await self._session.scalar(
            select(Entity).where(
                Entity.dataset_id == dataset_id,
                Entity.canonical_key == canonical_key,
                Entity.is_active.is_(True),
            )
        )
        return cast(Entity | None, result)
