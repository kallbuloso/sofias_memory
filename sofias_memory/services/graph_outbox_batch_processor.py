"""Drain an explicit dataset snapshot of retryable graph outbox events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.repositories.graph_outbox import GraphOutboxRepository
from sofias_memory.infrastructure.postgres.session import session_scope
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory


class GraphOutboxEventProcessor(Protocol):
    """Minimal one-row processor boundary used by the explicit drain."""

    async def process(self, outbox_id: int) -> object:
        """Process one explicitly identified graph outbox event."""


type GraphOutboxRepositoryFactory = Callable[[AsyncSession], GraphOutboxRepository]


@dataclass(frozen=True)
class GraphOutboxDrainResult:
    """Safe operational count for one finite dataset drain."""

    dataset_id: UUID
    processed: int


class GraphOutboxBatchProcessor:
    """Process a finite dependency-ordered snapshot without polling or claiming work."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        processor: GraphOutboxEventProcessor,
        repository_factory: GraphOutboxRepositoryFactory = GraphOutboxRepository,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._repository_factory = repository_factory

    async def process_dataset(self, dataset_id: UUID) -> GraphOutboxDrainResult:
        """Drain pending/failed events captured before Neo4j processing begins."""

        async with session_scope(self._session_factory) as session:
            repository = self._repository_factory(session)
            outbox_ids = await repository.list_processable_ids_for_dataset(dataset_id)

        processed = 0
        for outbox_id in outbox_ids:
            await self._processor.process(outbox_id)
            processed += 1
        return GraphOutboxDrainResult(dataset_id=dataset_id, processed=processed)
