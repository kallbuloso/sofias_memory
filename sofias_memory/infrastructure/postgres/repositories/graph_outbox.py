"""Graph outbox-specific PostgreSQL repository."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import GraphOutboxStatus
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

    async def mark_processing(self, event: GraphOutbox) -> GraphOutbox:
        event.status = GraphOutboxStatus.PROCESSING
        event.attempt += 1
        event.processed_at = None
        await self._session.flush()
        return event

    async def mark_done(self, event_id: int, *, processed_at: datetime) -> GraphOutbox | None:
        event = await self.get_by_id(event_id)
        if event is None:
            return None
        event.status = GraphOutboxStatus.DONE
        event.processed_at = processed_at
        await self._session.flush()
        return event

    async def mark_failed(self, event_id: int) -> GraphOutbox | None:
        event = await self.get_by_id(event_id)
        if event is None:
            return None
        event.status = GraphOutboxStatus.FAILED
        event.processed_at = None
        await self._session.flush()
        return event
