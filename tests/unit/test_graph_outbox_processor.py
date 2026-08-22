from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.ports import ProjectionCommand
from sofias_memory.services.graph_outbox_processor import (
    DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
    GraphOutboxAttemptsExhaustedError,
    GraphOutboxPayloadMismatchError,
    GraphOutboxProcessor,
)

FAKE_NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FakeProjection:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commands: list[ProjectionCommand] = []

    async def apply(self, command: ProjectionCommand) -> None:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure


class FakeAsyncSession:
    """Distinguishes a ``select(GraphOutbox)``-shaped statement (returns the
    single fake event) from a ``select(func.now())``-shaped one (returns a
    real, fixed datetime) by inspecting ``column_descriptions`` -- needed
    once :class:`GraphOutboxRepository.claim_one` does real Python-side
    datetime arithmetic (``now - timedelta(seconds=stale_after_seconds)``)
    against ``processing_started_at``.

    ``on_read`` is an optional hook invoked every time a ``GraphOutbox`` read
    happens, so a test can mutate ``event`` between reads to simulate another
    owner's lease resolving mid-observation (backlog review round 2, SS 3-4:
    the explicit path must keep observing committed state, not fail fast).
    """

    def __init__(
        self,
        event: GraphOutbox | None,
        *,
        now: datetime = FAKE_NOW,
        on_read: Callable[[GraphOutbox], None] | None = None,
    ) -> None:
        self.event = event
        self.now = now
        self.on_read = on_read
        self.read_calls = 0
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    async def scalar(self, statement: object) -> object:
        entity = None
        descriptions = getattr(statement, "column_descriptions", None)
        if descriptions:
            entity = descriptions[0].get("entity")
        if entity is GraphOutbox:
            self.read_calls += 1
            if self.event is not None and self.on_read is not None:
                self.on_read(self.event)
            return self.event
        return self.now

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class FakeSessionFactory:
    def __init__(
        self,
        event: GraphOutbox | None,
        *,
        now: datetime = FAKE_NOW,
        on_read: Callable[[GraphOutbox], None] | None = None,
    ) -> None:
        self.event = event
        self.now = now
        self.on_read = on_read
        self.sessions: list[FakeAsyncSession] = []

    def __call__(self) -> AsyncSession:
        session = FakeAsyncSession(self.event, now=self.now, on_read=self.on_read)
        self.sessions.append(session)
        return cast(AsyncSession, session)


def build_event(
    *,
    status: GraphOutboxStatus = GraphOutboxStatus.PENDING,
    attempt: int = 0,
    processing_started_at: datetime | None = None,
    worker_id: str | None = None,
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
        processing_started_at=processing_started_at,
        worker_id=worker_id,
    )


def make_processor(
    event: GraphOutbox,
    projection: FakeProjection,
    *,
    now: datetime = FAKE_NOW,
    on_read: Callable[[GraphOutbox], None] | None = None,
) -> GraphOutboxProcessor:
    factory = cast(AsyncSessionFactory, FakeSessionFactory(event, now=now, on_read=on_read))
    return GraphOutboxProcessor(
        session_factory=factory,
        projection=projection,
        explicit_observe_interval_seconds=0.001,
    )


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
    assert event.worker_id == processor.worker_id
    assert event.processing_started_at == FAKE_NOW
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
async def test_legacy_processing_event_with_null_lease_is_reclaimed() -> None:
    """A row left ``PROCESSING`` by a pre-SM-506 process (no lease fields
    populated) must not be stuck forever -- backlog SS 6 / ADR-0009 SS V."""

    event = build_event(
        status=GraphOutboxStatus.PROCESSING,
        attempt=1,
        processing_started_at=None,
        worker_id=None,
    )
    projection = FakeProjection()
    processor = make_processor(event, projection)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.attempt == 2
    assert event.worker_id == processor.worker_id
    assert len(projection.commands) == 1


@pytest.mark.asyncio
async def test_stale_processing_event_is_reclaimed_and_processed() -> None:
    stale_processing_started_at = FAKE_NOW - timedelta(
        seconds=DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS + 1
    )
    event = build_event(
        status=GraphOutboxStatus.PROCESSING,
        attempt=1,
        processing_started_at=stale_processing_started_at,
        worker_id="wk-crashed",
    )
    projection = FakeProjection()
    processor = make_processor(event, projection)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.attempt == 2
    assert event.worker_id == processor.worker_id
    assert event.processing_started_at == FAKE_NOW
    assert len(projection.commands) == 1


@pytest.mark.asyncio
async def test_live_processing_event_is_observed_not_stolen_and_returns_once_done() -> None:
    """Backlog review round 2, finding SS 2: a row owned by a non-stale
    lease must never be raced against in parallel, and must never fail the
    caller's request just because the other owner is still working --
    :meth:`GraphOutboxProcessor.process` keeps re-reading committed state
    until that lease resolves."""

    live_processing_started_at = FAKE_NOW - timedelta(seconds=1)
    event = build_event(
        status=GraphOutboxStatus.PROCESSING,
        attempt=1,
        processing_started_at=live_processing_started_at,
        worker_id="wk-other-owner",
    )
    projection = FakeProjection()

    def resolve_after_a_few_reads(current: GraphOutbox) -> None:
        # Simulate the other owner's transaction committing DONE partway
        # through this call's observation loop -- never on the very first
        # read, so the "keep waiting" branch is genuinely exercised first.
        if current.status == GraphOutboxStatus.PROCESSING and processor_reads_seen["count"] >= 3:
            current.status = GraphOutboxStatus.DONE
            current.processed_at = FAKE_NOW
        processor_reads_seen["count"] += 1

    processor_reads_seen = {"count": 0}
    processor = make_processor(event, projection, on_read=resolve_after_a_few_reads)

    result = await processor.process(event.id)

    assert result.status == GraphOutboxStatus.DONE
    assert result.already_done is True
    assert projection.commands == []  # never applied in parallel by this call
    assert event.worker_id == "wk-other-owner"  # ownership never stolen


@pytest.mark.asyncio
async def test_live_processing_event_that_settles_failed_at_ceiling_raises_exhausted() -> None:
    live_processing_started_at = FAKE_NOW - timedelta(seconds=1)
    event = build_event(
        status=GraphOutboxStatus.PROCESSING,
        attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
        processing_started_at=live_processing_started_at,
        worker_id="wk-other-owner",
    )
    projection = FakeProjection()

    def resolve_to_failed(current: GraphOutbox) -> None:
        if current.status == GraphOutboxStatus.PROCESSING:
            current.status = GraphOutboxStatus.FAILED

    processor = make_processor(event, projection, on_read=resolve_to_failed)

    with pytest.raises(GraphOutboxAttemptsExhaustedError):
        await processor.process(event.id)

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
