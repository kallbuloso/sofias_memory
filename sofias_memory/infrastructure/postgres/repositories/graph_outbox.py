"""Graph outbox-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.ports import ProjectionCommand


@dataclass(frozen=True, slots=True)
class ClaimedGraphOutbox:
    """Detached lease snapshot for one autonomously claimed outbox row.

    Never carries the ORM instance across a transaction boundary -- plain
    data only, per ADR-0009 SS V (claim commits before Neo4j apply begins).
    """

    outbox_id: int
    dataset_id: str
    aggregate_type: str
    aggregate_id: str
    operation: str
    payload: dict[str, object]
    worker_id: str
    attempt: int
    processing_started_at: datetime

    @classmethod
    def from_model(cls, event: GraphOutbox) -> ClaimedGraphOutbox:
        assert event.worker_id is not None  # noqa: S101 - set by this same claim
        assert event.processing_started_at is not None  # noqa: S101 - set by this same claim
        return cls(
            outbox_id=event.id,
            dataset_id=str(event.dataset_id),
            aggregate_type=event.aggregate_type,
            aggregate_id=str(event.aggregate_id),
            operation=event.operation.value,
            payload=dict(event.payload),
            worker_id=event.worker_id,
            attempt=event.attempt,
            processing_started_at=event.processing_started_at,
        )


def _is_claim_eligible(
    event: GraphOutbox,
    *,
    now: datetime,
    stale_after_seconds: float,
    max_attempts: int,
) -> bool:
    """Python-side mirror of :meth:`GraphOutboxRepository._claim_eligibility_predicate`,
    evaluated against an already row-locked, guaranteed-committed
    :class:`GraphOutbox` instance (ADR-0009 SS V; backlog review round 2,
    SS 3: also what lets a simple in-memory fake session exercise real
    eligibility branches without parsing SQL)."""

    if event.status == GraphOutboxStatus.PENDING:
        return True
    if event.status == GraphOutboxStatus.PROCESSING:
        if event.processing_started_at is None:
            return True
        stale_cutoff = now - timedelta(seconds=stale_after_seconds)
        return event.processing_started_at < stale_cutoff
    if event.status == GraphOutboxStatus.FAILED:
        return event.attempt < max_attempts
    return False


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

    async def get_database_now(self) -> datetime:
        """PostgreSQL's own ``now()`` -- the sole time authority for
        ``processing_started_at``/``processed_at`` (ADR-0009 SS 14)."""

        result = await self._session.scalar(select(func.now()))
        assert result is not None  # noqa: S101 - PostgreSQL now() is never NULL
        return cast(datetime, result)

    @staticmethod
    def _claim_eligibility_predicate(*, stale_after_seconds: float, max_attempts: int) -> Any:
        """ADR-0009 SS V eligibility: pending, stale-processing (including a
        legacy row with a NULL lease predating this migration, backlog SS 6),
        or failed with attempts remaining."""

        stale_cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, stale_after_seconds)
        return or_(
            GraphOutbox.status == GraphOutboxStatus.PENDING,
            and_(
                GraphOutbox.status == GraphOutboxStatus.PROCESSING,
                or_(
                    GraphOutbox.processing_started_at.is_(None),
                    GraphOutbox.processing_started_at < stale_cutoff,
                ),
            ),
            and_(
                GraphOutbox.status == GraphOutboxStatus.FAILED,
                GraphOutbox.attempt < max_attempts,
            ),
        )

    async def list_claimable_ids(
        self,
        *,
        stale_after_seconds: float,
        max_attempts: int,
        limit: int,
    ) -> list[int]:
        """Discovery scan only -- no lock, no ownership (ADR-0009 SS V).

        Ordered by ``id`` ascending: insertion order already respects the
        dependency order producers emit commands in within one authoritative
        transaction (Entity -> Chunk -> MENTIONED_IN -> RELATES_TO -> NEXT).
        """

        statement = (
            select(GraphOutbox.id)
            .where(
                self._claim_eligibility_predicate(
                    stale_after_seconds=stale_after_seconds, max_attempts=max_attempts
                )
            )
            .order_by(GraphOutbox.id.asc())
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def claim_one(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        stale_after_seconds: float,
        max_attempts: int,
    ) -> ClaimedGraphOutbox | None:
        """Lock and lease exactly one candidate row, identified by id.

        ``FOR UPDATE SKIP LOCKED`` gives exclusivity over this row only:
        another concurrent claimer (or finalizer) already holding its lock
        makes this call return ``None`` immediately, never blocking and
        never double-claiming. Once locked, eligibility is re-evaluated
        against the just-read, guaranteed-committed row -- equivalent to
        folding the predicate into the ``WHERE`` clause, but expressed in
        Python so the same method is exercised by both the autonomous
        consumer's discovery-driven claim and the explicit path's
        claim-by-known-id (backlog review round 2, SS 3), which needs to
        distinguish "not eligible because it does not exist / is DONE /
        FAILED-at-ceiling" from "not eligible because SKIP LOCKED found it
        already locked" without a second query.
        """

        statement = (
            select(GraphOutbox).where(GraphOutbox.id == outbox_id).with_for_update(skip_locked=True)
        )
        event = await self._session.scalar(statement)
        if event is None:
            return None

        db_now = await self.get_database_now()
        if not _is_claim_eligible(
            event, now=db_now, stale_after_seconds=stale_after_seconds, max_attempts=max_attempts
        ):
            return None

        event.status = GraphOutboxStatus.PROCESSING
        event.processing_started_at = db_now
        event.worker_id = worker_id
        event.attempt += 1
        event.processed_at = None
        await self._session.flush()
        return ClaimedGraphOutbox.from_model(event)

    async def _finalize_if_owned(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        attempt: int,
        next_status: GraphOutboxStatus,
        processed_at: datetime | None,
    ) -> bool:
        """Guarded finalization (ADR-0009 SS V / backlog SS 15-16).

        ``FOR UPDATE`` + a re-check of ``(worker_id, attempt, status)`` makes
        this atomic: a superseded lease (already reclaimed under a later
        ``attempt``) can never overwrite the current owner's outcome. Returns
        ``False`` when ownership already moved on -- ordinary, not an error.
        """

        statement = (
            select(GraphOutbox)
            .where(
                GraphOutbox.id == outbox_id,
                GraphOutbox.worker_id == worker_id,
                GraphOutbox.attempt == attempt,
                GraphOutbox.status == GraphOutboxStatus.PROCESSING,
            )
            .with_for_update()
        )
        event = await self._session.scalar(statement)
        if event is None:
            return False
        event.status = next_status
        event.processed_at = processed_at
        await self._session.flush()
        return True

    async def mark_done_if_owned(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        attempt: int,
    ) -> bool:
        db_now = await self.get_database_now()
        return await self._finalize_if_owned(
            outbox_id,
            worker_id=worker_id,
            attempt=attempt,
            next_status=GraphOutboxStatus.DONE,
            processed_at=db_now,
        )

    async def mark_failed_if_owned(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        attempt: int,
    ) -> bool:
        return await self._finalize_if_owned(
            outbox_id,
            worker_id=worker_id,
            attempt=attempt,
            next_status=GraphOutboxStatus.FAILED,
            processed_at=None,
        )

    @staticmethod
    def _drain_snapshot_predicate(*, max_attempts: int) -> Any:
        """What the explicit dataset drain must still observe converge to a
        terminal outcome -- a strictly *wider* set than
        :meth:`_claim_eligibility_predicate` (backlog review round 3, SS 1-4).

        Eligibility for *claiming* a row and needing to *observe* a row are
        different questions: a ``PROCESSING`` row under a live, non-stale
        lease is not claimable right now (excluded from
        ``_claim_eligibility_predicate``), but the drain must still include
        it in its snapshot so :class:`GraphOutboxProcessor`'s claim-or-observe
        semantics can wait for that lease to resolve -- otherwise the drain
        would silently skip a row and return before its projection has
        actually converged. Staleness is therefore irrelevant here; only the
        FAILED-at-ceiling exclusion is shared with claim eligibility (a row
        no claimer -- autonomous or explicit -- will ever process again must
        not be included, to avoid ``GraphOutboxAttemptsExhaustedError`` on
        every drain call for a permanently stuck row).
        """

        return or_(
            GraphOutbox.status == GraphOutboxStatus.PENDING,
            GraphOutbox.status == GraphOutboxStatus.PROCESSING,
            and_(
                GraphOutbox.status == GraphOutboxStatus.FAILED,
                GraphOutbox.attempt < max_attempts,
            ),
        )

    async def list_processable_ids_for_dataset(
        self,
        dataset_id: UUID,
        *,
        max_attempts: int,
    ) -> list[int]:
        """Return a detached, dependency-ordered snapshot of every row in
        this dataset that still needs to converge to a terminal outcome --
        including one currently ``PROCESSING`` under a live (possibly
        autonomous-owned) lease, which the caller must observe via
        :class:`GraphOutboxProcessor`'s claim-or-observe semantics rather
        than skip. A row already ``DONE``, or ``FAILED`` at the attempt
        ceiling, is correctly absent -- nothing left to observe for either.
        """

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
                self._drain_snapshot_predicate(max_attempts=max_attempts),
            )
            .order_by(aggregate_order, GraphOutbox.id)
        )
        result = await self._session.scalars(statement)
        return list(result)
