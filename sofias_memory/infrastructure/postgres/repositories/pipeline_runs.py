"""Pipeline run-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun

FORGET_TARGET_CONFLICT_ERROR_CODE = "FORGET_TARGET_CONFLICT"
"""Marks a FORGET run rejected pre-mutation by a conflict check.

A run tagged with this code never touched authoritative state, so it must
never be picked up by :meth:`~PipelineRunRepository.find_latest_forget_for_source_except`
or :meth:`~PipelineRunRepository.find_latest_forget_for_dataset_except` as the
"latest intent" for a target — otherwise a single incorrectly-retried
request would permanently poison every later, correctly-intentioned retry.
"""


class PipelineRunRepository:
    """Persistence operations for durable pipeline runs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: PipelineRun) -> PipelineRun:
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.id == run_id)
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        """Row-locked read for a single-run transition (no queue-wide claim)."""

        statement = select(PipelineRun).where(PipelineRun.id == run_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def list_by_status(
        self,
        status: PipelineRunStatus,
        *,
        limit: int | None = None,
    ) -> list[PipelineRun]:
        """Runs in one status, oldest first (recovery/observability primitive).

        Not a claim query: no locking, no same-dataset/global-barrier
        arbitration. SM-503 owns queue claiming; SM-507 owns stale recovery
        scanning.
        """

        statement = (
            select(PipelineRun)
            .where(PipelineRun.status == status)
            .order_by(PipelineRun.created_at, PipelineRun.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        result = await self._session.scalars(statement)
        return list(result)

    async def list_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineRun]:
        """Filtered, paginated listing for the future Runs API (SM-508).

        Newest first. Filters are optional and additive; no filter means no
        restriction on that dimension.
        """

        statement = select(PipelineRun).order_by(
            PipelineRun.created_at.desc(), PipelineRun.id.desc()
        )
        if statuses:
            statement = statement.where(PipelineRun.status.in_(statuses))
        if dataset_id is not None:
            statement = statement.where(PipelineRun.dataset_id == dataset_id)
        if pipeline_type is not None:
            statement = statement.where(PipelineRun.pipeline_type == pipeline_type)
        statement = statement.limit(limit).offset(offset)
        result = await self._session.scalars(statement)
        return list(result)

    async def find_latest_forget_for_source_except(
        self,
        *,
        source_id: UUID,
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Most recent FORGET run (any status) that targeted this source.

        Used to recover the *intent* that put a source into ``deleting`` when
        no RUNNING owner remains (e.g. it crashed): a retry may only resume a
        target whose persisted ``payload_hash`` matches this request's.
        """

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.source_id == source_id,
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.id != excluded_run_id,
                PipelineRun.error_code.is_distinct_from(FORGET_TARGET_CONFLICT_ERROR_CODE),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_running_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Detect an incompatible in-flight FORGET touching this dataset or its sources.

        Covers a running dataset-scoped run (``run.dataset_id``), a running
        source-scoped run for any source that belongs to this dataset
        (``run.source_id``), and a running everything-scoped run (which has
        both fields ``NULL`` and, being global, is always a potential
        conflict for any dataset), so dataset/everything forget can avoid
        disputing post-commit drain/storage/finalization with it (FR-090
        concurrency).
        """

        conditions = [
            PipelineRun.dataset_id == dataset_id,
            and_(PipelineRun.dataset_id.is_(None), PipelineRun.source_id.is_(None)),
        ]
        if source_ids:
            conditions.append(PipelineRun.source_id.in_(source_ids))
        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.status == PipelineRunStatus.RUNNING,
                PipelineRun.id != excluded_run_id,
                or_(*conditions),
            )
            .order_by(PipelineRun.created_at, PipelineRun.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_latest_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Most recent FORGET run (any status) that targeted this dataset.

        Same widened match as :meth:`find_running_forget_for_dataset_except`
        (dataset-scoped, source-scoped on one of its sources, or
        everything-scoped) but ignores ``status``, so it can recover the
        intent of a *finished* (e.g. ``failed``) prior attempt when no
        RUNNING owner remains.
        """

        conditions = [
            PipelineRun.dataset_id == dataset_id,
            and_(PipelineRun.dataset_id.is_(None), PipelineRun.source_id.is_(None)),
        ]
        if source_ids:
            conditions.append(PipelineRun.source_id.in_(source_ids))
        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.id != excluded_run_id,
                PipelineRun.error_code.is_distinct_from(FORGET_TARGET_CONFLICT_ERROR_CODE),
                or_(*conditions),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.idempotency_key == idempotency_key)
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)
