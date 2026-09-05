"""Pipeline run-specific PostgreSQL repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, literal, or_, select, text, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep

type _SelectAny = Select[Any]


def _apply_list_page_filters(
    statement: _SelectAny,
    *,
    statuses: list[PipelineRunStatus] | None,
    dataset_id: UUID | None,
    pipeline_type: PipelineType | None,
    session_id: UUID | None = None,
) -> _SelectAny:
    """Shared predicate set for :meth:`PipelineRunRepository.list_page` and
    :meth:`PipelineRunRepository.count_page` (SM-508) -- count and list must
    never drift on what they consider a match."""

    if statuses:
        statement = statement.where(PipelineRun.status.in_(statuses))
    if dataset_id is not None:
        statement = statement.where(PipelineRun.dataset_id == dataset_id)
    if pipeline_type is not None:
        statement = statement.where(PipelineRun.pipeline_type == pipeline_type)
    if session_id is not None:
        statement = statement.where(PipelineRun.session_id == session_id)
    return statement


FORGET_TARGET_CONFLICT_ERROR_CODE = "FORGET_TARGET_CONFLICT"
"""Marks a FORGET run rejected pre-mutation by a conflict check.

A run tagged with this code never touched authoritative state, so it must
never be picked up by :meth:`~PipelineRunRepository.find_latest_forget_for_source_except`
or :meth:`~PipelineRunRepository.find_latest_forget_for_dataset_except` as the
"latest intent" for a target — otherwise a single incorrectly-retried
request would permanently poison every later, correctly-intentioned retry.
"""


AUTHORITATIVE_MUTATION_STEP_NAMES_BY_PIPELINE_TYPE: dict[PipelineType, str] = {
    PipelineType.FORGET: "authoritative_mutation",
    PipelineType.DATASET_DELETE: "deactivate_authoritative",
}
"""ADR-0011 D31 sixth amendment / D43: the single, shared mapping of which
``PipelineStep`` name durably proves a destructive run's authoritative
mutation completed, for each destructive ``pipeline_type``. Used by both
:meth:`PipelineRunRepository.find_compatible_destructive_lineage` (D34 Case
B, Source-classification level) and
:meth:`PipelineRunRepository.authoritative_mutation_succeeded` (D31/D43,
run-claim level) -- one definition, never two subtly different ones."""


class PipelineRunRepository:
    """Persistence operations for durable pipeline runs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: PipelineRun) -> PipelineRun:
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.id == run_id)
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        """Row-locked read for a single-run transition (no queue-wide claim)."""

        statement = select(PipelineRun).where(PipelineRun.id == run_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def list_by_status(
        self,
        status: PipelineRunStatus,
        *,
        limit: int | None = None,
    ) -> list[PipelineRun]:
        """Runs in one status, oldest first (recovery/observability primitive).

        Not a claim query: no locking, no same-dataset/global-barrier
        arbitration. SM-503 owns queue claiming; SM-507 owns stale recovery
        scanning.
        """

        statement = (
            select(PipelineRun)
            .where(PipelineRun.status == status)
            .order_by(PipelineRun.created_at, PipelineRun.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        result = await self._session.scalars(statement)
        return list(result)

    async def list_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        session_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineRun]:
        """Filtered, paginated listing for the Runs API (SM-508).

        Newest first. Filters are optional and additive; no filter means no
        restriction on that dimension. ``session_id`` is a SM-601 foundation
        primitive -- the Runs API does not expose it as a public filter yet
        (SM-605).
        """

        statement = _apply_list_page_filters(
            select(PipelineRun),
            statuses=statuses,
            dataset_id=dataset_id,
            pipeline_type=pipeline_type,
            session_id=session_id,
        ).order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
        statement = statement.limit(limit).offset(offset)
        result = await self._session.scalars(statement)
        return list(result)

    async def count_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        session_id: UUID | None = None,
    ) -> int:
        """Total matching rows for :meth:`list_page`'s identical filter set (SM-508)."""

        statement = _apply_list_page_filters(
            select(func.count()).select_from(PipelineRun),
            statuses=statuses,
            dataset_id=dataset_id,
            pipeline_type=pipeline_type,
            session_id=session_id,
        )
        result = await self._session.scalar(statement)
        return int(result or 0)

    # -- SM-503 queue claiming primitives (ADR-0009 SS D) -------------------
    #
    # These queries implement the claim algorithm's building blocks. None of
    # them decide *when* to claim -- that orchestration lives in
    # services.pipeline_queue_claimer. Temporal eligibility always uses
    # PostgreSQL's own ``now()``, never a Python-side clock, per ADR-0009 SS 5.

    async def list_eligible_candidate_ids(self, *, limit: int) -> list[UUID]:
        """Small ordered batch of claimable run ids -- no lock, no ownership.

        One representative per serialization scope: the oldest eligible
        ``queued`` run for each non-null ``dataset_id``, plus the oldest
        eligible global (``dataset_id IS NULL``) run. This closes a
        head-of-line blocking gap a plain top-N-by-``created_at`` scan has --
        a congested dataset with many queued runs could otherwise fill the
        entire scan window and starve an unrelated, immediately-claimable
        dataset out of discovery entirely. Only one representative can ever
        be claimed per scope at a time anyway (same-dataset serialization,
        ADR-0009 SS E; at most one global, SS F), so later runs of the same
        scope contribute nothing to discovery while their oldest sibling is
        still queued.

        A second, distinct head-of-line gap remains even with one
        representative per scope: if more distinct scopes are congested
        (already RUNNING/CANCELLING, so every one of their queued
        representatives is certain to fail revalidation) than fit in
        ``limit``, those certain-to-fail representatives alone can fill the
        whole scan window and still starve a free scope. This query closes
        that gap too by excluding, at discovery time, any representative
        whose scope already has a *committed* RUNNING/CANCELLING conflict --
        same-dataset conflict or a global conflict for a dataset-scoped
        candidate; any dataset-scoped conflict or another global conflict for
        a global candidate. This mirrors ``_arbitrate_dataset_scoped`` /
        ``_arbitrate_global``'s committed-conflict checks exactly, but purely
        as a pre-filter -- it is evaluated on the same MVCC snapshot as the
        rest of this query, so it can go stale by the time a candidate is
        actually locked. That is fine: it is discovery only.

        This is a discovery/liveness optimization only, not a correctness
        mechanism: neither ``DISTINCT ON`` nor this conflict pre-filter is
        ever treated as an exclusion primitive. Every representative still
        goes through the unchanged per-candidate ``FOR UPDATE SKIP LOCKED``
        + advisory-lock arbitration + committed-state revalidation before it
        can be claimed -- a scope that looked free here but changed by the
        time its candidate transaction opens simply fails that later
        arbitration/revalidation and is skipped, exactly as before this
        pre-filter existed.

        Representatives are then ordered by ``(created_at, id)``, the single
        total ordering ADR-0009 SS 6 requires everywhere (claim order and
        fairness precedence alike).
        """

        conflict = aliased(PipelineRun)
        eligible = and_(
            PipelineRun.status == PipelineRunStatus.QUEUED,
            or_(
                PipelineRun.next_attempt_at.is_(None),
                PipelineRun.next_attempt_at <= func.now(),
            ),
        )
        no_known_conflict = ~exists().where(
            conflict.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
            or_(
                # dataset-scoped candidate: same dataset already committed, or
                # a global run already committed (blocks every dataset).
                and_(
                    PipelineRun.dataset_id.is_not(None),
                    conflict.dataset_id == PipelineRun.dataset_id,
                ),
                and_(PipelineRun.dataset_id.is_not(None), conflict.dataset_id.is_(None)),
                # global candidate: any dataset-scoped run already committed,
                # or a *different* global run already committed.
                and_(PipelineRun.dataset_id.is_(None), conflict.dataset_id.is_not(None)),
                and_(
                    PipelineRun.dataset_id.is_(None),
                    conflict.dataset_id.is_(None),
                    conflict.id != PipelineRun.id,
                ),
            ),
        )
        representatives = (
            select(PipelineRun.id, PipelineRun.created_at)
            .distinct(PipelineRun.dataset_id)
            .where(eligible, no_known_conflict)
            .order_by(PipelineRun.dataset_id, PipelineRun.created_at.asc(), PipelineRun.id.asc())
            .subquery()
        )
        statement = (
            select(representatives.c.id)
            .order_by(representatives.c.created_at.asc(), representatives.c.id.asc())
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def get_database_now(self) -> datetime:
        """PostgreSQL's own ``now()``, read inside the caller's current
        transaction -- the single time authority for a claim (ADR-0009 SS 5):
        eligibility, fairness, and the persisted ``started_at``/
        ``heartbeat_at`` this claim writes all derive from this same value,
        never from the application's Python clock."""

        result = await self._session.scalar(select(func.now()))
        assert result is not None  # noqa: S101 - PostgreSQL now() is never NULL
        return cast(datetime, result)

    async def get_eligible_for_update(self, run_id: UUID) -> PipelineRun | None:
        """Lock one candidate row with ``FOR UPDATE SKIP LOCKED``.

        Returns ``None`` -- never blocks, never raises -- when the row is
        already locked by a concurrent claimer, or is no longer ``queued``
        and temporally eligible by the time this runs (ADR-0009 SS 7: SKIP
        LOCKED protects only this one row's identity, not cross-row
        same-dataset/global conflicts -- those are the advisory-lock and
        revalidation queries below).
        """

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status == PipelineRunStatus.QUEUED,
                or_(
                    PipelineRun.next_attempt_at.is_(None),
                    PipelineRun.next_attempt_at <= func.now(),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def exists_dataset_conflict(self, dataset_id: UUID) -> bool:
        """Another RUNNING/CANCELLING run already owns this dataset (persisted)."""

        statement = select(
            exists().where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def exists_any_dataset_scoped_conflict(self) -> bool:
        """Any dataset-scoped run is RUNNING/CANCELLING anywhere (global claim step 4)."""

        statement = select(
            exists().where(
                PipelineRun.dataset_id.is_not(None),
                PipelineRun.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def exists_other_global_conflict(self, *, exclude_run_id: UUID) -> bool:
        """Another global (dataset_id IS NULL) run is already RUNNING/CANCELLING."""

        statement = select(
            exists().where(
                PipelineRun.dataset_id.is_(None),
                PipelineRun.id != exclude_run_id,
                PipelineRun.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def exists_eligible_global_with_precedence(
        self,
        *,
        before_created_at: datetime,
        before_id: UUID,
    ) -> bool:
        """A QUEUED, temporally-eligible global run precedes ``(created_at, id)``.

        The fairness starvation guard (ADR-0009 SS D "Global barrier fairness"):
        used only to withhold a dataset-scoped candidate, never to decide a
        global candidate's own claim. Row-value comparison gives a single
        deterministic total order, resolving created_at ties by id (SS Q).
        """

        statement = select(
            exists().where(
                PipelineRun.dataset_id.is_(None),
                PipelineRun.status == PipelineRunStatus.QUEUED,
                or_(
                    PipelineRun.next_attempt_at.is_(None),
                    PipelineRun.next_attempt_at <= func.now(),
                ),
                tuple_(PipelineRun.created_at, PipelineRun.id)
                < tuple_(literal(before_created_at), literal(before_id)),
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def try_advisory_lock_shared(self, key: int) -> bool:
        """Non-blocking ``pg_try_advisory_xact_lock_shared`` on this session's
        current transaction. Released automatically at COMMIT/ROLLBACK."""

        result = await self._session.execute(
            text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": key}
        )
        return bool(result.scalar())

    async def try_advisory_lock_exclusive(self, key: int) -> bool:
        """Non-blocking ``pg_try_advisory_xact_lock`` on this session's current
        transaction. Released automatically at COMMIT/ROLLBACK."""

        result = await self._session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        )
        return bool(result.scalar())

    async def find_latest_forget_for_source_except(
        self,
        *,
        source_id: UUID,
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Most recent FORGET run (any status) that targeted this source.

        Used to recover the *intent* that put a source into ``deleting`` when
        no RUNNING owner remains (e.g. it crashed): a retry may only resume a
        target whose persisted ``payload_hash`` matches this request's.
        """

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.source_id == source_id,
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.id != excluded_run_id,
                PipelineRun.error_code.is_distinct_from(FORGET_TARGET_CONFLICT_ERROR_CODE),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_running_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Detect an incompatible in-flight FORGET touching this dataset or its sources.

        Covers a running dataset-scoped run (``run.dataset_id``), a running
        source-scoped run for any source that belongs to this dataset
        (``run.source_id``), and a running everything-scoped run (which has
        both fields ``NULL`` and, being global, is always a potential
        conflict for any dataset), so dataset/everything forget can avoid
        disputing post-commit drain/storage/finalization with it (FR-090
        concurrency).
        """

        conditions = [
            PipelineRun.dataset_id == dataset_id,
            and_(PipelineRun.dataset_id.is_(None), PipelineRun.source_id.is_(None)),
        ]
        if source_ids:
            conditions.append(PipelineRun.source_id.in_(source_ids))
        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.status == PipelineRunStatus.RUNNING,
                PipelineRun.id != excluded_run_id,
                or_(*conditions),
            )
            .order_by(PipelineRun.created_at, PipelineRun.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_latest_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None:
        """Most recent FORGET run (any status) that targeted this dataset.

        Same widened match as :meth:`find_running_forget_for_dataset_except`
        (dataset-scoped, source-scoped on one of its sources, or
        everything-scoped) but ignores ``status``, so it can recover the
        intent of a *finished* (e.g. ``failed``) prior attempt when no
        RUNNING owner remains.
        """

        conditions = [
            PipelineRun.dataset_id == dataset_id,
            and_(PipelineRun.dataset_id.is_(None), PipelineRun.source_id.is_(None)),
        ]
        if source_ids:
            conditions.append(PipelineRun.source_id.in_(source_ids))
        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.pipeline_type == PipelineType.FORGET,
                PipelineRun.id != excluded_run_id,
                PipelineRun.error_code.is_distinct_from(FORGET_TARGET_CONFLICT_ERROR_CODE),
                or_(*conditions),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def heartbeat_if_owned(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        attempt: int,
    ) -> bool:
        """Guarded ``heartbeat_at = now()`` update (ADR-0009 SS H, SM-505 SS 8).

        Short, standalone statement -- callers commit it immediately in its
        own transaction, never inside a step's business transaction or across
        a ``SELECT ... FOR UPDATE``. Fencing uses ``(run_id, worker_id,
        attempt)``, the same triple the engine uses (SS 16), not
        ``worker_id`` alone: the same process-boot ``worker_id`` is reused
        across every run it ever claims, so ``attempt`` is what distinguishes
        a still-owned claim from one already superseded by a reclaim.

        Only ``RUNNING``/``CANCELLING`` rows are eligible -- ``QUEUED``
        (including a run awaiting ``next_attempt_at``) never receives a
        heartbeat (ADR-0009 SS H). Returns ``True`` when a row was updated,
        ``False`` when ownership/status no longer match -- the caller must
        treat ``False`` as ordinary "stop heartbeating this run", never as an
        error.
        """

        statement = (
            update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.worker_id == worker_id,
                PipelineRun.attempt == attempt,
                PipelineRun.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
            )
            .values(heartbeat_at=func.now())
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return bool(result.rowcount)

    # -- SM-507 stale recovery primitives (ADR-0009 SS I) --------------------
    #
    # Discovery/claim only -- these never decide *whether* a candidate is
    # still stale after locking (Python-side re-check in
    # services.pipeline_recovery, mirroring the SM-506 graph_outbox claim
    # pattern) and never execute business logic.

    async def list_stale_candidate_ids(
        self,
        *,
        stale_after_seconds: float,
        include_null_heartbeat: bool,
        limit: int,
    ) -> list[UUID]:
        """RUNNING/CANCELLING candidates whose heartbeat already satisfies
        the stale predicate (ADR-0009 SS H/SS I): ``heartbeat_at`` strictly
        older than ``now() - stale_after_seconds``.

        ``include_null_heartbeat`` additionally matches a legacy pre-B5 run
        with no heartbeat at all -- the startup pass only (ADR-0009 SS I
        "Startup behavior", backlog SS 6): a NULL heartbeat can only mean
        "abandoned by a now-dead process" at process boot, before this
        process's own worker has claimed anything. A future periodic
        in-process pass must never set this ``True`` while B4 direct-RUNNING
        writers still exist (backlog SS 6).

        ``created_at`` is never used as staleness evidence (ADR-0009 SS I,
        backlog SS 5.1) -- only ``status`` + ``heartbeat_at``, both read
        against PostgreSQL's own ``now()``.
        """

        stale_cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, stale_after_seconds)
        heartbeat_condition = PipelineRun.heartbeat_at < stale_cutoff
        if include_null_heartbeat:
            heartbeat_condition = or_(PipelineRun.heartbeat_at.is_(None), heartbeat_condition)
        statement = (
            select(PipelineRun.id)
            .where(
                PipelineRun.status.in_((PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING)),
                heartbeat_condition,
            )
            .order_by(PipelineRun.created_at, PipelineRun.id)
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def get_stale_for_update(self, run_id: UUID) -> PipelineRun | None:
        """Lock exactly one candidate row, identified by id, with ``FOR
        UPDATE SKIP LOCKED`` (ADR-0009 SS I/SS 17: race-safe by construction,
        even though startup recovery is normally a single process).

        No status/staleness predicate here -- the caller re-validates both
        against the just-locked, guaranteed-committed row (see
        ``services.pipeline_recovery``), the same split responsibility
        SM-506's ``GraphOutboxRepository.claim_one`` already established.
        Returns ``None`` when the row is already locked by a concurrent
        recovery pass, or no longer exists.
        """

        statement = (
            select(PipelineRun).where(PipelineRun.id == run_id).with_for_update(skip_locked=True)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.idempotency_key == idempotency_key)
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    # -- SM-515 dataset administrative delete primitives (ADR-0010) ---------

    async def find_nonterminal_dataset_delete_for_dataset(
        self, dataset_id: UUID
    ) -> PipelineRun | None:
        """The one currently QUEUED/RUNNING/CANCELLING ``DATASET_DELETE`` run
        for this dataset, if any (ADR-0010 D12/D21 delete-intent barrier and
        repeated-DELETE convergence)."""

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
                PipelineRun.status.in_(
                    (
                        PipelineRunStatus.QUEUED,
                        PipelineRunStatus.RUNNING,
                        PipelineRunStatus.CANCELLING,
                    )
                ),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_nonterminal_dataset_delete_for_dataset_for_update(
        self, dataset_id: UUID
    ) -> PipelineRun | None:
        """Row-locked variant of :meth:`find_nonterminal_dataset_delete_for_dataset`,
        used while already holding the Dataset row lock inside the same
        transaction that may create a brand-new ``DATASET_DELETE`` run
        (ADR-0010 D8/D21 -- TOCTOU-safe convergence)."""

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
                PipelineRun.status.in_(
                    (
                        PipelineRunStatus.QUEUED,
                        PipelineRunStatus.RUNNING,
                        PipelineRunStatus.CANCELLING,
                    )
                ),
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_latest_dataset_delete_for_dataset(self, dataset_id: UUID) -> PipelineRun | None:
        """Most recent ``DATASET_DELETE`` run (any status) for this dataset --
        used to surface a public-safe ``run_id`` when a repeated ``DELETE``
        finds a terminal FAILED/CANCELLED lineage awaiting manual retry
        (ADR-0010 D21/D23)."""

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def find_compatible_destructive_lineage(
        self, *, dataset_id: UUID, source_id: UUID
    ) -> PipelineRun | None:
        """ADR-0011 D34 Case B: the most recent durable ``forget`` or
        ``dataset_delete`` run whose scope provably includes this exact
        Source **and** which has actually reached the step that mutates
        ``Source.status`` to ``deleting`` -- mirrors
        :meth:`exists_administrative_delete_ownership`'s discipline (ADR-0010
        D28: "don't infer ownership from status alone"). Mere run existence
        is not proof of an actual `DELETING`-causing effect; a `queued` run
        that never executed, or one whose scope only nominally overlaps,
        must never be mistaken for the owner of a missing local object.

        Scope match: a FORGET run is compatible if it targeted this exact
        ``source_id``, this exact ``dataset_id`` (dataset-scope, with
        ``source_id IS NULL``), or is an EVERYTHING-scope run (both
        ``dataset_id``/``source_id`` ``NULL``) -- the same widened match
        :meth:`find_running_forget_for_dataset_except` already uses. A
        DATASET_DELETE run is compatible if it targeted this exact
        ``dataset_id``. FORGET runs marked
        ``FORGET_TARGET_CONFLICT_ERROR_CODE`` are excluded -- they never
        touched authoritative state (same exclusion as
        :meth:`find_latest_forget_for_source_except`/
        :meth:`find_latest_forget_for_dataset_except`).
        """

        forget_scope = or_(
            PipelineRun.source_id == source_id,
            and_(PipelineRun.dataset_id == dataset_id, PipelineRun.source_id.is_(None)),
            and_(PipelineRun.dataset_id.is_(None), PipelineRun.source_id.is_(None)),
        )
        mutation_step_succeeded = (
            select(PipelineStep.id)
            .where(
                PipelineStep.run_id == PipelineRun.id,
                PipelineStep.status == PipelineStepStatus.SUCCEEDED,
                or_(
                    *(
                        and_(
                            PipelineRun.pipeline_type == pipeline_type,
                            PipelineStep.name == step_name,
                        )
                        for pipeline_type, step_name in (
                            AUTHORITATIVE_MUTATION_STEP_NAMES_BY_PIPELINE_TYPE.items()
                        )
                    )
                ),
            )
            .exists()
        )
        statement = (
            select(PipelineRun)
            .where(
                or_(
                    and_(
                        PipelineRun.pipeline_type == PipelineType.FORGET,
                        forget_scope,
                        PipelineRun.error_code.is_distinct_from(FORGET_TARGET_CONFLICT_ERROR_CODE),
                    ),
                    and_(
                        PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
                        PipelineRun.dataset_id == dataset_id,
                    ),
                ),
                mutation_step_succeeded,
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(PipelineRun | None, result)

    async def authoritative_mutation_succeeded(
        self, run_id: UUID, *, pipeline_type: PipelineType
    ) -> bool:
        """ADR-0011 D31 sixth amendment / D43: the run-claim-level half of
        the predicate :meth:`find_compatible_destructive_lineage` already
        applies at the Source-classification level -- whether THIS run's own
        authoritative destructive mutation step has durably `succeeded`.
        Case-B Source membership alone is never sufficient to make a run
        claim-eligible during STORAGE_CONVERGING (D31); this is the direct,
        run-scoped check the worker's claim path uses once it already holds
        the candidate row locked (so ``pipeline_type`` is already known,
        avoiding a redundant join back to ``pipeline_runs``). A
        ``pipeline_type`` with no destructive authoritative-mutation step at
        all (``remember``, ``cognify``, ``improve``) always returns
        ``False`` -- this predicate is only ever meaningful for
        ``forget``/``dataset_delete``.
        """

        step_name = AUTHORITATIVE_MUTATION_STEP_NAMES_BY_PIPELINE_TYPE.get(pipeline_type)
        if step_name is None:
            return False
        statement = select(
            exists().where(
                PipelineStep.run_id == run_id,
                PipelineStep.status == PipelineStepStatus.SUCCEEDED,
                PipelineStep.name == step_name,
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def exists_administrative_delete_ownership(self, dataset_id: UUID) -> bool:
        """ADR-0010 D28's frozen ``administratively_deleting`` predicate,
        minus the ``Dataset.status == DELETING`` half (the caller already
        holds/observes the Dataset row): whether *any* ``DATASET_DELETE`` run
        for this dataset has a persisted ``begin_delete`` step that reached
        ``SUCCEEDED``. Mere run existence is deliberately NOT sufficient --
        see ADR-0010 D28's counterexample (a `DATASET_DELETE` cancelled while
        still QUEUED must never poison a later, unrelated Forget-owned
        ``DELETING`` cycle on the same dataset)."""

        statement = select(
            exists().where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.pipeline_type == PipelineType.DATASET_DELETE,
                PipelineStep.run_id == PipelineRun.id,
                PipelineStep.name == "begin_delete",
                PipelineStep.status == PipelineStepStatus.SUCCEEDED,
            )
        )
        result = await self._session.scalar(statement)
        return bool(result)

    async def list_incompatible_queued_for_dataset_for_update(
        self, *, dataset_id: UUID, exclude_run_id: UUID
    ) -> list[PipelineRun]:
        """Every still-``QUEUED`` run on this dataset that is NOT itself a
        ``DATASET_DELETE`` run, row-locked -- what ``begin_delete``
        administratively cancels (ADR-0010 D14/D16). Never includes another
        ``DATASET_DELETE`` run: the create-time Dataset-row-locked
        resolve-or-create sequence (ADR-0010 D21) already guarantees at most
        one nonterminal ``DATASET_DELETE`` exists per dataset, and this run's
        own id is excluded defensively regardless (ADR-0010 D16 "never
        cancel the executing DATASET_DELETE run itself")."""

        statement = (
            select(PipelineRun)
            .where(
                PipelineRun.dataset_id == dataset_id,
                PipelineRun.status == PipelineRunStatus.QUEUED,
                PipelineRun.pipeline_type != PipelineType.DATASET_DELETE,
                PipelineRun.id != exclude_run_id,
            )
            .order_by(PipelineRun.created_at, PipelineRun.id)
            .with_for_update()
        )
        result = await self._session.scalars(statement)
        return list(result)
