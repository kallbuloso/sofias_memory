"""Pipeline run-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import PipelineRun


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

    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.idempotency_key == idempotency_key)
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)
