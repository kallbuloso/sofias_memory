"""MemoryItem-specific PostgreSQL repository (ADR-0016, SM-1001)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import MemoryItem


class MemoryItemRepository:
    """Persistence operations for Native Cognitive Memory items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, memory_item: MemoryItem) -> MemoryItem:
        self._session.add(memory_item)
        await self._session.flush()
        return memory_item

    async def get_by_id(self, memory_id: UUID) -> MemoryItem | None:
        statement = select(MemoryItem).where(MemoryItem.id == memory_id)
        result = await self._session.scalar(statement)
        return cast(MemoryItem | None, result)

    async def get_by_id_for_update(self, memory_id: UUID) -> MemoryItem | None:
        """Row-locked read serializing every lifecycle-mutating operation
        (supersede, forget) for this MemoryItem (ADR-0016 SS 12/13)."""

        statement = select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(MemoryItem | None, result)
