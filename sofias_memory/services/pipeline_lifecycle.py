"""Operational persistence primitives for PipelineRun/PipelineStep (ADR-0009).

SM-502 scope only: atomic run+step-plan materialization and guarded status
transitions. No queue claiming, no advisory locks, no worker loop, no
pipeline engine, no business step execution -- those belong to SM-503..SM-507.

Callers own the UnitOfWork/transaction boundary; nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sofias_memory.domain import (
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
    validate_progress,
    validate_run_transition,
    validate_step_transition,
)
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import StepPlan

__all__ = [
    "StepPlan",
    "apply_run_progress",
    "create_run_with_steps",
    "transition_run",
    "transition_step",
]


async def create_run_with_steps(
    uow: PostgresUnitOfWork,
    *,
    pipeline_type: PipelineType,
    dataset_id: UUID | None,
    source_id: UUID | None,
    idempotency_key: str | None,
    payload_hash: str,
    input: dict[str, Any],
    config_fingerprint: str,
    steps: Sequence[StepPlan],
    retry_of_run_id: UUID | None = None,
) -> PipelineRun:
    """Materialize a ``queued`` PipelineRun and its ``queued`` PipelineSteps.

    ADR-0009 SS B/SS C: PipelineRun and every PipelineStep row are persisted
    in the same submission transaction, all initially ``queued`` -- step
    materialization is not deferred to first claim. This function only adds
    the rows to the UnitOfWork's session (flush, not commit); the caller
    commits.
    """

    run = PipelineRun(
        id=uuid4(),
        pipeline_type=pipeline_type,
        dataset_id=dataset_id,
        source_id=source_id,
        status=PipelineRunStatus.QUEUED,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        input=input,
        progress=0.0,
        current_step=None,
        attempt=0,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint=config_fingerprint,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=None,
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=retry_of_run_id,
    )
    await uow.pipeline_runs.add(run)

    step_rows = [
        PipelineStep(
            id=uuid4(),
            run_id=run.id,
            name=plan.name,
            ordinal=plan.ordinal,
            status=PipelineStepStatus.QUEUED,
            attempt=0,
            input_hash=plan.input_hash,
            output={},
            metrics={},
            error=None,
            started_at=None,
            finished_at=None,
        )
        for plan in steps
    ]
    if step_rows:
        await uow.pipeline_steps.add_many(step_rows)

    return run


def transition_run(
    run: PipelineRun,
    target: PipelineRunStatus,
    *,
    now: datetime,
    worker_id: str | None = None,
    next_attempt_at: datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Mutate ``run`` in place to ``target``, enforcing ADR-0009's run matrix.

    Only mutates already-loaded ORM attributes; the caller's session/UnitOfWork
    owns flush/commit and any row locking. This function does not decide
    *when* a transition should happen (claim eligibility, backoff scheduling,
    safe checkpoints) -- it only knows how to apply an already-decided target
    status coherently. Raises ``PipelineRunTransitionError`` for any move not
    in ``domain.RUN_TRANSITIONS``.
    """

    validate_run_transition(run.status, target)

    if target == PipelineRunStatus.RUNNING:
        # ADR-0009 SS A/SS L: attempt increments on every claim
        # (queued -> running); started_at is set once, on the first claim.
        run.attempt += 1
        if run.started_at is None:
            run.started_at = now
        run.worker_id = worker_id
        run.heartbeat_at = now
        run.next_attempt_at = None
    elif target == PipelineRunStatus.QUEUED:
        # Reachable only from RUNNING (ADR-0009 SS K automatic-retry requeue,
        # or SS I RUNNING-stale recovery reconciling an abandoned claim).
        run.next_attempt_at = next_attempt_at
    elif target in (
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLED,
    ):
        run.finished_at = now
        if target == PipelineRunStatus.FAILED:
            run.error_code = error_code
            run.error_message = error_message
    # CANCELLING: pure status flip; the safe-checkpoint completion that
    # eventually reaches CANCELLED is the future worker's job (SM-505/SM-514).

    run.status = target


def transition_step(
    step: PipelineStep,
    target: PipelineStepStatus,
    *,
    now: datetime,
    output: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Mutate ``step`` in place to ``target``, enforcing ADR-0009's step matrix.

    Same contract as :func:`transition_run`: pure state application, no
    business step execution. Raises ``PipelineStepTransitionError`` for any
    move not in ``domain.STEP_TRANSITIONS``.
    """

    validate_step_transition(step.status, target)

    if target == PipelineStepStatus.RUNNING:
        # ADR-0009 SS L: step attempt increments on every execution.
        step.attempt += 1
        step.started_at = now
        step.finished_at = None
    elif target == PipelineStepStatus.SUCCEEDED:
        step.finished_at = now
        if output is not None:
            step.output = output
        if metrics is not None:
            step.metrics = metrics
    elif target == PipelineStepStatus.FAILED:
        step.finished_at = now
        if error is not None:
            step.error = error
        if metrics is not None:
            step.metrics = metrics
    elif target == PipelineStepStatus.CANCELLED:
        # ADR-0009 SS B/SS N: either a queued-but-never-started step whose
        # owning run was cancelled, or CANCELLING-stale recovery classifying
        # an orphaned running step as safe/reconciled (SS I cases A/B).
        step.finished_at = now
    elif target == PipelineStepStatus.QUEUED:
        # ADR-0009 SS I: RUNNING-stale recovery resets an orphaned step to a
        # clean slate so the engine re-executes it on the next normal claim.
        # ADR-0009 SS 28 (SM-504 focused extension): a retryable failure also
        # lands here (RUNNING -> QUEUED) and may carry the last attempt's
        # safe error, visible on the step while its owning run awaits
        # automatic retry -- this never touches PipelineRun.error_code/
        # error_message, which stay reserved for a FAILED terminal run (SS
        # 30).
        step.started_at = None
        step.finished_at = None
        if error is not None:
            step.error = error

    step.status = target


def apply_run_progress(run: PipelineRun, value: float) -> None:
    """Set ``run.progress``, enforcing the ``[0.0, 1.0]`` bound (ADR-0009 SS A)."""

    validate_progress(value)
    run.progress = value
