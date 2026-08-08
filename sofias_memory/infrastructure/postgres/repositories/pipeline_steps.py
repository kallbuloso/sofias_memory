"""Pipeline step-specific PostgreSQL repository."""

from __future__ import annotations

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

    async def get_by_id(self, step_id: UUID) -> PipelineStep | None:
        statement = select(PipelineStep).where(PipelineStep.id == step_id)
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
