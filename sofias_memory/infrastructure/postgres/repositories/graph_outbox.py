"""Graph outbox-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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

    async def list_status_by_ids(self, ids: list[int]) -> dict[int, tuple[GraphOutboxStatus, int]]:
        """Exact, by-row-id ``(status, attempt)`` snapshot for a specific set
        of outbox rows (SM-515, ADR-0010 Finding 2).

        Unlike :meth:`list_claimable_ids`/the drain-eligibility predicate --
        which deliberately excludes a row permanently ``FAILED`` at the
        attempt ceiling because it is no longer *processable* -- this method
        answers a different question: "what did these EXACT rows end up as,
        including a dead-ended one?" A caller that needs to prove
        convergence (not merely drain what remains claimable) must use this,
        never infer non-membership-in-claimable as proof of ``DONE``.
        """

        if not ids:
            return {}
        statement = select(GraphOutbox.id, GraphOutbox.status, GraphOutbox.attempt).where(
            GraphOutbox.id.in_(ids)
        )
        result = await self._session.execute(statement)
        return {row.id: (row.status, row.attempt) for row in result}

    async def get_database_now(self) -> datetime:
        """PostgreSQL's own ``now()`` -- the sole time authority for
        ``processing_started_at``/``processed_at`` (ADR-0009 SS 14)."""

        result = await self._session.scalar(select(func.now()))
        assert result is not None  # noqa: S101 - PostgreSQL now() is never NULL
        return cast(datetime, result)

    @staticmethod
    def _blocking_upsert_status_predicate(column_owner: Any, *, max_attempts: int) -> Any:
        """Statuses that make an UPSERT row still "relevant" for the
        cross-row fence below -- the same three statuses
        ``_drain_snapshot_predicate`` treats as not-yet-terminal. A row
        ``FAILED`` at the attempt ceiling is excluded: it has no autonomous
        resurrection path (nothing resets its attempt counter, and no new
        pipeline work can target a DELETING/DELETED dataset), so it can
        never recreate a projection later -- safe to ignore here."""

        return or_(
            column_owner.status == GraphOutboxStatus.PENDING,
            column_owner.status == GraphOutboxStatus.PROCESSING,
            and_(
                column_owner.status == GraphOutboxStatus.FAILED,
                column_owner.attempt < max_attempts,
            ),
        )

    def _blocking_upsert_exists_for_dataset(self, dataset_id: Any, *, max_attempts: int) -> Any:
        """Correlated ``EXISTS`` proving "this dataset still has a relevant
        UPSERT" -- the durable, PostgreSQL-authoritative cross-row fence
        (backlog review round 2, BLOCKER): row-level claim-or-observe only
        prevents two workers from claiming the *same* row; it does nothing
        to stop an autonomous worker from independently claiming a DELETE
        row for dataset X while a *different* row -- an older UPSERT for
        the same dataset -- is still PENDING/PROCESSING/retryable-FAILED
        under a live or separate lease. Evaluated fresh at claim time (never
        cached, never backed by an in-memory mutex), so discovery and claim
        can never together violate the invariant."""

        blocking = aliased(GraphOutbox)
        return (
            select(blocking.id)
            .where(
                blocking.dataset_id == dataset_id,
                blocking.operation == GraphOutboxOperation.UPSERT,
                self._blocking_upsert_status_predicate(blocking, max_attempts=max_attempts),
            )
            .exists()
        )

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

        A DELETE row is excluded from discovery while its own dataset still
        has a relevant UPSERT outstanding (backlog review round 2, BLOCKER)
        -- the same cross-row fence :meth:`claim_one` re-checks atomically
        at claim time, since a row excluded here could otherwise still be
        raced onto another candidate list between this scan and the claim.
        """

        statement = (
            select(GraphOutbox.id)
            .where(
                self._claim_eligibility_predicate(
                    stale_after_seconds=stale_after_seconds, max_attempts=max_attempts
                ),
                or_(
                    GraphOutbox.operation != GraphOutboxOperation.DELETE,
                    ~self._blocking_upsert_exists_for_dataset(
                        GraphOutbox.dataset_id, max_attempts=max_attempts
                    ),
                ),
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

        if event.operation == GraphOutboxOperation.DELETE and await self._has_blocking_upsert(
            event.dataset_id, max_attempts=max_attempts
        ):
            # Cross-row fence (backlog review round 2, BLOCKER): re-evaluated
            # here, inside the same transaction that is about to claim this
            # row, so discovery (list_claimable_ids) racing a concurrent
            # claim elsewhere can never together let a DELETE win while a
            # relevant UPSERT for the same dataset is still outstanding.
            # This only reads committed state -- it never locks the blocking
            # UPSERT row(s), so it never holds a PostgreSQL lock across the
            # Neo4j I/O that happens after this claim commits.
            return None

        event.status = GraphOutboxStatus.PROCESSING
        event.processing_started_at = db_now
        event.worker_id = worker_id
        event.attempt += 1
        event.processed_at = None
        await self._session.flush()
        return ClaimedGraphOutbox.from_model(event)

    async def _has_blocking_upsert(self, dataset_id: UUID, *, max_attempts: int) -> bool:
        """Non-correlated form of :meth:`_blocking_upsert_exists_for_dataset`
        for a single, already-known ``dataset_id`` -- used by
        :meth:`claim_one`'s atomic re-check."""

        statement = (
            select(GraphOutbox.id)
            .where(
                GraphOutbox.dataset_id == dataset_id,
                GraphOutbox.operation == GraphOutboxOperation.UPSERT,
                self._blocking_upsert_status_predicate(GraphOutbox, max_attempts=max_attempts),
            )
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return result is not None

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

        Ordering is two-phase, by ``(operation, aggregate_type)`` -- never
        ``aggregate_type`` alone (a production incident traced to exactly
        that gap: a mixed snapshot of stale UPSERTs left over from an
        earlier, still-converging pipeline run alongside a fresh
        administrative Dataset delete's DELETEs let ``entity``/``chunk``
        DELETEs run before older ``entity_mention``/``relation`` UPSERTs,
        which then failed with a missing-endpoint error because their
        Entity/Chunk anchor node was already gone):

        1. Every UPSERT first, in dependency order (Entity/Chunk before
           EntityMention/Relation/ChunkNext) -- this is what lets a stale
           UPSERT left over from a still-converging or since-abandoned
           pipeline run safely finish (or fail out under its own retry
           budget) before anything in this dataset is torn down, instead of
           racing a DELETE that removes its endpoint out from under it.
        2. Every DELETE second, in the *reverse* order (EntityMention/
           Relation/ChunkNext before Chunk/Entity) -- edges/dependents are
           removed before the nodes they point at, mirroring how UPSERTs
           build the graph up.

        Because one drain call processes this whole ordered snapshot to
        completion before returning (claim-or-observe, never skip), every
        UPSERT already enqueued for this dataset is guaranteed to reach a
        terminal outcome before any DELETE in the same snapshot begins --
        so a Dataset can never end up torn down with a dangling stale
        UPSERT still able to recreate part of its projection afterward.
        """

        operation_order = case(
            (GraphOutbox.operation == GraphOutboxOperation.UPSERT, 0),
            (GraphOutbox.operation == GraphOutboxOperation.DELETE, 1),
            else_=2,
        )
        upsert_aggregate_order = case(
            (GraphOutbox.aggregate_type == "entity", 0),
            (GraphOutbox.aggregate_type == "chunk", 1),
            (GraphOutbox.aggregate_type == "entity_mention", 2),
            (GraphOutbox.aggregate_type == "relation", 3),
            (GraphOutbox.aggregate_type == "chunk_next", 4),
            else_=5,
        )
        delete_aggregate_order = case(
            (GraphOutbox.aggregate_type == "chunk_next", 0),
            (GraphOutbox.aggregate_type == "relation", 1),
            (GraphOutbox.aggregate_type == "entity_mention", 2),
            (GraphOutbox.aggregate_type == "chunk", 3),
            (GraphOutbox.aggregate_type == "entity", 4),
            else_=5,
        )
        aggregate_order = case(
            (GraphOutbox.operation == GraphOutboxOperation.DELETE, delete_aggregate_order),
            else_=upsert_aggregate_order,
        )
        statement = (
            select(GraphOutbox.id)
            .where(
                GraphOutbox.dataset_id == dataset_id,
                self._drain_snapshot_predicate(max_attempts=max_attempts),
            )
            .order_by(operation_order, aggregate_order, GraphOutbox.id)
        )
        result = await self._session.scalars(statement)
        return list(result)
