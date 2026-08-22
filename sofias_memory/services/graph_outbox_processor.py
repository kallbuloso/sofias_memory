"""Process one graph outbox event by applying its projection command.

Shared by the B4 explicit drain (:class:`GraphOutboxBatchProcessor`, called
at the end of a pipeline's ``project_to_neo4j`` step for low-latency
projection) and the SM-506 autonomous safety-net consumer -- one engine, one
lease/finalization contract (ADR-0009 SS V, backlog SS 20).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from sofias_memory.domain import GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.repositories.graph_outbox import ClaimedGraphOutbox
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import (
    GraphProjectionPort,
    ProjectionCommand,
    projection_command_from_payload,
)

DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS = 5
"""ADR-0009 SS V mentions ``max_outbox_attempts`` without freezing a value.
Code-level constant, injectable in tests -- not a new Settings field
(backlog SS 10)."""

DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS = 300.0
"""Operational lease threshold for autonomous stale-processing recovery
(ADR-0009 SS V). Not necessarily equal to ``WORKER_STALE_AFTER_SECONDS`` --
same kind of derived-staleness pattern, own value (backlog SS 11)."""

DEFAULT_EXPLICIT_OBSERVE_INTERVAL_SECONDS = 0.05
"""Poll cadence while :meth:`GraphOutboxProcessor.process` observes a row
owned by another live lease (backlog review round 2, SS 3-4). This is a
polling *cadence*, not a wait *bound* -- the bound is the lease itself
(``processing_started_at`` + ``stale_after_seconds``): a legitimate owner
either finishes (DONE/FAILED) or its lease ages into reclaimable, so the
explicit path never needs, and must never invent, an arbitrary short
timeout that turns "someone else has this lease right now" into a B4
request failure."""


class GraphOutboxProcessorError(RuntimeError):
    """Base error for one-row graph outbox processing failures."""


class GraphOutboxEventNotFoundError(GraphOutboxProcessorError):
    """Requested graph outbox event does not exist."""


class GraphOutboxAlreadyProcessingError(GraphOutboxProcessorError):
    """Reserved for a genuine invariant violation, never for an ordinary
    lease race with another live owner (backlog review round 2, SS 5) --
    :meth:`GraphOutboxProcessor.process` no longer raises this for a row
    currently owned by a non-stale lease; it observes committed state until
    that lease resolves (DONE), is superseded, or the row becomes
    reclaimable."""


class GraphOutboxAttemptsExhaustedError(GraphOutboxProcessorError):
    """The row settled as ``FAILED`` with no attempts remaining while this
    caller was observing another owner's lease (backlog review round 2,
    SS 3.C) -- a real, deterministic outcome, distinct from
    :class:`GraphOutboxAlreadyProcessingError`."""


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
    """Process one explicitly selected graph_outbox row, or claim-and-process
    the next eligible row autonomously (SM-506).

    Both entry points converge on the same :class:`ProjectionCommand` +
    :class:`GraphProjectionPort` apply step and the same fenced
    finalization -- there is no second, parallel projection engine.
    """

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        projection: GraphProjectionPort,
        worker_id: str | None = None,
        max_attempts: int = DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
        stale_after_seconds: float = DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
        explicit_observe_interval_seconds: float = DEFAULT_EXPLICIT_OBSERVE_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._projection = projection
        self._worker_id = worker_id or f"explicit-{uuid4()}"
        self._max_attempts = max_attempts
        self._stale_after_seconds = stale_after_seconds
        self._explicit_observe_interval_seconds = explicit_observe_interval_seconds

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # -- explicit, caller-selected id (B4 drain) --------------------------

    async def process(self, outbox_id: int) -> GraphOutboxProcessResult:
        """Ensure one explicitly identified row reaches a terminal outcome
        (B4 explicit drain path; backlog review round 2, SS 3).

        ``ensure_processed`` semantics:

        - already ``DONE`` -> return immediately, no projection re-applied;
        - ``PENDING``/eligible ``FAILED``/stale ``PROCESSING`` -> claimed and
          processed by this call, through the exact same fenced claim as the
          autonomous consumer (:meth:`_claim_specific`);
        - ``PROCESSING`` owned by a live, non-stale lease (the autonomous
          consumer commonly won this race by a few milliseconds) -> this
          call never applies the projection in parallel and never steals
          the lease; it re-reads committed PostgreSQL state on a short
          cadence until that lease resolves to ``DONE`` (success, returned
          to the caller), becomes reclaimable (looped back into a claim
          attempt by this same call), or -- if the other owner's attempt
          fails and the row has no attempts left -- raises
          :class:`GraphOutboxAttemptsExhaustedError`, a real deterministic
          outcome, never mislabeled as "already processing".
        """

        while True:
            claimed = await self._claim_specific(outbox_id)
            if claimed is not None:
                snapshot = GraphOutboxEventSnapshot(
                    id=claimed.outbox_id,
                    dataset_id=claimed.dataset_id,
                    aggregate_type=claimed.aggregate_type,
                    aggregate_id=claimed.aggregate_id,
                    operation=claimed.operation,
                    payload=claimed.payload,
                    attempt=claimed.attempt,
                )
                return await self._apply_and_finalize(snapshot)

            outcome = await self._read_terminal_or_wait(outbox_id)
            if outcome is not None:
                return outcome
            await asyncio.sleep(self._explicit_observe_interval_seconds)

    async def _claim_specific(self, outbox_id: int) -> ClaimedGraphOutbox | None:
        """Same fenced claim as the autonomous consumer, for one explicitly
        named row. Returns ``None`` when this exact row is not eligible
        right now -- not found, already ``DONE``, ``FAILED`` at the attempt
        ceiling, or ``PROCESSING`` under a still-live lease."""

        async with PostgresUnitOfWork(self._session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                outbox_id,
                worker_id=self._worker_id,
                stale_after_seconds=self._stale_after_seconds,
                max_attempts=self._max_attempts,
            )
            if claimed is None:
                return None
            await uow.commit()
            return claimed

    async def _read_terminal_or_wait(self, outbox_id: int) -> GraphOutboxProcessResult | None:
        """Called only after :meth:`_claim_specific` found this row
        ineligible. Distinguishes a real terminal outcome from an ordinary
        "someone else's live lease" wait. Returns ``None`` to mean "keep
        polling" -- never a signal to fail the caller's request."""

        async with PostgresUnitOfWork(self._session_factory) as uow:
            event = await uow.graph_outbox.get_by_id(outbox_id)
            if event is None:
                raise GraphOutboxEventNotFoundError("graph outbox event not found")
            if event.status == GraphOutboxStatus.DONE:
                snapshot = _snapshot(event, already_done=True)
                await uow.commit()
                return await self._apply_and_finalize(snapshot)
            if event.status == GraphOutboxStatus.FAILED and event.attempt >= self._max_attempts:
                await uow.commit()
                raise GraphOutboxAttemptsExhaustedError(
                    "graph outbox event failed with no attempts remaining"
                )
            if event.status not in {
                GraphOutboxStatus.PENDING,
                GraphOutboxStatus.PROCESSING,
                GraphOutboxStatus.FAILED,
            }:
                raise GraphOutboxProcessorError("graph outbox event status is not processable")
            await uow.commit()
            return None

    # -- autonomous claim (SM-506 safety net) -----------------------------

    async def claim_and_process_one(self) -> GraphOutboxProcessResult | None:
        """Claim and process exactly one eligible row (ADR-0009 SS V).

        Returns ``None`` when no eligible candidate exists right now --
        ordinary, expected, not an error.
        """

        claimed = await self._claim_one()
        if claimed is None:
            return None
        snapshot = GraphOutboxEventSnapshot(
            id=claimed.outbox_id,
            dataset_id=claimed.dataset_id,
            aggregate_type=claimed.aggregate_type,
            aggregate_id=claimed.aggregate_id,
            operation=claimed.operation,
            payload=claimed.payload,
            attempt=claimed.attempt,
        )
        return await self._apply_and_finalize(snapshot)

    async def _claim_one(self) -> ClaimedGraphOutbox | None:
        async with PostgresUnitOfWork(self._session_factory) as scan_uow:
            candidate_ids = await scan_uow.graph_outbox.list_claimable_ids(
                stale_after_seconds=self._stale_after_seconds,
                max_attempts=self._max_attempts,
                limit=1,
            )
        if not candidate_ids:
            return None

        async with PostgresUnitOfWork(self._session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                candidate_ids[0],
                worker_id=self._worker_id,
                stale_after_seconds=self._stale_after_seconds,
                max_attempts=self._max_attempts,
            )
            if claimed is None:
                return None
            await uow.commit()
            return claimed

    # -- shared apply + guarded finalize -----------------------------------

    async def _apply_and_finalize(
        self, snapshot: GraphOutboxEventSnapshot
    ) -> GraphOutboxProcessResult:
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
            await self._mark_failed_if_owned(snapshot.id, attempt=snapshot.attempt)
            raise

        await self._mark_done_if_owned(snapshot.id, attempt=snapshot.attempt)
        return GraphOutboxProcessResult(
            outbox_id=snapshot.id,
            status=GraphOutboxStatus.DONE,
            attempt=snapshot.attempt,
        )

    async def _mark_done_if_owned(self, outbox_id: int, *, attempt: int) -> bool:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            owned = await uow.graph_outbox.mark_done_if_owned(
                outbox_id, worker_id=self._worker_id, attempt=attempt
            )
            await uow.commit()
            return owned

    async def _mark_failed_if_owned(self, outbox_id: int, *, attempt: int) -> bool:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            owned = await uow.graph_outbox.mark_failed_if_owned(
                outbox_id, worker_id=self._worker_id, attempt=attempt
            )
            await uow.commit()
            return owned


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
