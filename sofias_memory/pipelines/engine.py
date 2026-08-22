"""Resumable pipeline engine (ADR-0009 SS O and related sections).

Executes a ``PipelineRun`` that SM-503's claimer has already put into
``RUNNING``, walking its persisted ``PipelineStep`` rows strictly in
ordinal order: validating the persisted plan against the current registry
(SS B/SS 14), skipping already-succeeded steps whose input hash still
matches (SS B/SS 15), executing the next ``QUEUED`` step outside any held
lock (SS 17/18), and persisting the outcome -- success, retryable,
permanent, or cancelled -- each in its own short transaction (SS 19/28/29/
32/34/35).

This module does not poll, does not loop claiming, does not start a
worker, does not heartbeat, and does not implement stale recovery -- it
only knows how to run ONE already-claimed attempt to its own conclusion: a
terminal state, a scheduled retry, or an abandoned/fenced-out execution
whose result was superseded by a newer claim. SM-505 owns the poll loop
and heartbeat; SM-507 owns stale recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus
from sofias_memory.infrastructure.postgres.models import PipelineStep
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import (
    STEP_INPUT_DRIFT_ERROR_CODE,
    STEP_INPUT_UNRESOLVABLE_ERROR_CODE,
    UNEXPECTED_STEP_ERROR_CODE,
    PermanentPipelineStepError,
    RetryablePipelineStepError,
)
from sofias_memory.pipelines.registry import PipelineDefinition, PipelineRegistry, StepResult
from sofias_memory.pipelines.retry_policy import RetryPolicy
from sofias_memory.services.pipeline_lifecycle import (
    apply_run_progress,
    transition_run,
    transition_step,
)
from sofias_memory.services.pipeline_queue_claimer import ClaimedRun

STEP_INPUT_DRIFT_MESSAGE = "The persisted step plan no longer matches the registered pipeline."
STEP_INPUT_UNRESOLVABLE_MESSAGE = "A pipeline step's input could not be resolved."
UNEXPECTED_STEP_ERROR_MESSAGE = "An unexpected internal error occurred while executing this step."


async def _run_transactional_phase[T](coro: Coroutine[Any, Any, T]) -> T:
    """Cancellation-safe wrapper for one short, already-open PostgreSQL
    transaction (SM-505 forced-shutdown audit / ADR-0009 SS O, SS T).

    SM-505's worker calls ``task.cancel()`` on an execution task once its
    shutdown grace period expires (ADR-0009 SS T). ``PipelineEngine.execute``
    never holds a transaction open across a step's external ``execute()``
    call (SS 17/18), so that call is exactly where a forced cancellation is
    *meant* to land -- but the engine's own short transactional phases
    (``_prepare``, the step-start checkpoint, ``_commit_success`` including
    ``step.persist(...)``, and ``_commit_failure``) must never be interrupted
    mid-flight: a business mutation committing while its own ``PipelineStep``
    row transition does not (or vice versa) would violate the "Commit
    boundary preference" invariant this exact story's engine amendment
    exists to satisfy.

    ``asyncio.shield()`` alone is insufficient: if the *caller* awaiting the
    shield is cancelled, the shielded inner task keeps running independently
    but is left orphaned unless something explicitly awaits it afterward
    (never abandon a shielded inner task -- SM-505 SS 4). This wrapper
    guarantees the inner transaction always reaches its own natural
    conclusion -- COMMIT or ROLLBACK -- before any cancellation is allowed to
    propagate further: the ``CancelledError`` is deferred, never dropped,
    and re-raised only once nothing transactional is left running.
    """

    task: asyncio.Task[T] = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        raise


class PipelineEngineInvariantError(RuntimeError):
    """A persisted state was encountered that ADR-0009's contract should
    make impossible given a correctly-functioning claim/engine. Raised
    rather than silently adapted or "corrected" (SM-504 SS 14/SS 44)."""


@dataclass(frozen=True, slots=True)
class PipelineExecutionResult:
    """Small, internal result -- not a public/HTTP schema (SM-508 owns
    that). ``status`` is ``None`` only when ``abandoned`` is ``True``: an
    ownership-fencing check (ADR-0009 SS 16) found this execution's claim
    token superseded by a newer attempt, and nothing was mutated by this
    call beyond whatever had already committed before that point."""

    run_id: UUID
    status: PipelineRunStatus | None
    abandoned: bool = False
    paused_for_shutdown: bool = False
    """SM-505/ADR-0009 SS T amendment: this execution stopped at a safe,
    per-step checkpoint because the caller's ``stop_requested`` callable
    returned ``True`` before the next step started -- never because of
    fencing (``abandoned``) and never a business cancellation. The run is
    left ``RUNNING`` with whatever progress already committed; nothing was
    mutated by this call beyond that. Distinct from ``abandoned`` because the
    worker needs to tell "a newer claim superseded me" apart from "I chose to
    stop before starting more work"."""


@dataclass(frozen=True, slots=True)
class _StepSnapshot:
    """Detached, plain copy of one ``PipelineStep`` row read during Phase 0.

    Never an ORM instance carried across a session boundary: SQLAlchemy
    expires/detaches attributes once their session closes, so every value
    the loop needs is copied out here while the loading session is still
    open (the same pattern SM-503's own integration tests use to read state
    back safely).
    """

    id: UUID
    name: str
    ordinal: int
    status: PipelineStepStatus
    input_hash: str | None
    output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    kind: str  # "success" | "retryable" | "permanent"
    code: str | None = None
    message: str | None = None
    result: StepResult | None = None


@dataclass(frozen=True, slots=True)
class _CheckpointOutcome:
    abandoned: bool = False
    paused_for_shutdown: bool = False
    terminal_status: PipelineRunStatus | None = None
    step_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class _PersistOutcome:
    abandoned: bool = False
    terminal_status: PipelineRunStatus | None = None
    output: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _FencedStep:
    """Result of a successful ownership-fencing re-check (ADR-0009 SS 16)
    for the post-execute persist phase: the locked, still-owned ``run``/
    ``step`` ORM rows, whether the run is ``CANCELLING``, and the
    transaction's own PostgreSQL ``now()``."""

    run: Any
    step: Any
    run_is_cancelling: bool
    now: Any


class _StepPersistFailure(Exception):
    """Internal control-flow signal only, never surfaced to a caller of
    :meth:`PipelineEngine.execute`: a step's ``persist()`` phase raised
    (ADR-0009 SS O amendment). Carries the same typed classification as an
    ``execute()``-phase failure, to be recorded in a fresh, separate
    transaction after the failed persist transaction has fully rolled back."""

    def __init__(self, kind: str, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message


def _is_owner(run_worker_id: str | None, run_attempt: int, claimed: ClaimedRun) -> bool:
    """ADR-0009 SS 16 fencing token: ``(worker_id, PipelineRun.attempt)``.

    Comparing ``worker_id`` alone is not sufficient -- the same process
    reuses the same ``worker_id`` for its whole boot, so a stale execution
    from an earlier claim of the same run by the same process would
    otherwise still pass a ``worker_id``-only check after a reclaim.
    """

    return run_worker_id == claimed.worker_id and run_attempt == claimed.attempt


class PipelineEngine:
    """Executes one already-claimed run to its own conclusion."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: PipelineRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        resources: dict[str, Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._retry_policy = retry_policy or RetryPolicy()
        self._resources = resources or {}

    async def execute(
        self,
        claimed_run: ClaimedRun,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> PipelineExecutionResult:
        """Run one already-claimed attempt to its own conclusion.

        ``stop_requested`` is SM-505's process-local, cooperative shutdown
        signal (ADR-0009 SS T amendment) -- a zero-argument callable, never an
        ``asyncio.Event`` directly, to keep this module free of an asyncio
        dependency. Checked only at the safe per-step checkpoint, and only
        after business cancellation (``CANCELLING``) has already been ruled
        out for that same checkpoint: cancellation intent always takes
        precedence over a worker-local shutdown pause. When ``None`` (the
        default), behavior is identical to before this parameter existed.
        """

        definition = self._registry.get(claimed_run.pipeline_type)

        prepared = await _run_transactional_phase(self._prepare(claimed_run, definition))
        if prepared.abandoned:
            return PipelineExecutionResult(run_id=claimed_run.run_id, status=None, abandoned=True)
        if prepared.terminal_status is not None:
            return PipelineExecutionResult(
                run_id=claimed_run.run_id, status=prepared.terminal_status
            )

        assert prepared.run_input is not None  # noqa: S101 - set whenever not abandoned/terminal
        assert prepared.step_outputs is not None  # noqa: S101
        assert prepared.pending_steps is not None  # noqa: S101

        run_input = prepared.run_input
        step_outputs = dict(prepared.step_outputs)
        total_steps = len(definition.steps)
        succeeded_count = total_steps - len(prepared.pending_steps)
        steps_by_name = {step_def.name: step_def for step_def in definition.steps}

        for step_row in prepared.pending_steps:
            step_def = steps_by_name[step_row.name]

            checkpoint = await _run_transactional_phase(
                self._checkpoint_before_step(
                    claimed_run,
                    step_row,
                    step_def,
                    run_input=run_input,
                    step_outputs=step_outputs,
                    stop_requested=stop_requested,
                )
            )
            if checkpoint.abandoned:
                return PipelineExecutionResult(
                    run_id=claimed_run.run_id, status=None, abandoned=True
                )
            if checkpoint.paused_for_shutdown:
                return PipelineExecutionResult(
                    run_id=claimed_run.run_id, status=None, paused_for_shutdown=True
                )
            if checkpoint.terminal_status is not None:
                return PipelineExecutionResult(
                    run_id=claimed_run.run_id, status=checkpoint.terminal_status
                )
            assert checkpoint.step_attempt is not None  # noqa: S101 - set on the executing branch

            context = PipelineContext(
                run_id=claimed_run.run_id,
                pipeline_type=claimed_run.pipeline_type,
                dataset_id=claimed_run.dataset_id,
                source_id=prepared.source_id,
                run_input=run_input,
                step_outputs=dict(step_outputs),
                session_factory=self._session_factory,
                resources=self._resources,
            )

            outcome = await self._run_step(step_def, context)

            persisted = await self._persist_step_outcome(
                claimed_run=claimed_run,
                step_id=step_row.id,
                expected_step_attempt=checkpoint.step_attempt,
                outcome=outcome,
                succeeded_count=succeeded_count,
                total_steps=total_steps,
                step_def=step_def,
                context=context,
            )
            if persisted.abandoned:
                return PipelineExecutionResult(
                    run_id=claimed_run.run_id, status=None, abandoned=True
                )
            if persisted.terminal_status is not None:
                return PipelineExecutionResult(
                    run_id=claimed_run.run_id, status=persisted.terminal_status
                )

            succeeded_count += 1
            step_outputs[step_row.name] = persisted.output or {}

        raise PipelineEngineInvariantError(
            f"Run {claimed_run.run_id} exhausted its pending steps without reaching a "
            "terminal state -- the last step's success path should always finalize the run."
        )

    # -- Phase 0: fencing, plan validation, pre-loop terminal cases --------

    async def _prepare(
        self,
        claimed_run: ClaimedRun,
        definition: PipelineDefinition,
    ) -> _PrepareOutcome:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(claimed_run.run_id)
            if run is None:
                raise PipelineEngineInvariantError(
                    f"Claimed run {claimed_run.run_id} does not exist."
                )
            if not _is_owner(run.worker_id, run.attempt, claimed_run):
                return _PrepareOutcome(abandoned=True)

            steps: list[PipelineStep] = await uow.pipeline_steps.list_for_run(run.id)
            step_snapshots = [
                _StepSnapshot(
                    id=step.id,
                    name=step.name,
                    ordinal=step.ordinal,
                    status=step.status,
                    input_hash=step.input_hash,
                    output=dict(step.output),
                )
                for step in steps
            ]
            run_input = dict(run.input)
            source_id = run.source_id
            run_status = run.status

            issue, step_outputs, first_pending_index = self._validate_plan(
                definition, step_snapshots, run_input=run_input
            )
            if issue is not None:
                db_now = await uow.pipeline_runs.get_database_now()
                transition_run(
                    run,
                    PipelineRunStatus.FAILED,
                    now=db_now,
                    error_code=issue[0],
                    error_message=issue[1],
                )
                await uow.commit()
                return _PrepareOutcome(terminal_status=PipelineRunStatus.FAILED)

            # ADR-0009 SS A/SS N: CANCELLING may only converge to CANCELLED
            # or FAILED, never SUCCEEDED -- RUN_TRANSITIONS has no
            # (CANCELLING, SUCCEEDED) edge, so this check must run BEFORE
            # the "every step already SUCCEEDED" finalization below, not
            # after it. A cancel request that lands after the business work
            # is already complete still converges to CANCELLED: the intent
            # was observed and accepted, even though it was too late to
            # prevent completion (see the dedicated race-order tests).
            if run_status == PipelineRunStatus.CANCELLING:
                db_now = await uow.pipeline_runs.get_database_now()
                self._cancel_queued(steps, now=db_now)
                if first_pending_index is None:
                    apply_run_progress(run, 1.0)
                transition_run(run, PipelineRunStatus.CANCELLED, now=db_now)
                await uow.commit()
                return _PrepareOutcome(terminal_status=PipelineRunStatus.CANCELLED)

            if first_pending_index is None:
                # Every step already SUCCEEDED and the run is not
                # CANCELLING -- e.g. a crash landed between the last step's
                # own commit and the run's SUCCEEDED transition (SS 19).
                # Finalize now; never re-execute anything.
                db_now = await uow.pipeline_runs.get_database_now()
                apply_run_progress(run, 1.0)
                transition_run(run, PipelineRunStatus.SUCCEEDED, now=db_now)
                await uow.commit()
                return _PrepareOutcome(terminal_status=PipelineRunStatus.SUCCEEDED)

            if run_status != PipelineRunStatus.RUNNING:
                raise PipelineEngineInvariantError(
                    f"Run {claimed_run.run_id} has unexpected status {run_status!s} "
                    "at the start of execution."
                )

            # Read-only pass: nothing to commit. Letting the UnitOfWork
            # context manager roll back on exit is harmless and releases
            # the row lock exactly the same as a commit would.
            return _PrepareOutcome(
                run_input=run_input,
                source_id=source_id,
                step_outputs=step_outputs,
                pending_steps=step_snapshots[first_pending_index:],
            )

    def _validate_plan(
        self,
        definition: PipelineDefinition,
        steps: list[_StepSnapshot],
        *,
        run_input: dict[str, Any],
    ) -> tuple[tuple[str, str] | None, dict[str, dict[str, Any]], int | None]:
        """ADR-0009 SS 14/SS 15: compare the persisted plan against the
        current registry before executing anything, and derive the initial
        ``step_outputs`` accumulator plus the index of the first pending
        (non-``SUCCEEDED``) step from the same single pass.

        Returns ``((error_code, error_message), {}, None)`` on drift,
        ``(None, outputs, None)`` when every step is already ``SUCCEEDED``,
        or ``(None, outputs, first_pending_index)`` otherwise.
        """

        drift = (STEP_INPUT_DRIFT_ERROR_CODE, STEP_INPUT_DRIFT_MESSAGE)
        step_outputs: dict[str, dict[str, Any]] = {}

        if len(steps) != len(definition.steps):
            return drift, step_outputs, None

        first_pending_index: int | None = None
        seen_pending = False
        for index, (step_row, step_def) in enumerate(zip(steps, definition.steps, strict=True)):
            if step_row.ordinal != index or step_row.name != step_def.name:
                return drift, step_outputs, None

            expected_hash = step_def.compute_input_hash(
                run_input=run_input, step_outputs=step_outputs
            )
            if step_row.input_hash is not None and (
                expected_hash is None or step_row.input_hash != expected_hash
            ):
                return drift, step_outputs, None
            # step_row.input_hash is None: either this step has no hash
            # contract by design (deriver always returns None), or it has
            # not been backfilled yet -- both are fine here; backfill (when
            # applicable) happens at that step's own checkpoint (SS 12).

            if step_row.status == PipelineStepStatus.SUCCEEDED:
                if seen_pending:
                    raise PipelineEngineInvariantError(
                        f"Step {step_row.name!r} (ordinal {step_row.ordinal}) is SUCCEEDED "
                        "after an earlier step that is not -- persisted step order is corrupted."
                    )
                step_outputs[step_row.name] = dict(step_row.output)
            else:
                seen_pending = True
                if first_pending_index is None:
                    first_pending_index = index
                if step_row.status != PipelineStepStatus.QUEUED:
                    raise PipelineEngineInvariantError(
                        f"Step {step_row.name!r} (ordinal {step_row.ordinal}) has unexpected "
                        f"status {step_row.status!s} for a normal engine resume."
                    )

        return None, step_outputs, first_pending_index

    @staticmethod
    def _cancel_queued(steps: list[PipelineStep], *, now: Any) -> None:
        for step in steps:
            if step.status == PipelineStepStatus.QUEUED:
                transition_step(step, PipelineStepStatus.CANCELLED, now=now)

    # -- Per-step checkpoint (ADR-0009 SS 17/SS 32) -------------------------

    async def _checkpoint_before_step(
        self,
        claimed_run: ClaimedRun,
        step_row: _StepSnapshot,
        step_def: Any,
        *,
        run_input: dict[str, Any],
        step_outputs: dict[str, dict[str, Any]],
        stop_requested: Callable[[], bool] | None = None,
    ) -> _CheckpointOutcome:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(claimed_run.run_id)
            if run is None:
                raise PipelineEngineInvariantError(
                    f"Run {claimed_run.run_id} disappeared mid-execution."
                )
            if not _is_owner(run.worker_id, run.attempt, claimed_run):
                return _CheckpointOutcome(abandoned=True)

            step = await uow.pipeline_steps.get_by_id_for_update(step_row.id)
            if step is None or step.status != PipelineStepStatus.QUEUED:
                raise PipelineEngineInvariantError(
                    f"Step {step_row.id} is not QUEUED at its own checkpoint."
                )

            db_now = await uow.pipeline_runs.get_database_now()

            if run.status == PipelineRunStatus.CANCELLING:
                all_steps = await uow.pipeline_steps.list_for_run(run.id)
                self._cancel_queued(all_steps, now=db_now)
                transition_run(run, PipelineRunStatus.CANCELLED, now=db_now)
                await uow.commit()
                return _CheckpointOutcome(terminal_status=PipelineRunStatus.CANCELLED)

            if run.status != PipelineRunStatus.RUNNING:
                raise PipelineEngineInvariantError(
                    f"Run {claimed_run.run_id} has unexpected status {run.status!s} "
                    "at a step checkpoint."
                )

            # ADR-0009 SS T amendment (checkpoint ordering, SM-505 SS 18):
            # business cancellation has already been ruled out above -- only
            # now, with the run confirmed RUNNING, may a process-local
            # shutdown pause take effect. Nothing is mutated: exiting this
            # ``async with`` block without a commit rolls back, releasing the
            # row lock exactly as an unclaimed candidate would.
            if stop_requested is not None and stop_requested():
                return _CheckpointOutcome(paused_for_shutdown=True)

            expected_hash = step_def.compute_input_hash(
                run_input=run_input, step_outputs=step_outputs
            )
            if expected_hash is None:
                transition_run(
                    run,
                    PipelineRunStatus.FAILED,
                    now=db_now,
                    error_code=STEP_INPUT_UNRESOLVABLE_ERROR_CODE,
                    error_message=STEP_INPUT_UNRESOLVABLE_MESSAGE,
                )
                await uow.commit()
                return _CheckpointOutcome(terminal_status=PipelineRunStatus.FAILED)

            if step.input_hash is not None and step.input_hash != expected_hash:
                transition_run(
                    run,
                    PipelineRunStatus.FAILED,
                    now=db_now,
                    error_code=STEP_INPUT_DRIFT_ERROR_CODE,
                    error_message=STEP_INPUT_DRIFT_MESSAGE,
                )
                await uow.commit()
                return _CheckpointOutcome(terminal_status=PipelineRunStatus.FAILED)
            if step.input_hash is None:
                step.input_hash = expected_hash

            run.current_step = step.name
            transition_step(step, PipelineStepStatus.RUNNING, now=db_now)
            step_attempt = step.attempt
            await uow.commit()
            return _CheckpointOutcome(step_attempt=step_attempt)

    # -- Step execution: no lock/transaction held (ADR-0009 SS 17/SS 18) ---

    async def _run_step(self, step_def: Any, context: PipelineContext) -> _StepOutcome:
        try:
            result = await step_def.step.execute(context)
        except RetryablePipelineStepError as exc:
            return _StepOutcome(kind="retryable", code=exc.code, message=exc.message)
        except PermanentPipelineStepError as exc:
            return _StepOutcome(kind="permanent", code=exc.code, message=exc.message)
        except Exception:  # noqa: BLE001 - fail-safe classification (SS 22)
            return _StepOutcome(
                kind="permanent",
                code=UNEXPECTED_STEP_ERROR_CODE,
                message=UNEXPECTED_STEP_ERROR_MESSAGE,
            )
        return _StepOutcome(kind="success", result=result)

    # -- Persisting the outcome (ADR-0009 SS 19/SS 28/SS 29/SS 34/SS 35) ---

    async def _load_fenced_running_step(
        self,
        uow: PostgresUnitOfWork,
        claimed_run: ClaimedRun,
        step_id: UUID,
        expected_step_attempt: int,
    ) -> _FencedStep | None:
        """Load+lock ``run``/``step`` and re-check ownership fencing (ADR-0009
        SS 16) for the post-execute persist phase. Returns ``None`` when this
        execution has been superseded -- the caller must return
        ``abandoned=True`` without any further mutation. Shared by both the
        success and the retryable/permanent failure paths, which are always
        two *separate* transactions (a failed ``persist()`` rolls the success
        transaction back in full, per the SM-504 amendment to ADR-0009 SS O)."""

        run = await uow.pipeline_runs.get_by_id_for_update(claimed_run.run_id)
        if run is None:
            raise PipelineEngineInvariantError(
                f"Run {claimed_run.run_id} disappeared mid-execution."
            )
        if not _is_owner(run.worker_id, run.attempt, claimed_run):
            return None

        step = await uow.pipeline_steps.get_by_id_for_update(step_id)
        if step is None:
            raise PipelineEngineInvariantError(f"Step {step_id} disappeared mid-execution.")
        if step.status != PipelineStepStatus.RUNNING or step.attempt != expected_step_attempt:
            # A newer attempt already superseded this in-flight execution's
            # result. Discard silently -- it must never overwrite a newer try.
            return None

        if run.status not in (PipelineRunStatus.RUNNING, PipelineRunStatus.CANCELLING):
            raise PipelineEngineInvariantError(
                f"Run {claimed_run.run_id} has unexpected status {run.status!s} "
                "while persisting a step outcome."
            )
        db_now = await uow.pipeline_runs.get_database_now()
        return _FencedStep(
            run=run,
            step=step,
            run_is_cancelling=run.status == PipelineRunStatus.CANCELLING,
            now=db_now,
        )

    async def _persist_step_outcome(
        self,
        *,
        claimed_run: ClaimedRun,
        step_id: UUID,
        expected_step_attempt: int,
        outcome: _StepOutcome,
        succeeded_count: int,
        total_steps: int,
        step_def: Any,
        context: PipelineContext,
    ) -> _PersistOutcome:
        if outcome.kind == "success":
            try:
                return await _run_transactional_phase(
                    self._commit_success(
                        claimed_run=claimed_run,
                        step_id=step_id,
                        expected_step_attempt=expected_step_attempt,
                        outcome=outcome,
                        succeeded_count=succeeded_count,
                        total_steps=total_steps,
                        step_def=step_def,
                        context=context,
                    )
                )
            except _StepPersistFailure as failure:
                # persist() raised: the success transaction above has
                # already rolled back in full (no partial business mutation,
                # no succeeded transition survives it). Re-classify and
                # record the failure in a brand-new, separate transaction --
                # identical in kind to an execute()-phase failure.
                outcome = _StepOutcome(
                    kind=failure.kind, code=failure.code, message=failure.message
                )

        return await _run_transactional_phase(
            self._commit_failure(
                claimed_run=claimed_run,
                step_id=step_id,
                expected_step_attempt=expected_step_attempt,
                outcome=outcome,
            )
        )

    async def _commit_success(
        self,
        *,
        claimed_run: ClaimedRun,
        step_id: UUID,
        expected_step_attempt: int,
        outcome: _StepOutcome,
        succeeded_count: int,
        total_steps: int,
        step_def: Any,
        context: PipelineContext,
    ) -> _PersistOutcome:
        assert outcome.result is not None  # noqa: S101 - always set for "success"

        async with PostgresUnitOfWork(self._session_factory) as uow:
            fenced = await self._load_fenced_running_step(
                uow, claimed_run, step_id, expected_step_attempt
            )
            if fenced is None:
                return _PersistOutcome(abandoned=True)

            # ADR-0009 SS O (SM-504 amendment): the step's own authoritative
            # PostgreSQL mutation, applied inside this same short
            # transaction, BEFORE the step's own succeeded transition --
            # never a transaction the step opens independently, never after
            # this transaction's commit. If this raises, the whole
            # transaction rolls back on the way out of this ``async with``
            # block (no commit was called) and the caller records the
            # failure separately.
            try:
                await step_def.step.persist(context, outcome.result, uow)
            except (RetryablePipelineStepError, PermanentPipelineStepError) as exc:
                kind = "retryable" if isinstance(exc, RetryablePipelineStepError) else "permanent"
                raise _StepPersistFailure(kind, exc.code, exc.message) from exc
            except Exception as exc:  # noqa: BLE001 - fail-safe classification (SS 22)
                raise _StepPersistFailure(
                    "permanent", UNEXPECTED_STEP_ERROR_CODE, UNEXPECTED_STEP_ERROR_MESSAGE
                ) from exc

            transition_step(
                fenced.step,
                PipelineStepStatus.SUCCEEDED,
                now=fenced.now,
                output=outcome.result.output,
                metrics=outcome.result.metrics,
            )
            new_succeeded_count = succeeded_count + 1
            apply_run_progress(fenced.run, new_succeeded_count / total_steps)
            output = dict(outcome.result.output)

            if fenced.run_is_cancelling:
                # ADR-0009 SS 32/SS 33: the step already reached its own
                # terminal outcome (success) before cancellation is
                # observed -- never interrupted mid-step. Finalize
                # cancellation now instead of continuing to the next step.
                # Never SUCCEEDED here, even for the run's own last step
                # (SS A/SS N: CANCELLING has no path to SUCCEEDED).
                other_steps = await uow.pipeline_steps.list_for_run(fenced.run.id)
                self._cancel_queued(other_steps, now=fenced.now)
                transition_run(fenced.run, PipelineRunStatus.CANCELLED, now=fenced.now)
                await uow.commit()
                return _PersistOutcome(terminal_status=PipelineRunStatus.CANCELLED)

            if new_succeeded_count == total_steps:
                transition_run(fenced.run, PipelineRunStatus.SUCCEEDED, now=fenced.now)
                await uow.commit()
                return _PersistOutcome(terminal_status=PipelineRunStatus.SUCCEEDED)

            await uow.commit()
            return _PersistOutcome(output=output)

    async def _commit_failure(
        self,
        *,
        claimed_run: ClaimedRun,
        step_id: UUID,
        expected_step_attempt: int,
        outcome: _StepOutcome,
    ) -> _PersistOutcome:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            fenced = await self._load_fenced_running_step(
                uow, claimed_run, step_id, expected_step_attempt
            )
            if fenced is None:
                return _PersistOutcome(abandoned=True)

            error = {"code": outcome.code, "message": outcome.message}

            if fenced.run_is_cancelling:
                return await self._persist_failure_while_cancelling(
                    uow,
                    run=fenced.run,
                    step=fenced.step,
                    outcome=outcome,
                    error=error,
                    now=fenced.now,
                )

            if outcome.kind == "retryable" and self._retry_policy.has_attempts_remaining(
                fenced.run.attempt
            ):
                transition_step(fenced.step, PipelineStepStatus.QUEUED, now=fenced.now, error=error)
                delay = self._retry_policy.backoff_seconds(fenced.run.attempt)
                transition_run(
                    fenced.run,
                    PipelineRunStatus.QUEUED,
                    now=fenced.now,
                    next_attempt_at=fenced.now + timedelta(seconds=delay),
                )
                await uow.commit()
                return _PersistOutcome(terminal_status=PipelineRunStatus.QUEUED)

            # Permanent, or retryable with the run's attempt ceiling
            # exhausted (ADR-0009 SS 25): fail the step and the run with the
            # last error's stable code/safe message.
            transition_step(fenced.step, PipelineStepStatus.FAILED, now=fenced.now, error=error)
            transition_run(
                fenced.run,
                PipelineRunStatus.FAILED,
                now=fenced.now,
                error_code=outcome.code,
                error_message=outcome.message,
            )
            await uow.commit()
            return _PersistOutcome(terminal_status=PipelineRunStatus.FAILED)

    async def _persist_failure_while_cancelling(
        self,
        uow: PostgresUnitOfWork,
        *,
        run: Any,
        step: Any,
        outcome: _StepOutcome,
        error: dict[str, Any],
        now: Any,
    ) -> _PersistOutcome:
        if outcome.kind == "permanent":
            # ADR-0009 SS 35: a real failure must never be masked as a
            # successful cancellation.
            transition_step(step, PipelineStepStatus.FAILED, now=now, error=error)
            transition_run(
                run,
                PipelineRunStatus.FAILED,
                now=now,
                error_code=outcome.code,
                error_message=outcome.message,
            )
            await uow.commit()
            return _PersistOutcome(terminal_status=PipelineRunStatus.FAILED)

        # ADR-0009 SS 34: cancellation intent must never be lost. Route the
        # step through its already-legal RUNNING -> QUEUED edge (the same
        # edge normal retry uses) and immediately on to QUEUED -> CANCELLED
        # in the same transaction -- both are members of STEP_TRANSITIONS.
        # The run itself never touches QUEUED (CANCELLING -> QUEUED is not a
        # legal run transition): it goes straight CANCELLING -> CANCELLED.
        transition_step(step, PipelineStepStatus.QUEUED, now=now, error=error)
        transition_step(step, PipelineStepStatus.CANCELLED, now=now)
        other_steps = await uow.pipeline_steps.list_for_run(run.id)
        self._cancel_queued(other_steps, now=now)
        transition_run(run, PipelineRunStatus.CANCELLED, now=now)
        await uow.commit()
        return _PersistOutcome(terminal_status=PipelineRunStatus.CANCELLED)


@dataclass(frozen=True, slots=True)
class _PrepareOutcome:
    abandoned: bool = False
    terminal_status: PipelineRunStatus | None = None
    run_input: dict[str, Any] | None = None
    source_id: UUID | None = None
    step_outputs: dict[str, dict[str, Any]] | None = None
    pending_steps: list[_StepSnapshot] | None = None
