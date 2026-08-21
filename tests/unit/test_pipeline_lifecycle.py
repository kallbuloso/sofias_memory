from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sofias_memory.domain import (
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    PipelineProgressOutOfBoundsError,
    PipelineRunStatus,
    PipelineRunTransitionError,
    PipelineStepStatus,
    PipelineStepTransitionError,
    PipelineType,
    validate_progress,
    validate_run_transition,
    validate_step_transition,
)
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.services.pipeline_lifecycle import (
    StepPlan,
    apply_run_progress,
    create_run_with_steps,
    transition_run,
    transition_step,
)

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
ALL_RUN_STATUSES = list(PipelineRunStatus)
ALL_STEP_STATUSES = list(PipelineStepStatus)


class FakePipelineRunRepository:
    def __init__(self) -> None:
        self.added: list[PipelineRun] = []

    async def add(self, run: PipelineRun) -> PipelineRun:
        self.added.append(run)
        return run


class FakePipelineStepRepository:
    def __init__(self) -> None:
        self.added: list[PipelineStep] = []

    async def add_many(self, steps: list[PipelineStep]) -> list[PipelineStep]:
        self.added.extend(steps)
        return list(steps)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.pipeline_runs = FakePipelineRunRepository()
        self.pipeline_steps = FakePipelineStepRepository()


def make_run(status: PipelineRunStatus, **overrides: object) -> PipelineRun:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=None,
        source_id=None,
        status=status,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={},
        progress=0.0,
        current_step=None,
        attempt=0,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint="b" * 64,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=None,
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=None,
    )
    defaults.update(overrides)
    return PipelineRun(**defaults)  # type: ignore[arg-type]


def make_step(status: PipelineStepStatus, **overrides: object) -> PipelineStep:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        run_id=uuid4(),
        name="do_thing",
        ordinal=0,
        status=status,
        attempt=0,
        input_hash=None,
        output={},
        metrics={},
        error=None,
        started_at=None,
        finished_at=None,
    )
    defaults.update(overrides)
    return PipelineStep(**defaults)  # type: ignore[arg-type]


# --- Domain: RUN_TRANSITIONS matrix ---------------------------------------


@pytest.mark.parametrize(("current", "target"), sorted(RUN_TRANSITIONS, key=str))
def test_valid_run_transitions_are_accepted(
    current: PipelineRunStatus, target: PipelineRunStatus
) -> None:
    validate_run_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in ALL_RUN_STATUSES
        for target in ALL_RUN_STATUSES
        if (current, target) not in RUN_TRANSITIONS
    ],
)
def test_invalid_run_transitions_are_rejected(
    current: PipelineRunStatus, target: PipelineRunStatus
) -> None:
    with pytest.raises(PipelineRunTransitionError) as exc_info:
        validate_run_transition(current, target)
    assert exc_info.value.current == current
    assert exc_info.value.target == target


@pytest.mark.parametrize(
    "terminal", [PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED, PipelineRunStatus.CANCELLED]
)
def test_run_terminal_states_are_immutable(terminal: PipelineRunStatus) -> None:
    for target in ALL_RUN_STATUSES:
        with pytest.raises(PipelineRunTransitionError):
            validate_run_transition(terminal, target)


def test_run_cancelling_never_returns_to_running() -> None:
    with pytest.raises(PipelineRunTransitionError):
        validate_run_transition(PipelineRunStatus.CANCELLING, PipelineRunStatus.RUNNING)


def test_run_failed_never_reopens_via_transition() -> None:
    """Manual retry (SM-514) creates a NEW PipelineRun; the failed run itself never reopens."""

    for target in ALL_RUN_STATUSES:
        with pytest.raises(PipelineRunTransitionError):
            validate_run_transition(PipelineRunStatus.FAILED, target)


# --- Domain: STEP_TRANSITIONS matrix ---------------------------------------


@pytest.mark.parametrize(("current", "target"), sorted(STEP_TRANSITIONS, key=str))
def test_valid_step_transitions_are_accepted(
    current: PipelineStepStatus, target: PipelineStepStatus
) -> None:
    validate_step_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in ALL_STEP_STATUSES
        for target in ALL_STEP_STATUSES
        if (current, target) not in STEP_TRANSITIONS
    ],
)
def test_invalid_step_transitions_are_rejected(
    current: PipelineStepStatus, target: PipelineStepStatus
) -> None:
    with pytest.raises(PipelineStepTransitionError) as exc_info:
        validate_step_transition(current, target)
    assert exc_info.value.current == current
    assert exc_info.value.target == target


# --- Domain: progress bounds -----------------------------------------------


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_validate_progress_accepts_bounded_values(value: float) -> None:
    validate_progress(value)  # must not raise


@pytest.mark.parametrize("value", [-0.01, 1.01, -1.0, 2.0])
def test_validate_progress_rejects_out_of_bounds(value: float) -> None:
    with pytest.raises(PipelineProgressOutOfBoundsError):
        validate_progress(value)


def test_apply_run_progress_rejects_out_of_bounds() -> None:
    run = make_run(PipelineRunStatus.RUNNING)
    with pytest.raises(PipelineProgressOutOfBoundsError):
        apply_run_progress(run, 1.5)
    assert run.progress == 0.0  # unchanged on rejection


def test_apply_run_progress_sets_value() -> None:
    run = make_run(PipelineRunStatus.RUNNING)
    apply_run_progress(run, 0.75)
    assert run.progress == 0.75


# --- Services: transition_run timestamp/attempt semantics -----------------


def test_transition_run_queued_to_running_sets_started_at_and_increments_attempt() -> None:
    run = make_run(PipelineRunStatus.QUEUED, attempt=0, started_at=None)
    transition_run(run, PipelineRunStatus.RUNNING, now=NOW, worker_id="wk-1")
    assert run.status == PipelineRunStatus.RUNNING
    assert run.attempt == 1
    assert run.started_at == NOW
    assert run.worker_id == "wk-1"
    assert run.heartbeat_at == NOW
    assert run.next_attempt_at is None


def test_transition_run_reclaim_does_not_reset_started_at() -> None:
    original_start = NOW - timedelta(minutes=5)
    run = make_run(
        PipelineRunStatus.QUEUED,
        attempt=1,
        started_at=original_start,
        next_attempt_at=NOW - timedelta(seconds=1),
    )
    transition_run(run, PipelineRunStatus.RUNNING, now=NOW, worker_id="wk-2")
    assert run.attempt == 2
    assert run.started_at == original_start  # unchanged on reclaim
    assert run.next_attempt_at is None


def test_transition_run_running_to_queued_sets_next_attempt_at() -> None:
    run = make_run(PipelineRunStatus.RUNNING, attempt=1, started_at=NOW)
    retry_at = NOW + timedelta(seconds=30)
    transition_run(run, PipelineRunStatus.QUEUED, now=NOW, next_attempt_at=retry_at)
    assert run.status == PipelineRunStatus.QUEUED
    assert run.next_attempt_at == retry_at
    assert run.attempt == 1  # requeue itself does not increment attempt


@pytest.mark.parametrize(
    ("from_status", "terminal"),
    [
        (PipelineRunStatus.RUNNING, PipelineRunStatus.SUCCEEDED),
        (PipelineRunStatus.RUNNING, PipelineRunStatus.FAILED),
        (PipelineRunStatus.CANCELLING, PipelineRunStatus.FAILED),
        (PipelineRunStatus.QUEUED, PipelineRunStatus.CANCELLED),
        (PipelineRunStatus.CANCELLING, PipelineRunStatus.CANCELLED),
    ],
)
def test_transition_run_terminal_sets_finished_at_exactly_once(
    from_status: PipelineRunStatus, terminal: PipelineRunStatus
) -> None:
    run = make_run(from_status, finished_at=None)
    transition_run(run, terminal, now=NOW)
    assert run.finished_at == NOW
    assert run.status == terminal


def test_transition_run_failed_sets_error_fields() -> None:
    run = make_run(PipelineRunStatus.RUNNING)
    transition_run(
        run,
        PipelineRunStatus.FAILED,
        now=NOW,
        error_code="CONFIG_FINGERPRINT_MISMATCH",
        error_message="Configuration changed since this run was claimed.",
    )
    assert run.error_code == "CONFIG_FINGERPRINT_MISMATCH"
    assert run.error_message == "Configuration changed since this run was claimed."


def test_transition_run_invalid_move_raises_and_does_not_mutate() -> None:
    run = make_run(PipelineRunStatus.SUCCEEDED, progress=1.0)
    with pytest.raises(PipelineRunTransitionError):
        transition_run(run, PipelineRunStatus.RUNNING, now=NOW)
    assert run.status == PipelineRunStatus.SUCCEEDED  # unchanged


# --- Services: transition_step attempt/timestamp semantics ----------------


def test_transition_step_queued_to_running_increments_attempt_and_sets_started_at() -> None:
    step = make_step(PipelineStepStatus.QUEUED, attempt=0)
    transition_step(step, PipelineStepStatus.RUNNING, now=NOW)
    assert step.attempt == 1
    assert step.started_at == NOW
    assert step.finished_at is None


def test_transition_step_reexecution_increments_attempt_again() -> None:
    step = make_step(PipelineStepStatus.QUEUED, attempt=1)
    transition_step(step, PipelineStepStatus.RUNNING, now=NOW)
    assert step.attempt == 2


def test_transition_step_succeeded_sets_output_and_finished_at() -> None:
    step = make_step(PipelineStepStatus.RUNNING, attempt=1)
    transition_step(
        step,
        PipelineStepStatus.SUCCEEDED,
        now=NOW,
        output={"chunks": 3},
        metrics={"duration_ms": 42},
    )
    assert step.status == PipelineStepStatus.SUCCEEDED
    assert step.finished_at == NOW
    assert step.output == {"chunks": 3}
    assert step.metrics == {"duration_ms": 42}


def test_transition_step_running_stale_recovery_resets_to_queued() -> None:
    step = make_step(PipelineStepStatus.RUNNING, attempt=1, started_at=NOW, finished_at=None)
    transition_step(step, PipelineStepStatus.QUEUED, now=NOW)
    assert step.status == PipelineStepStatus.QUEUED
    assert step.started_at is None
    assert step.finished_at is None
    assert step.attempt == 1  # recovery reset does not itself count as an execution


def test_transition_step_invalid_move_raises_and_does_not_mutate() -> None:
    step = make_step(PipelineStepStatus.SUCCEEDED)
    with pytest.raises(PipelineStepTransitionError):
        transition_step(step, PipelineStepStatus.RUNNING, now=NOW)
    assert step.status == PipelineStepStatus.SUCCEEDED


# --- Services: atomic run+steps materialization ----------------------------


@pytest.mark.asyncio
async def test_create_run_with_steps_materializes_everything_queued() -> None:
    uow = FakeUnitOfWork()
    plans = [
        StepPlan(name="chunk_document", ordinal=0),
        StepPlan(name="embed_chunks", ordinal=1, input_hash="c" * 64),
    ]
    run = await create_run_with_steps(
        uow,  # type: ignore[arg-type]
        pipeline_type=PipelineType.COGNIFY,
        dataset_id=uuid4(),
        source_id=None,
        idempotency_key=None,
        payload_hash="d" * 64,
        input={"foo": "bar"},
        config_fingerprint="e" * 64,
        steps=plans,
    )

    assert run.status == PipelineRunStatus.QUEUED
    assert run.progress == 0.0
    assert run.current_step is None
    assert run.attempt == 0
    assert run.worker_id is None
    assert run.heartbeat_at is None
    assert run.next_attempt_at is None
    assert run.finished_at is None
    assert run.retry_of_run_id is None

    assert uow.pipeline_runs.added == [run]
    assert len(uow.pipeline_steps.added) == 2
    for step, plan in zip(uow.pipeline_steps.added, plans, strict=True):
        assert step.run_id == run.id
        assert step.status == PipelineStepStatus.QUEUED
        assert step.attempt == 0
        assert step.name == plan.name
        assert step.ordinal == plan.ordinal
        assert step.input_hash == plan.input_hash
        assert step.output == {}
        assert step.metrics == {}
        assert step.error is None
        assert step.started_at is None
        assert step.finished_at is None


@pytest.mark.asyncio
async def test_create_run_with_steps_persists_retry_of_run_id() -> None:
    uow = FakeUnitOfWork()
    original_run_id = uuid4()
    run = await create_run_with_steps(
        uow,  # type: ignore[arg-type]
        pipeline_type=PipelineType.FORGET,
        dataset_id=None,
        source_id=uuid4(),
        idempotency_key="sys:retry:" + str(original_run_id),
        payload_hash="f" * 64,
        input={},
        config_fingerprint="a" * 64,
        steps=[],
        retry_of_run_id=original_run_id,
    )
    assert run.retry_of_run_id == original_run_id
    assert uow.pipeline_steps.added == []


@pytest.mark.asyncio
async def test_create_run_with_steps_with_no_steps_persists_only_run() -> None:
    uow = FakeUnitOfWork()
    run = await create_run_with_steps(
        uow,  # type: ignore[arg-type]
        pipeline_type=PipelineType.IMPROVE,
        dataset_id=uuid4(),
        source_id=None,
        idempotency_key=None,
        payload_hash="1" * 64,
        input={},
        config_fingerprint="2" * 64,
        steps=[],
    )
    assert uow.pipeline_runs.added == [run]
    assert uow.pipeline_steps.added == []
