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
from sofias_memory.services.graph_outbox_processor import DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS


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
        max_attempts: int = DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._repository_factory = repository_factory
        self._max_attempts = max_attempts

    async def process_dataset(self, dataset_id: UUID) -> GraphOutboxDrainResult:
        """Drain this dataset's outbox snapshot to convergence.

        The snapshot (``list_processable_ids_for_dataset``) includes any row
        still ``PROCESSING`` -- including one owned by the SM-506 autonomous
        consumer, whether its lease is live or stale -- not just
        ``PENDING``/``FAILED``. ``self._processor.process(id)`` (the same
        :class:`GraphOutboxProcessor` the autonomous consumer uses) already
        implements claim-or-observe: it never applies a projection in
        parallel with another owner, and only returns once that row reaches
        a terminal outcome. ``max_attempts`` here must match the processor's
        own configuration -- both default to the same code-level constant
        (backlog review round 3, SS 1-4) -- so a ``FAILED`` row already at
        the ceiling is excluded from the snapshot the same way it is from
        claim eligibility, instead of raising
        ``GraphOutboxAttemptsExhaustedError`` on every drain call for a
        dataset with one permanently stuck row.
        """

        async with session_scope(self._session_factory) as session:
            repository = self._repository_factory(session)
            outbox_ids = await repository.list_processable_ids_for_dataset(
                dataset_id,
                max_attempts=self._max_attempts,
            )

        processed = 0
        for outbox_id in outbox_ids:
            await self._processor.process(outbox_id)
            processed += 1
        return GraphOutboxDrainResult(dataset_id=dataset_id, processed=processed)
