from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.ports import ProjectionCommand
from sofias_memory.services.graph_outbox_processor import (
    GraphOutboxAlreadyProcessingError,
    GraphOutboxPayloadMismatchError,
    GraphOutboxProcessor,
)


class FakeProjection:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commands: list[ProjectionCommand] = []

    async def apply(self, command: ProjectionCommand) -> None:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure


class FakeAsyncSession:
    def __init__(self, event: GraphOutbox | None) -> None:
        self.event = event
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    async def scalar(self, statement: object) -> GraphOutbox | None:
        return self.event

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class FakeSessionFactory:
    def __init__(self, event: GraphOutbox | None) -> None:
        self.event = event
        self.sessions: list[FakeAsyncSession] = []

    def __call__(self) -> AsyncSession:
        session = FakeAsyncSession(self.event)
        self.sessions.append(session)
        return cast(AsyncSession, session)


def build_event(
    *,
    status: GraphOutboxStatus = GraphOutboxStatus.PENDING,
    attempt: int = 0,
) -> GraphOutbox:
    dataset_id = uuid4()
    entity_id = uuid4()
    return GraphOutbox(
        id=1,
        dataset_id=dataset_id,
        aggregate_type="entity",
        aggregate_id=entity_id,
        operation=GraphOutboxOperation.UPSERT,
        payload={
            "schema_version": 1,
            "aggregate_type": "entity",
            "operation": "upsert",
            "dataset_id": str(dataset_id),
            "aggregate_id": str(entity_id),
            "identity": {"id": str(entity_id)},
            "properties": {
                "id": str(entity_id),
                "dataset_id": str(dataset_id),
                "name": "Sofia",
                "entity_type": "person",
                "description": "Processor test entity.",
                "importance_weight": 0.5,
                "generation": 1,
            },
        },
        status=status,
        attempt=attempt,
        processed_at=None,
    )


def make_processor(
    event: GraphOutbox,
    projection: FakeProjection,
) -> GraphOutboxProcessor:
    factory = cast(AsyncSessionFactory, FakeSessionFactory(event))
    return GraphOutboxProcessor(session_factory=factory, projection=projection)


@pytest.mark.asyncio
async def test_pending_event_marks_processing_applies_projection_and_marks_done() -> None:
    event = build_event()
    projection = FakeProjection()
    processor = make_processor(event, projection)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.attempt == 1
    assert result.already_done is False
    assert event.status == GraphOutboxStatus.DONE
    assert event.attempt == 1
    assert event.processed_at is not None
    assert len(projection.commands) == 1


@pytest.mark.asyncio
async def test_failed_event_can_be_retried_and_increments_attempt() -> None:
    event = build_event(status=GraphOutboxStatus.FAILED, attempt=2)
    projection = FakeProjection()
    processor = make_processor(event, projection)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.attempt == 3
    assert event.status == GraphOutboxStatus.DONE
    assert event.attempt == 3
    assert len(projection.commands) == 1


@pytest.mark.asyncio
async def test_done_event_is_noop_without_projection_or_attempt_increment() -> None:
    event = build_event(status=GraphOutboxStatus.DONE, attempt=1)
    projection = FakeProjection()
    processor = make_processor(event, projection)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.already_done is True
    assert event.attempt == 1
    assert projection.commands == []


@pytest.mark.asyncio
async def test_processing_event_is_rejected_without_projection() -> None:
    event = build_event(status=GraphOutboxStatus.PROCESSING, attempt=1)
    projection = FakeProjection()
    processor = make_processor(event, projection)

    with pytest.raises(GraphOutboxAlreadyProcessingError):
        await processor.process(event.id)

    assert event.status == GraphOutboxStatus.PROCESSING
    assert projection.commands == []


@pytest.mark.asyncio
async def test_payload_mismatch_marks_failed_without_projection() -> None:
    event = build_event()
    event.payload = {
        "schema_version": 1,
        "aggregate_type": "chunk",
        "operation": "upsert",
        "dataset_id": str(event.dataset_id),
        "aggregate_id": str(event.aggregate_id),
        "identity": {"id": str(event.aggregate_id)},
        "properties": {
            "id": str(event.aggregate_id),
            "dataset_id": str(event.dataset_id),
            "source_id": str(uuid4()),
            "document_id": str(uuid4()),
            "ordinal": 0,
            "generation": 1,
        },
    }
    projection = FakeProjection()
    processor = make_processor(event, projection)

    with pytest.raises(GraphOutboxPayloadMismatchError):
        await processor.process(event.id)

    assert event.status == GraphOutboxStatus.FAILED
    assert event.attempt == 1
    assert event.processed_at is None
    assert projection.commands == []


@pytest.mark.asyncio
async def test_projection_failure_marks_failed_and_propagates_error() -> None:
    event = build_event()
    projection = FakeProjection(failure=RuntimeError("neo4j unavailable"))
    processor = make_processor(event, projection)

    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        await processor.process(event.id)

    assert event.status == GraphOutboxStatus.FAILED
    assert event.attempt == 1
    assert event.processed_at is None
    assert len(projection.commands) == 1
