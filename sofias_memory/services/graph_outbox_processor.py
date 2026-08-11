"""Process one graph outbox event by applying its projection command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sofias_memory.domain import GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import (
    GraphProjectionPort,
    ProjectionCommand,
    projection_command_from_payload,
)


class GraphOutboxProcessorError(RuntimeError):
    """Base error for one-row graph outbox processing failures."""


class GraphOutboxEventNotFoundError(GraphOutboxProcessorError):
    """Requested graph outbox event does not exist."""


class GraphOutboxAlreadyProcessingError(GraphOutboxProcessorError):
    """Requested graph outbox event is already processing."""


class GraphOutboxPayloadMismatchError(GraphOutboxProcessorError):
    """Graph outbox row columns do not match its projection payload."""


@dataclass(frozen=True)
class GraphOutboxProcessResult:
    """Result of processing one graph outbox event."""

    outbox_id: int
    status: GraphOutboxStatus
    attempt: int
    already_done: bool = False


@dataclass(frozen=True)
class GraphOutboxEventSnapshot:
    """Stable copy of row data needed outside the first PostgreSQL transaction."""

    id: int
    dataset_id: str
    aggregate_type: str
    aggregate_id: str
    operation: str
    payload: dict[str, object]
    attempt: int
    already_done: bool = False


class GraphOutboxProcessor:
    """Process one explicitly selected graph_outbox row."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        projection: GraphProjectionPort,
    ) -> None:
        self._session_factory = session_factory
        self._projection = projection

    async def process(self, outbox_id: int) -> GraphOutboxProcessResult:
        snapshot = await self._start_attempt(outbox_id)
        if snapshot.already_done:
            return GraphOutboxProcessResult(
                outbox_id=snapshot.id,
                status=GraphOutboxStatus.DONE,
                attempt=snapshot.attempt,
                already_done=True,
            )

        try:
            command = projection_command_from_payload(snapshot.payload)
            _validate_row_matches_command(snapshot, command)
            await self._projection.apply(command)
        except Exception:
            await self._mark_failed(snapshot.id)
            raise

        await self._mark_done(snapshot.id)
        return GraphOutboxProcessResult(
            outbox_id=snapshot.id,
            status=GraphOutboxStatus.DONE,
            attempt=snapshot.attempt,
        )

    async def _start_attempt(self, outbox_id: int) -> GraphOutboxEventSnapshot:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            event = await uow.graph_outbox.get_by_id(outbox_id)
            if event is None:
                raise GraphOutboxEventNotFoundError("graph outbox event not found")
            if event.status == GraphOutboxStatus.DONE:
                snapshot = _snapshot(event, already_done=True)
                await uow.commit()
                return snapshot
            if event.status == GraphOutboxStatus.PROCESSING:
                raise GraphOutboxAlreadyProcessingError("graph outbox event already processing")
            if event.status not in {GraphOutboxStatus.PENDING, GraphOutboxStatus.FAILED}:
                raise GraphOutboxProcessorError("graph outbox event status is not processable")

            await uow.graph_outbox.mark_processing(event)
            snapshot = _snapshot(event)
            await uow.commit()
            return snapshot

    async def _mark_done(self, outbox_id: int) -> None:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            await uow.graph_outbox.mark_done(outbox_id, processed_at=datetime.now(UTC))
            await uow.commit()

    async def _mark_failed(self, outbox_id: int) -> None:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            await uow.graph_outbox.mark_failed(outbox_id)
            await uow.commit()


def _snapshot(event: GraphOutbox, *, already_done: bool = False) -> GraphOutboxEventSnapshot:
    return GraphOutboxEventSnapshot(
        id=event.id,
        dataset_id=_uuid_text(event.dataset_id),
        aggregate_type=event.aggregate_type,
        aggregate_id=_uuid_text(event.aggregate_id),
        operation=event.operation.value,
        payload=dict(event.payload),
        attempt=event.attempt,
        already_done=already_done,
    )


def _validate_row_matches_command(
    snapshot: GraphOutboxEventSnapshot,
    command: ProjectionCommand,
) -> None:
    if snapshot.dataset_id != command.dataset_id:
        raise GraphOutboxPayloadMismatchError("graph outbox dataset_id does not match payload")
    if snapshot.aggregate_type != command.aggregate_type:
        raise GraphOutboxPayloadMismatchError("graph outbox aggregate_type does not match payload")
    if snapshot.aggregate_id != command.aggregate_id:
        raise GraphOutboxPayloadMismatchError("graph outbox aggregate_id does not match payload")
    if snapshot.operation != command.operation:
        raise GraphOutboxPayloadMismatchError("graph outbox operation does not match payload")


def _uuid_text(value: UUID | str) -> str:
    return str(value)
