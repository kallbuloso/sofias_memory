"""Pipeline step-specific PostgreSQL repository."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import PipelineStep


class PipelineStepRepository:
    """Persistence operations for pipeline run steps."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, step: PipelineStep) -> PipelineStep:
        self._session.add(step)
        await self._session.flush()
        return step

    async def add_many(self, steps: Sequence[PipelineStep]) -> list[PipelineStep]:
        """Persist a run's full step plan atomically (ADR-0009 SS B/SS C)."""

        self._session.add_all(steps)
        await self._session.flush()
        return list(steps)

    async def get_by_id(self, step_id: UUID) -> PipelineStep | None:
        statement = select(PipelineStep).where(PipelineStep.id == step_id)
        result = await self._session.scalar(statement)
        return cast(PipelineStep | None, result)

    async def get_by_id_for_update(self, step_id: UUID) -> PipelineStep | None:
        """Row-locked read for a single-step transition."""

        statement = select(PipelineStep).where(PipelineStep.id == step_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(PipelineStep | None, result)

    async def get_by_run_and_ordinal(self, run_id: UUID, ordinal: int) -> PipelineStep | None:
        statement = select(PipelineStep).where(
            PipelineStep.run_id == run_id,
            PipelineStep.ordinal == ordinal,
        )
        result = await self._session.scalar(statement)
        return cast(PipelineStep | None, result)

    async def list_for_run(self, run_id: UUID) -> list[PipelineStep]:
        statement = (
            select(PipelineStep)
            .where(PipelineStep.run_id == run_id)
            .order_by(PipelineStep.ordinal, PipelineStep.id)
        )
        result = await self._session.scalars(statement)
        return list(result)
