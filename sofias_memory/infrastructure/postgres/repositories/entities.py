"""Entity-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Entity


@dataclass(frozen=True)
class RecalledEntity:
    """Authoritative entity snapshot for graph recall."""

    id: UUID
    name: str
    entity_type: str
    description: str
    importance_weight: float


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

    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        entity_ids: list[UUID],
    ) -> list[RecalledEntity]:
        if not dataset_ids or not entity_ids:
            return []
        statement = (
            select(
                Entity.id,
                Entity.name,
                Entity.entity_type,
                Entity.description,
                Entity.importance_weight,
            )
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.id.in_(entity_ids),
                Entity.dataset_id.in_(dataset_ids),
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
            )
            .order_by(Entity.id)
        )
        result = await self._session.execute(statement)
        return [
            RecalledEntity(
                id=row.id,
                name=row.name,
                entity_type=row.entity_type,
                description=row.description,
                importance_weight=row.importance_weight,
            )
            for row in result
        ]
