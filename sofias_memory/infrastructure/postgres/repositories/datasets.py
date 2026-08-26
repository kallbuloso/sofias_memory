"""Dataset-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Select, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sofias_memory.domain import DatasetStatus, PipelineStepStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    PipelineRun,
    PipelineStep,
    Relation,
    Source,
    Summary,
)


@dataclass(frozen=True)
class DatasetStatsSnapshot:
    """Detached operational dataset counters."""

    sources_total: int
    sources_active: int
    documents_total: int
    documents_active: int
    chunks_total: int
    chunks_active: int
    entities_active_current_generation: int
    relations_active_current_generation: int
    summaries_total: int


class DatasetRepository:
    """Persistence operations for dataset roots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, dataset: Dataset) -> Dataset:
        self._session.add(dataset)
        await self._session.flush()
        return dataset

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        statement = select(Dataset).where(Dataset.id == dataset_id)
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def get_by_slug(self, slug: str) -> Dataset | None:
        statement = select(Dataset).where(Dataset.slug == slug)
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def get_or_create_by_slug(self, candidate: Dataset) -> Dataset:
        """Lazily resolve ``candidate.slug``, racing safely with a concurrent
        first-ever caller (SM-513 SS 7: Remember's lazy ``main`` creation).

        ``INSERT ... ON CONFLICT (slug) DO NOTHING`` + re-read (the
        :data:`~sofias_memory.services.pipeline_submission.PreparationHook`
        contract's documented pattern) rather than get-then-add: two
        concurrent transactions racing to create the same slug for the
        first time never see an uncaught ``IntegrityError`` from this call --
        the loser's insert silently affects zero rows, and the immediate
        re-read returns the winner's already-committed row. Never used to
        update an existing dataset; a slug that already exists is returned
        unchanged, even if ``candidate``'s other fields differ.
        """

        statement = (
            pg_insert(Dataset)
            .values(
                id=candidate.id,
                name=candidate.name,
                slug=candidate.slug,
                description=candidate.description,
                status=candidate.status,
                active_generation=candidate.active_generation,
            )
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        await self._session.execute(statement)
        await self._session.flush()
        resolved = await self.get_by_slug(candidate.slug)
        assert resolved is not None  # noqa: S101 - just inserted or already existed
        return resolved

    async def get_by_slug_for_update(self, slug: str) -> Dataset | None:
        statement = select(Dataset).where(Dataset.slug == slug).with_for_update()
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def get_by_id_for_update(self, dataset_id: UUID) -> Dataset | None:
        statement = select(Dataset).where(Dataset.id == dataset_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def list_ids_for_everything_forget(self) -> list[UUID]:
        """Enumerate authoritative forget targets: active datasets and
        Forget-owned recoverable-DELETING datasets.

        ADR-0010 D28: a dataset whose ``DELETING`` status is administratively
        owned (at least one ``DATASET_DELETE`` run for it has a persisted
        ``begin_delete`` step that reached ``SUCCEEDED``) is excluded --
        Forget Everything must never resume/finalize it back to ``ACTIVE``.
        An ordinary Forget-owned ``DELETING`` dataset (no such administrative
        lineage ever crossed ``begin_delete``) remains correctly eligible,
        exactly as before this exclusion existed.
        """

        administratively_owned = exists().where(
            PipelineRun.dataset_id == Dataset.id,
            PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
            PipelineStep.run_id == PipelineRun.id,
            PipelineStep.name == "begin_delete",
            PipelineStep.status == PipelineStepStatus.SUCCEEDED,
        )
        statement = (
            select(Dataset.id)
            .where(
                Dataset.status.in_((DatasetStatus.ACTIVE, DatasetStatus.DELETING)),
                ~administratively_owned,
            )
            .order_by(Dataset.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def get_by_name(self, name: str) -> Dataset | None:
        statement = select(Dataset).where(Dataset.name == name)
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def list_paginated(self, *, limit: int, offset: int) -> tuple[list[Dataset], int]:
        statement = (
            select(Dataset).order_by(Dataset.created_at, Dataset.id).limit(limit).offset(offset)
        )
        total_statement = select(func.count()).select_from(Dataset)
        result = await self._session.scalars(statement)
        total = await self._session.scalar(total_statement)
        return list(result), int(total or 0)

    async def stats_for_dataset(self, dataset: Dataset) -> DatasetStatsSnapshot:
        generation = dataset.active_generation
        dataset_id = dataset.id

        return DatasetStatsSnapshot(
            sources_total=await self._count(
                select(func.count()).select_from(Source).where(Source.dataset_id == dataset_id)
            ),
            sources_active=await self._count(
                select(func.count())
                .select_from(Source)
                .where(Source.dataset_id == dataset_id, Source.status == SourceStatus.ACTIVE)
            ),
            documents_total=await self._count(
                select(func.count()).select_from(Document).where(Document.dataset_id == dataset_id)
            ),
            documents_active=await self._count(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.dataset_id == dataset_id,
                    Document.generation == generation,
                    Document.is_active.is_(True),
                )
            ),
            chunks_total=await self._count(
                select(func.count()).select_from(Chunk).where(Chunk.dataset_id == dataset_id)
            ),
            chunks_active=await self._count(
                select(func.count())
                .select_from(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .join(Source, Chunk.source_id == Source.id)
                .where(
                    Chunk.dataset_id == dataset_id,
                    Chunk.generation == generation,
                    Chunk.is_active.is_(True),
                    Document.dataset_id == dataset_id,
                    Document.generation == generation,
                    Document.is_active.is_(True),
                    Source.dataset_id == dataset_id,
                    Source.status == SourceStatus.ACTIVE,
                )
            ),
            entities_active_current_generation=await self._count(
                select(func.count())
                .select_from(Entity)
                .where(
                    Entity.dataset_id == dataset_id,
                    Entity.generation == generation,
                    Entity.is_active.is_(True),
                )
            ),
            relations_active_current_generation=await self._active_current_relations_count(
                dataset_id=dataset_id,
                generation=generation,
            ),
            summaries_total=await self._count(
                select(func.count()).select_from(Summary).where(Summary.dataset_id == dataset_id)
            ),
        )

    async def _count(self, statement: Select[tuple[int]]) -> int:
        result = await self._session.scalar(statement)
        return int(result or 0)

    async def _active_current_relations_count(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> int:
        source_entity = aliased(Entity)
        target_entity = aliased(Entity)
        statement = (
            select(func.count())
            .select_from(Relation)
            .join(source_entity, Relation.source_entity_id == source_entity.id)
            .join(target_entity, Relation.target_entity_id == target_entity.id)
            .where(
                Relation.dataset_id == dataset_id,
                Relation.generation == generation,
                Relation.is_active.is_(True),
                source_entity.dataset_id == dataset_id,
                source_entity.generation == generation,
                source_entity.is_active.is_(True),
                target_entity.dataset_id == dataset_id,
                target_entity.generation == generation,
                target_entity.is_active.is_(True),
            )
        )
        return await self._count(statement)
