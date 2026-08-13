"""Relation-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, Relation


@dataclass(frozen=True)
class RecalledRelation:
    """Authoritative relation snapshot for graph recall."""

    id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    predicate: str
    description: str
    confidence: float
    importance_weight: float


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

    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
    ) -> list[RecalledRelation]:
        if not dataset_ids or not relation_ids:
            return []

        source_entity = aliased(Entity)
        target_entity = aliased(Entity)
        statement = (
            select(
                Relation.id,
                Relation.source_entity_id,
                Relation.target_entity_id,
                Relation.predicate,
                Relation.description,
                Relation.confidence,
                Relation.importance_weight,
            )
            .join(Dataset, Relation.dataset_id == Dataset.id)
            .join(source_entity, Relation.source_entity_id == source_entity.id)
            .join(target_entity, Relation.target_entity_id == target_entity.id)
            .where(
                Relation.id.in_(relation_ids),
                Relation.dataset_id.in_(dataset_ids),
                Dataset.status == DatasetStatus.ACTIVE,
                Relation.generation == Dataset.active_generation,
                Relation.is_active.is_(True),
                source_entity.dataset_id == Dataset.id,
                source_entity.generation == Dataset.active_generation,
                source_entity.is_active.is_(True),
                target_entity.dataset_id == Dataset.id,
                target_entity.generation == Dataset.active_generation,
                target_entity.is_active.is_(True),
            )
            .order_by(Relation.id)
        )
        result = await self._session.execute(statement)
        return [
            RecalledRelation(
                id=row.id,
                source_entity_id=row.source_entity_id,
                target_entity_id=row.target_entity_id,
                predicate=row.predicate,
                description=row.description,
                confidence=row.confidence,
                importance_weight=row.importance_weight,
            )
            for row in result
        ]
