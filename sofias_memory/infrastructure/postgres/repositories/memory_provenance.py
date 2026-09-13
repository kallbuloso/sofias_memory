"""MemoryProvenance-specific PostgreSQL repository (ADR-0016, SM-1001)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import MemoryProvenance


class MemoryProvenanceRepository:
    """Persistence operations for the 1:1 MemoryItem <-> provenance row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, provenance: MemoryProvenance) -> MemoryProvenance:
        self._session.add(provenance)
        await self._session.flush()
        return provenance

    async def get_by_memory_id(self, memory_id: UUID) -> MemoryProvenance | None:
        statement = select(MemoryProvenance).where(MemoryProvenance.memory_id == memory_id)
        result = await self._session.scalar(statement)
        return cast(MemoryProvenance | None, result)

    async def get_by_memory_id_for_update(self, memory_id: UUID) -> MemoryProvenance | None:
        """Row-locked read for the atomic Forget scrub (ADR-0016 SS 13)."""

        statement = (
            select(MemoryProvenance)
            .where(MemoryProvenance.memory_id == memory_id)
            .with_for_update()
        )
        result = await self._session.scalar(statement)
        return cast(MemoryProvenance | None, result)
