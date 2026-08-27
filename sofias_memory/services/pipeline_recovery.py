"""Startup stale PipelineRun/PipelineStep recovery (ADR-0009 SS I, SM-507).

Recovery is pure state-reconciliation: it never calls a step's ``execute()``/
``compensate()``, never talks to an LLM/embedding/HTTP/Neo4j dependency, and
never writes ``graph_outbox``. Its only job is moving a ``RUNNING``/
``CANCELLING`` run abandoned by a crashed process into a state the ordinary
claim path (SM-503's :class:`PipelineRunClaimer`) and engine (SM-504's
:class:`PipelineEngine`) can pick up normally -- or, when that is not safe, a
terminal state. Any actual re-execution of business logic happens later,
only through a normal claim.

This module also carries SM-507's inherited B4 debt: the four still-
synchronous public write services (``remember``/``cognify``/``improve``/
``forget``) create ``PipelineRun`` rows directly ``RUNNING``, with zero
``PipelineStep`` rows and no heartbeat (Gate A, confirmed by direct
inspection of all four services). A legacy run like that has no durable B5
execution plan for the engine to resume, so recovery always terminalizes it
safely instead of ever requeuing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.pipelines.errors import (
    CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
    CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE,
    WORKER_LOST_ERROR_CODE,
)
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    CancellationRecoveryOutcome,
    PipelineCancellationRecoveryContext,
    PipelineRegistry,
    PipelineStepDefinition,
    UnknownPipelineTypeError,
)
from sofias_memory.pipelines.retry_policy import RetryPolicy
from sofias_memory.services.pipeline_lifecycle import (
    apply_run_progress,
    transition_run,
    transition_step,
)

if TYPE_CHECKING:
    from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep

logger = get_logger(__name__)

DEFAULT_RECOVERY_SCAN_LIMIT = 100
"""Code-level batch size (backlog SS 18): no new configuration surface for a
single-replica startup pass. Batches repeat until no stale candidate
remains -- convergent because every recovered candidate leaves the
RUNNING/CANCELLING stale set (backlog SS 18/SS 31)."""

CONFIG_FINGERPRINT_MISMATCH_MESSAGE = (
    "Configuration changed since this run was claimed; it cannot be safely resumed."
)
WORKER_LOST_NO_ATTEMPTS_MESSAGE = "The worker that owned this run was lost and no attempts remain."
WORKER_LOST_NO_PLAN_MESSAGE = (
    "This run has no durable step plan to resume (legacy pre-B5 execution)."
)
WORKER_LOST_NO_LIVENESS_EVIDENCE_MESSAGE = (
    "This run has no valid liveness evidence (heartbeat) to determine whether it "
    "is genuinely stale, even though a step plan exists."
)
WORKER_LOST_INVARIANT_MESSAGE = "Persisted step state is inconsistent for this run."
CANCEL_RECOVERY_AMBIGUOUS_MESSAGE = (
    "Cancellation could not be safely confirmed for this run's in-flight step."
)


def _is_stale(
    run: PipelineRun,
    *,
    now: datetime,
    stale_after_seconds: float,
    include_null_heartbeat: bool,
) -> bool:
    """Python-side mirror of the SQL discovery predicate, re-evaluated
    against the just-locked, guaranteed-committed row (ADR-0009 SS H/SS I;
    same split responsibility as SM-506's ``_is_claim_eligible``)."""

    if run.status not in (PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING):
        return False
    if run.heartbeat_at is None:
        return include_null_heartbeat
    cutoff = now - timedelta(seconds=stale_after_seconds)
    return run.heartbeat_at < cutoff


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Observability-only summary of one startup pass (not a public schema)."""

    recovered: int


class PipelineRecoveryService:
    """Startup-only stale-run reconciliation (ADR-0009 SS I "Startup behavior").

    One call graph, reused for every candidate: discover -> lock (``FOR
    UPDATE SKIP LOCKED``) -> re-validate staleness -> reconcile -> commit, in
    a short transaction per candidate, never holding a lock across a sleep or
    an external call (SS 35).
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: PipelineRegistry,
        *,
        stale_after_seconds: int,
        config_fingerprint: str,
        retry_policy: RetryPolicy | None = None,
        scan_limit: int = DEFAULT_RECOVERY_SCAN_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._stale_after_seconds = float(stale_after_seconds)
        self._config_fingerprint = config_fingerprint
        self._retry_policy = retry_policy or RetryPolicy()
        self._scan_limit = scan_limit

    async def recover_startup(self) -> int:
        """Reconcile every stale RUNNING/CANCELLING run before the worker's
        poll loop starts claiming (ADR-0009 SS T/SS I "Startup behavior").

        Recognizes a legacy NULL heartbeat as abandoned -- valid only for
        this startup pass (backlog SS 6): a NULL heartbeat can only mean "the
        process that would have heartbeated it is not this one and never
        will be again", which is true at boot but not true of an in-flight
        B4 synchronous request this same process is currently serving.
        Idempotent: a second call with nothing left stale returns ``0``
        without touching anything (backlog SS 31).
        """

        total = 0
        while True:
            recovered = await self._recover_batch(include_null_heartbeat=True)
            total += recovered
            if recovered == 0:
                return total

    async def _recover_batch(self, *, include_null_heartbeat: bool) -> int:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            candidate_ids = await uow.pipeline_runs.list_stale_candidate_ids(
                stale_after_seconds=self._stale_after_seconds,
                include_null_heartbeat=include_null_heartbeat,
                limit=self._scan_limit,
            )

        recovered = 0
        for candidate_id in candidate_ids:
            if await self._recover_one(candidate_id, include_null_heartbeat=include_null_heartbeat):
                recovered += 1
        return recovered

    async def _recover_one(self, run_id: UUID, *, include_null_heartbeat: bool) -> bool:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.get_stale_for_update(run_id)
            if run is None:
                return False

            db_now = await uow.pipeline_runs.get_database_now()
            if not _is_stale(
                run,
                now=db_now,
                stale_after_seconds=self._stale_after_seconds,
                include_null_heartbeat=include_null_heartbeat,
            ):
                # Lost the SKIP LOCKED race to a concurrent recovery pass
                # whose commit already resolved it, or heartbeat/status moved
                # on between discovery and this lock (ADR-0009 SS 49).
                return False

            steps = await uow.pipeline_steps.list_for_run(run.id)

            if run.status == PipelineRunStatus.CANCELLING:
                await self._recover_cancelling(uow, run, steps, now=db_now)
            else:
                self._recover_running(run, steps, now=db_now)

            await uow.commit()
            reconciled_fields: dict[str, object] = {
                "run_id": str(run.id),
                "pipeline_type": str(run.pipeline_type),
                "final_status": str(run.status),
            }
            if run.error_code is not None:
                reconciled_fields["error_code"] = run.error_code
            logger.info("pipeline_recovery_run_reconciled", **reconciled_fields)
            return True

    # -- RUNNING (ADR-0009 SS I "RUNNING stale") -----------------------------

    def _recover_running(
        self,
        run: PipelineRun,
        steps: list[PipelineStep],
        *,
        now: datetime,
    ) -> None:
        if run.config_fingerprint != self._config_fingerprint:
            self._fail_running_step(
                steps,
                now=now,
                code=CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE,
                message=CONFIG_FINGERPRINT_MISMATCH_MESSAGE,
            )
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE,
                error_message=CONFIG_FINGERPRINT_MISMATCH_MESSAGE,
            )
            return

        if run.heartbeat_at is None:
            # ADR-0009 SS I "Pre-B5 legacy rollout exception" (SM-507): a
            # NULL heartbeat is never valid liveness evidence for the normal
            # B5 stale-reclaim decision -- it applies uniformly whether this
            # is genuine pre-B5 legacy debt (zero PipelineStep rows, Gate A:
            # every current B4 writer creates PipelineRun rows directly
            # RUNNING with no durable plan) or a NULL heartbeat with steps
            # present (an invalid/unproven state a correctly-functioning B5
            # claim can never produce, since claim always sets heartbeat_at
            # in the same transaction as RUNNING, SS D/SS G). Neither case
            # may return to QUEUED -- both terminalize fail-safe here,
            # regardless of the attempt ceiling.
            message = (
                WORKER_LOST_NO_PLAN_MESSAGE
                if not steps
                else WORKER_LOST_NO_LIVENESS_EVIDENCE_MESSAGE
            )
            self._fail_running_step(steps, now=now, code=WORKER_LOST_ERROR_CODE, message=message)
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=WORKER_LOST_ERROR_CODE,
                error_message=message,
            )
            return

        if not self._retry_policy.has_attempts_remaining(run.attempt):
            self._fail_running_step(
                steps,
                now=now,
                code=WORKER_LOST_ERROR_CODE,
                message=WORKER_LOST_NO_ATTEMPTS_MESSAGE,
            )
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=WORKER_LOST_ERROR_CODE,
                error_message=WORKER_LOST_NO_ATTEMPTS_MESSAGE,
            )
            return

        if not steps:
            # Defensive fail-safe only: heartbeat is set (checked above) but
            # no plan exists -- a correctly-functioning B5 claim always
            # materializes >=1 step at submission (SS B/SS C), so this
            # should not occur in practice. Never fabricate a resume.
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=WORKER_LOST_ERROR_CODE,
                error_message=WORKER_LOST_NO_PLAN_MESSAGE,
            )
            return

        running_steps = [step for step in steps if step.status == PipelineStepStatus.RUNNING]
        succeeded_count = sum(1 for step in steps if step.status == PipelineStepStatus.SUCCEEDED)

        if len(running_steps) > 1 or succeeded_count == len(steps):
            # Impossible under a correctly-functioning claim/engine (SS 14/SS
            # 24): the engine is strictly sequential, and a run whose last
            # step succeeded finalizes SUCCEEDED in that same transaction.
            # Fail safe rather than guess or fabricate success.
            self._fail_running_step(
                steps,
                now=now,
                code=WORKER_LOST_ERROR_CODE,
                message=WORKER_LOST_INVARIANT_MESSAGE,
            )
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=WORKER_LOST_ERROR_CODE,
                error_message=WORKER_LOST_INVARIANT_MESSAGE,
            )
            return

        if len(running_steps) == 1:
            transition_step(running_steps[0], PipelineStepStatus.QUEUED, now=now)

        delay = self._retry_policy.backoff_seconds(run.attempt)
        transition_run(
            run,
            PipelineRunStatus.QUEUED,
            now=now,
            next_attempt_at=now + timedelta(seconds=delay),
        )

    @staticmethod
    def _fail_running_step(
        steps: list[PipelineStep],
        *,
        now: datetime,
        code: str,
        message: str,
    ) -> None:
        """No step remains RUNNING under a terminal run (ADR-0009 SS 14)."""

        error = {"code": code, "message": message}
        for step in steps:
            if step.status == PipelineStepStatus.RUNNING:
                transition_step(step, PipelineStepStatus.FAILED, now=now, error=error)

    # -- CANCELLING (ADR-0009 SS I "CANCELLING stale") -----------------------

    async def _recover_cancelling(
        self,
        uow: PostgresUnitOfWork,
        run: PipelineRun,
        steps: list[PipelineStep],
        *,
        now: datetime,
    ) -> None:
        if run.heartbeat_at is None:
            # ADR-0009 SS I "Pre-B5 legacy rollout exception" (SM-507): a
            # NULL heartbeat is never valid liveness evidence, whether this
            # is genuine pre-B5 legacy debt (zero PipelineStep rows -- no
            # orphaned step to classify into case A/B/C, no durable evidence
            # to prove a safe CANCELLED outcome) or a NULL heartbeat with
            # steps present (a state a correctly-functioning B5 claim can
            # never produce). Never fabricate CANCELLED just to preserve
            # cancellation intent when no durable plan/evidence exists --
            # this is a case-C outcome, not a vacuous case A. Any RUNNING
            # step is failed, never left RUNNING under this terminal run.
            self._fail_running_step(
                steps,
                now=now,
                code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
                message=CANCEL_RECOVERY_AMBIGUOUS_MESSAGE,
            )
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
                error_message=CANCEL_RECOVERY_AMBIGUOUS_MESSAGE,
            )
            return

        if not steps:
            # Defensive fail-safe only: heartbeat is set (checked above) but
            # no plan exists -- should not occur for a correctly-functioning
            # B5 claim (SS B/SS C always materialize >=1 step). Never assume
            # a vacuous case A just because there is nothing to classify.
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
                error_message=CANCEL_RECOVERY_AMBIGUOUS_MESSAGE,
            )
            return

        running_steps = [step for step in steps if step.status == PipelineStepStatus.RUNNING]

        if len(running_steps) > 1:
            self._fail_running_step(
                steps,
                now=now,
                code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
                message=WORKER_LOST_INVARIANT_MESSAGE,
            )
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
                error_message=WORKER_LOST_INVARIANT_MESSAGE,
            )
            return

        if not running_steps:
            self._cancel_queued_steps(steps, now=now)
            if all(
                step.status in (PipelineStepStatus.SUCCEEDED, PipelineStepStatus.CANCELLED)
                for step in steps
            ):
                apply_run_progress(run, 1.0)
            transition_run(run, PipelineRunStatus.CANCELLED, now=now)
            return

        orphan = running_steps[0]
        step_def = self._find_step_definition(run.pipeline_type, orphan.name)
        mode = (
            step_def.cancellation_recovery_mode
            if step_def is not None
            else CancellationRecoveryMode.AMBIGUOUS
        )

        if mode == CancellationRecoveryMode.ATOMIC:
            self._finalize_cancelled(run, orphan, steps, now=now)
            return

        if mode == CancellationRecoveryMode.RECONCILABLE:
            assert step_def is not None  # noqa: S101 - RECONCILABLE always has a definition
            assert step_def.cancellation_reconcile is not None  # noqa: S101 - enforced at __post_init__
            context = self._build_cancellation_context(run, orphan)
            try:
                outcome = await step_def.cancellation_reconcile(context, uow)
            except Exception as exc:  # noqa: BLE001 - fail-safe: never let a bad callback crash startup
                logger.warning(
                    "pipeline_recovery_cancellation_reconcile_failed",
                    run_id=str(run.id),
                    step_name=orphan.name,
                    exception_type=type(exc).__name__,
                )
                outcome = CancellationRecoveryOutcome.INCONCLUSIVE

            if outcome == CancellationRecoveryOutcome.SAFE:
                self._finalize_cancelled(run, orphan, steps, now=now)
                return

        # Case C: AMBIGUOUS, or RECONCILABLE that could not prove safety.
        # Cancellation intent is still recorded via the stable error code,
        # never silently reported CANCELLED. Remaining QUEUED steps are left
        # untouched (unexecuted history), matching the engine's own existing
        # precedent for a permanent failure observed while CANCELLING
        # (PipelineEngine._persist_failure_while_cancelling).
        error = {
            "code": CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
            "message": CANCEL_RECOVERY_AMBIGUOUS_MESSAGE,
        }
        transition_step(orphan, PipelineStepStatus.FAILED, now=now, error=error)
        transition_run(
            run,
            PipelineRunStatus.FAILED,
            now=now,
            error_code=CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
            error_message=CANCEL_RECOVERY_AMBIGUOUS_MESSAGE,
        )

    def _finalize_cancelled(
        self,
        run: PipelineRun,
        orphan: PipelineStep,
        steps: list[PipelineStep],
        *,
        now: datetime,
    ) -> None:
        transition_step(orphan, PipelineStepStatus.CANCELLED, now=now)
        self._cancel_queued_steps(steps, now=now)
        transition_run(run, PipelineRunStatus.CANCELLED, now=now)

    @staticmethod
    def _cancel_queued_steps(steps: list[PipelineStep], *, now: datetime) -> None:
        for step in steps:
            if step.status == PipelineStepStatus.QUEUED:
                transition_step(step, PipelineStepStatus.CANCELLED, now=now)

    def _find_step_definition(
        self,
        pipeline_type: PipelineType,
        step_name: str,
    ) -> PipelineStepDefinition | None:
        try:
            definition = self._registry.get(pipeline_type)
        except UnknownPipelineTypeError:
            return None
        for step_def in definition.steps:
            if step_def.name == step_name:
                return step_def
        return None

    @staticmethod
    def _build_cancellation_context(
        run: PipelineRun,
        step: PipelineStep,
    ) -> PipelineCancellationRecoveryContext:
        return PipelineCancellationRecoveryContext(
            run_id=run.id,
            pipeline_type=run.pipeline_type,
            dataset_id=run.dataset_id,
            source_id=run.source_id,
            run_input=dict(run.input),
            step_id=step.id,
            step_name=step.name,
            step_attempt=step.attempt,
            input_hash=step.input_hash,
            output=dict(step.output),
            metrics=dict(step.metrics),
        )


__all__ = [
    "CANCEL_RECOVERY_AMBIGUOUS_MESSAGE",
    "CONFIG_FINGERPRINT_MISMATCH_MESSAGE",
    "DEFAULT_RECOVERY_SCAN_LIMIT",
    "WORKER_LOST_INVARIANT_MESSAGE",
    "WORKER_LOST_NO_ATTEMPTS_MESSAGE",
    "WORKER_LOST_NO_PLAN_MESSAGE",
    "PipelineRecoveryService",
    "RecoveryOutcome",
]
