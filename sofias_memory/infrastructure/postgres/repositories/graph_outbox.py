"""Graph outbox-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import GraphOutbox


class GraphOutboxRepository:
    """Persistence operations for graph projection outbox events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: GraphOutbox) -> GraphOutbox:
        self._session.add(event)
        await self._session.flush()
        return event

    async def get_by_id(self, event_id: int) -> GraphOutbox | None:
        statement = select(GraphOutbox).where(GraphOutbox.id == event_id)
        result = await self._session.scalar(statement)
        return cast(GraphOutbox | None, result)
