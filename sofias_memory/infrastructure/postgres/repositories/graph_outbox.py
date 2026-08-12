"""Graph outbox-specific PostgreSQL repository."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.ports import ProjectionCommand


class GraphOutboxRepository:
    """Persistence operations for graph projection outbox events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: GraphOutbox) -> GraphOutbox:
        self._session.add(event)
        await self._session.flush()
        return event

    async def add_projection_command(self, command: ProjectionCommand) -> GraphOutbox:
        """Persist one ADR-0008 projection snapshot in this active transaction."""

        event = GraphOutbox(
            dataset_id=UUID(command.dataset_id),
            aggregate_type=command.aggregate_type,
            aggregate_id=UUID(command.aggregate_id),
            operation=GraphOutboxOperation(command.operation),
            payload=command.to_payload(),
            status=GraphOutboxStatus.PENDING,
            attempt=0,
            processed_at=None,
        )
        return await self.add(event)

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

    async def list_processable_ids_for_dataset(self, dataset_id: UUID) -> list[int]:
        """Return a detached, dependency-ordered snapshot of pending retryable events."""

        aggregate_order = case(
            (GraphOutbox.aggregate_type == "entity", 0),
            (GraphOutbox.aggregate_type == "chunk", 1),
            (GraphOutbox.aggregate_type == "entity_mention", 2),
            (GraphOutbox.aggregate_type == "relation", 3),
            (GraphOutbox.aggregate_type == "chunk_next", 4),
            else_=5,
        )
        statement = (
            select(GraphOutbox.id)
            .where(
                GraphOutbox.dataset_id == dataset_id,
                GraphOutbox.status.in_((GraphOutboxStatus.PENDING, GraphOutboxStatus.FAILED)),
            )
            .order_by(aggregate_order, GraphOutbox.id)
        )
        result = await self._session.scalars(statement)
        return list(result)
