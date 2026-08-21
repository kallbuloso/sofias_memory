"""PostgreSQL queue claiming for durable PipelineRun rows (ADR-0009 SS D).

SM-503 scope only: the transactional claim mechanism itself --
``FOR UPDATE SKIP LOCKED`` for row identity, PostgreSQL transaction-level
advisory-lock arbitration for same-dataset/global exclusion, and persisted
conflict revalidation. No worker loop, no polling, no pipeline execution, no
Runs API, no wait contract, no stale recovery -- those belong to SM-504
onward. This module is the primitive SM-505's worker will consume.

Claim result: no candidate available or claimable is the normal outcome and
returns ``None``. Exceptions are reserved for real infrastructure failure
(database unavailable) or an unexpected/impossible invariant violation, never
for "row was locked" or "conflict found" -- those are ordinary control flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.advisory_lock_keys import (
    GLOBAL_BARRIER_KEY,
    dataset_lock_key,
)
from sofias_memory.infrastructure.postgres.models import PipelineRun
from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import (
    PipelineRunRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.pipeline_lifecycle import transition_run

DEFAULT_CANDIDATE_SCAN_LIMIT = 20
"""Bounded per-call candidate scan; an unclaimable oldest run must never stall
the whole queue (ADR-0009 SS 8). Not a configuration framework -- a documented
constant, adjustable in code if a future story proves it insufficient."""


def new_worker_id() -> str:
    """Opaque per-boot worker identity (ADR-0009 SS G).

    No hostname, PID, file path, or secret -- safe to surface as a
    correlation token in a future Runs API (SM-508).
    """

    return f"wk-{uuid4()}"


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """Minimal, stable result the future worker (SM-505) needs to dispatch a run.

    Deliberately not a public API/Runs schema -- just enough identity for an
    in-process caller to proceed, or re-read the full row if it needs more.
    """

    run_id: UUID
    dataset_id: UUID | None
    pipeline_type: PipelineType
    worker_id: str
    attempt: int

    @classmethod
    def from_model(cls, run: PipelineRun) -> ClaimedRun:
        assert run.worker_id is not None  # noqa: S101 - set by transition_run(RUNNING)
        return cls(
            run_id=run.id,
            dataset_id=run.dataset_id,
            pipeline_type=run.pipeline_type,
            worker_id=run.worker_id,
            attempt=run.attempt,
        )


class PipelineRunClaimer:
    """Implements ADR-0009 SS D's claim algorithm end to end.

    Each candidate attempt runs in its own short PostgreSQL transaction
    (its own :class:`PostgresUnitOfWork`), so a candidate that cannot be
    claimed is abandoned by simply never committing -- no explicit rollback
    call needed, and no lock (row or advisory) is ever held across candidate
    attempts or into caller-side pipeline execution.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        candidate_scan_limit: int = DEFAULT_CANDIDATE_SCAN_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._candidate_scan_limit = candidate_scan_limit

    async def try_claim_one(self, *, worker_id: str) -> ClaimedRun | None:
        """Attempt to claim exactly one eligible run.

        Returns ``None`` when no candidate in this scan could be claimed --
        that is ordinary, expected behavior, not an error.
        """

        async with PostgresUnitOfWork(self._session_factory) as scan_uow:
            candidate_ids = await scan_uow.pipeline_runs.list_eligible_candidate_ids(
                limit=self._candidate_scan_limit
            )

        for candidate_id in candidate_ids:
            claimed = await self._try_claim_candidate(candidate_id, worker_id=worker_id)
            if claimed is not None:
                return claimed
        return None

    async def _try_claim_candidate(
        self,
        candidate_id: UUID,
        *,
        worker_id: str,
    ) -> ClaimedRun | None:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            repository = uow.pipeline_runs
            run = await repository.get_eligible_for_update(candidate_id)
            if run is None:
                # Lost the row to a concurrent claimer (SKIP LOCKED), or it is
                # no longer queued/eligible by the time we reached it.
                return None

            if run.dataset_id is not None:
                ok = await self._arbitrate_dataset_scoped(repository, run)
            else:
                ok = await self._arbitrate_global(repository, run)

            if not ok:
                return None  # abandon; __aexit__ rolls back, nothing committed

            # ADR-0009 SS 5: PostgreSQL is the time authority for the claim --
            # the same now() this transaction already used for eligibility
            # and fairness is reused for started_at/heartbeat_at, never the
            # application's Python clock.
            db_now = await repository.get_database_now()
            transition_run(run, PipelineRunStatus.RUNNING, now=db_now, worker_id=worker_id)
            await uow.commit()
            return ClaimedRun.from_model(run)

    @staticmethod
    async def _arbitrate_dataset_scoped(
        repository: PipelineRunRepository,
        run: PipelineRun,
    ) -> bool:
        """ADR-0009 SS D dataset-scoped claim: fairness, shared+exclusive
        advisory locks, then persisted-conflict revalidation."""

        assert run.dataset_id is not None  # noqa: S101 - caller already checked

        # Fairness precheck (SS D "Global barrier fairness"): a pending
        # eligible global run older than this candidate withholds it, before
        # any lock is even attempted.
        if await repository.exists_eligible_global_with_precedence(
            before_created_at=run.created_at,
            before_id=run.id,
        ):
            return False

        if not await repository.try_advisory_lock_shared(GLOBAL_BARRIER_KEY):
            return False
        if not await repository.try_advisory_lock_exclusive(dataset_lock_key(run.dataset_id)):
            return False

        # Revalidate against committed state while both locks are held --
        # this is what closes the race a lock-free NOT EXISTS check cannot
        # (ADR-0009 SS D).
        if await repository.exists_dataset_conflict(run.dataset_id):
            return False

        return not await repository.exists_other_global_conflict(exclude_run_id=run.id)

    @staticmethod
    async def _arbitrate_global(
        repository: PipelineRunRepository,
        run: PipelineRun,
    ) -> bool:
        """ADR-0009 SS D global claim: exclusive barrier lock, then confirm no
        dataset-scoped or other global run is already committed RUNNING/CANCELLING."""

        assert run.dataset_id is None  # noqa: S101 - caller already checked

        if not await repository.try_advisory_lock_exclusive(GLOBAL_BARRIER_KEY):
            return False

        if await repository.exists_any_dataset_scoped_conflict():
            return False

        return not await repository.exists_other_global_conflict(exclude_run_id=run.id)
