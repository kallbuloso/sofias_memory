from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.repositories.graph_outbox import GraphOutboxRepository
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor


class FakeSession:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self) -> AsyncSession:
        session = FakeSession()
        self.sessions.append(session)
        return cast(AsyncSession, session)


class FakeGraphOutboxRepository:
    ids: list[int] = []

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_processable_ids_for_dataset(
        self,
        dataset_id: UUID,
        *,
        max_attempts: int,
    ) -> list[int]:
        del max_attempts
        return list(type(self).ids)


class RecordingProcessor:
    def __init__(self, *, failure_id: int | None = None) -> None:
        self.failure_id = failure_id
        self.calls: list[int] = []

    async def process(self, outbox_id: int) -> object:
        self.calls.append(outbox_id)
        if outbox_id == self.failure_id:
            raise RuntimeError("projection unavailable")
        return object()


@pytest.mark.asyncio
async def test_drain_processes_dependency_ordered_snapshot_and_stops_at_failure() -> None:
    dataset_id = uuid4()
    FakeGraphOutboxRepository.ids = [11, 12, 13, 14, 15]
    session_factory = FakeSessionFactory()
    processor = RecordingProcessor(failure_id=14)
    drain = GraphOutboxBatchProcessor(
        session_factory=cast(AsyncSessionFactory, session_factory),
        processor=processor,
        repository_factory=cast(type[GraphOutboxRepository], FakeGraphOutboxRepository),
    )

    with pytest.raises(RuntimeError, match="projection unavailable"):
        await drain.process_dataset(dataset_id)

    assert processor.calls == [11, 12, 13, 14]
    assert session_factory.sessions[0].close_calls == 1


@pytest.mark.asyncio
async def test_drain_returns_zero_for_empty_processable_snapshot() -> None:
    dataset_id = uuid4()
    FakeGraphOutboxRepository.ids = []
    processor = RecordingProcessor()
    drain = GraphOutboxBatchProcessor(
        session_factory=cast(AsyncSessionFactory, FakeSessionFactory()),
        processor=processor,
        repository_factory=cast(type[GraphOutboxRepository], FakeGraphOutboxRepository),
    )

    result = await drain.process_dataset(dataset_id)

    assert result.dataset_id == dataset_id
    assert result.processed == 0
    assert processor.calls == []
