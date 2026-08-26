"""Entity-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


@dataclass(frozen=True)
class EntityEmbeddingCandidate:
    """Detached entity snapshot for Improve entity embedding."""

    entity_id: UUID
    name: str


@dataclass(frozen=True)
class EntityDuplicateCandidate:
    """Detected unordered entity duplicate candidate pair."""

    entity_id: UUID
    entity_name: str
    candidate_id: UUID
    candidate_name: str
    entity_type: str
    similarity: float


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

    async def advance_active_generation(
        self,
        *,
        dataset_id: UUID,
        target_generation: int,
    ) -> list[Entity]:
        """Carry every still-active entity of a dataset into ``target_generation``.

        Required by the Cognify ``rebuild=true`` activation step (SM-510).
        ``uq_entities_dataset_id_canonical_key_active`` makes a dataset's
        active entity set canonical *across* generations -- two active rows
        with the same ``canonical_key`` can never coexist -- so a new
        generation cannot own a parallel copy of the entity graph. Entities
        are therefore carried forward in the very same transaction that flips
        ``Dataset.active_generation``, which is what keeps every
        ``generation == Dataset.active_generation`` reader consistent before
        and after the flip. Rows are locked in deterministic id order.
        """

        statement = (
            select(Entity)
            .where(
                Entity.dataset_id == dataset_id,
                Entity.is_active.is_(True),
                Entity.generation < target_generation,
            )
            .order_by(Entity.id)
            .with_for_update()
        )
        entities = list(await self._session.scalars(statement))
        for entity in entities:
            entity.generation = target_generation
        await self._session.flush()
        return entities

    async def list_missing_embedding_candidates(
        self,
        *,
        dataset_id: UUID,
    ) -> list[EntityEmbeddingCandidate]:
        statement = (
            select(Entity.id, Entity.name)
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.dataset_id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
                Entity.embedding.is_(None),
            )
            .order_by(Entity.id)
        )
        result = await self._session.execute(statement)
        return [
            EntityEmbeddingCandidate(
                entity_id=row.id,
                name=row.name,
            )
            for row in result
        ]

    async def set_missing_embeddings_for_active_current(
        self,
        *,
        dataset_id: UUID,
        embeddings_by_entity_id: dict[UUID, list[float]],
    ) -> int:
        if not embeddings_by_entity_id:
            return 0

        statement = (
            select(Entity)
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.id.in_(list(embeddings_by_entity_id)),
                Entity.dataset_id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
                Entity.embedding.is_(None),
            )
            .order_by(Entity.id)
        )
        entities = await self._session.scalars(statement)
        updated = 0
        for entity in entities:
            entity.embedding = embeddings_by_entity_id[entity.id]
            updated += 1
        await self._session.flush()
        return updated

    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> list[Entity]:
        if not entity_ids:
            return []

        statement = (
            select(Entity)
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.id.in_(entity_ids),
                Entity.dataset_id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
            )
            .order_by(Entity.created_at, Entity.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def get_active_current_by_id(
        self,
        *,
        dataset_id: UUID,
        entity_id: UUID,
    ) -> Entity | None:
        entities = await self.list_active_current_by_ids(
            dataset_id=dataset_id,
            entity_ids=[entity_id],
        )
        return entities[0] if entities else None

    async def list_active_current_entity_types(
        self,
        *,
        dataset_id: UUID,
    ) -> list[str]:
        statement = (
            select(Entity.entity_type)
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.dataset_id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
            )
            .distinct()
            .order_by(Entity.entity_type)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_active_current_for_dataset(
        self,
        *,
        dataset_id: UUID,
    ) -> list[Entity]:
        """All authoritative current-generation entities for a dataset-wide
        mutation (Forget dataset/everything scope, ADR-0010 DATASET_DELETE).

        Accepts ``DELETING`` as well as ``ACTIVE``: Forget's own dataset-scope
        mutation observes this query with an in-memory ``DELETING`` write
        still unflushed (so the committed value it reads is always
        ``ACTIVE``); DATASET_DELETE's ``deactivate_authoritative`` step runs
        in a separate, later transaction where ``DELETING`` is already
        durably committed by the prior ``begin_delete`` step (ADR-0010 D9) --
        excluding it here would silently skip every entity, never
        deactivating or projecting a DELETE for them. Excluding ``DELETED``
        is still correct: nothing legitimately mutates a dataset that has
        already reached its administrative tombstone.
        """

        statement = (
            select(Entity)
            .join(Dataset, Entity.dataset_id == Dataset.id)
            .where(
                Entity.dataset_id == dataset_id,
                Dataset.status.in_((DatasetStatus.ACTIVE, DatasetStatus.DELETING)),
                Entity.generation == Dataset.active_generation,
                Entity.is_active.is_(True),
            )
            .order_by(Entity.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_duplicate_candidates(
        self,
        *,
        dataset_id: UUID,
        similarity_threshold: float,
    ) -> list[EntityDuplicateCandidate]:
        entity = aliased(Entity)
        candidate = aliased(Entity)
        cosine_distance = entity.embedding.cosine_distance(candidate.embedding)
        similarity = (literal(1.0) - cosine_distance).label("similarity")
        normalized_entity_type = func.lower(func.btrim(entity.entity_type))
        normalized_candidate_type = func.lower(func.btrim(candidate.entity_type))
        statement = (
            select(
                entity.id.label("entity_id"),
                entity.name.label("entity_name"),
                candidate.id.label("candidate_id"),
                candidate.name.label("candidate_name"),
                entity.entity_type.label("entity_type"),
                similarity,
            )
            .join(Dataset, entity.dataset_id == Dataset.id)
            .join(
                candidate,
                candidate.dataset_id == entity.dataset_id,
            )
            .where(
                entity.dataset_id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                entity.generation == Dataset.active_generation,
                entity.is_active.is_(True),
                entity.embedding.is_not(None),
                candidate.id > entity.id,
                candidate.generation == Dataset.active_generation,
                candidate.is_active.is_(True),
                candidate.embedding.is_not(None),
                normalized_entity_type == normalized_candidate_type,
                similarity >= similarity_threshold,
            )
            .order_by(similarity.desc(), entity.id, candidate.id)
        )
        result = await self._session.execute(statement)
        return [
            EntityDuplicateCandidate(
                entity_id=row.entity_id,
                entity_name=row.entity_name,
                candidate_id=row.candidate_id,
                candidate_name=row.candidate_name,
                entity_type=row.entity_type,
                similarity=float(row.similarity),
            )
            for row in result
        ]

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
