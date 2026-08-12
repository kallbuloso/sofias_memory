"""Summary-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SummaryTargetType
from sofias_memory.infrastructure.postgres.models import Summary


class SummaryRepository:
    """Persistence operations for retrieval summaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, summary: Summary) -> Summary:
        self._session.add(summary)
        await self._session.flush()
        return summary

    async def get_by_id(self, summary_id: UUID) -> Summary | None:
        result = await self._session.scalar(select(Summary).where(Summary.id == summary_id))
        return cast(Summary | None, result)

    async def get_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> Summary | None:
        statement = (
            select(Summary)
            .where(
                Summary.dataset_id == dataset_id,
                Summary.generation == generation,
                Summary.target_type == target_type,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.is_active.is_(True),
            )
            .order_by(Summary.created_at.desc(), Summary.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(Summary | None, result)
