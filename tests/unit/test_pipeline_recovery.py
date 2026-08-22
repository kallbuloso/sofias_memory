"""Unit tests for the pure stale-recovery reconciliation logic (SM-507).

These exercise :class:`PipelineRecoveryService`'s classification/transition
methods directly against in-memory ORM instances (the same pattern
``test_pipeline_lifecycle.py`` uses) -- no PostgreSQL, no session. Repository
query correctness, ``recover_startup`` wiring, idempotency, and concurrency
are proven separately against real PostgreSQL in
``tests/integration/test_pipeline_recovery_postgres_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.pipelines.errors import (
    CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
    CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE,
    WORKER_LOST_ERROR_CODE,
)
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    CancellationRecoveryOutcome,
    PipelineCancellationRecoveryContext,
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
    no_op_persist,
)
from sofias_memory.pipelines.retry_policy import RetryPolicy
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService, _is_stale

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
CONFIG_FINGERPRINT = "a" * 64
OTHER_CONFIG_FINGERPRINT = "b" * 64


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
        attempt=1,
        worker_id="wk-old",
        heartbeat_at=NOW - timedelta(seconds=600),
        config_fingerprint=CONFIG_FINGERPRINT,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=NOW - timedelta(seconds=700),
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
        attempt=1,
        input_hash=None,
        output={},
        metrics={},
        error=None,
        started_at=NOW - timedelta(seconds=650),
        finished_at=None,
    )
    defaults.update(overrides)
    return PipelineStep(**defaults)  # type: ignore[arg-type]


def make_service(
    *,
    registry: PipelineRegistry | None = None,
    stale_after_seconds: int = 300,
    config_fingerprint: str = CONFIG_FINGERPRINT,
    retry_policy: RetryPolicy | None = None,
) -> PipelineRecoveryService:
    return PipelineRecoveryService(
        session_factory=cast(
            AsyncSessionFactory, object()
        ),  # unused by the pure methods tested here
        registry=registry or PipelineRegistry([]),
        stale_after_seconds=stale_after_seconds,
        config_fingerprint=config_fingerprint,
        retry_policy=retry_policy or RetryPolicy(jitter_source=lambda: 0.0),
    )


# --- _is_stale: strict predicate, no created_at, legacy NULL only at startup


def test_stale_predicate_is_strict_less_than() -> None:
    run = make_run(PipelineRunStatus.RUNNING, heartbeat_at=NOW - timedelta(seconds=300))
    assert not _is_stale(run, now=NOW, stale_after_seconds=300, include_null_heartbeat=False)
    run_just_over = make_run(
        PipelineRunStatus.RUNNING, heartbeat_at=NOW - timedelta(seconds=300, milliseconds=1)
    )
    assert _is_stale(run_just_over, now=NOW, stale_after_seconds=300, include_null_heartbeat=False)


def test_stale_predicate_recent_heartbeat_is_not_stale() -> None:
    run = make_run(PipelineRunStatus.RUNNING, heartbeat_at=NOW - timedelta(seconds=5))
    assert not _is_stale(run, now=NOW, stale_after_seconds=300, include_null_heartbeat=True)


def test_stale_predicate_ignores_created_at() -> None:
    old_created = make_run(
        PipelineRunStatus.RUNNING,
        created_at=NOW - timedelta(days=365),
        heartbeat_at=NOW - timedelta(seconds=5),
    )
    assert not _is_stale(old_created, now=NOW, stale_after_seconds=300, include_null_heartbeat=True)


def test_stale_predicate_null_heartbeat_only_recognized_when_flagged() -> None:
    run = make_run(PipelineRunStatus.RUNNING, heartbeat_at=None)
    assert _is_stale(run, now=NOW, stale_after_seconds=300, include_null_heartbeat=True)
    assert not _is_stale(run, now=NOW, stale_after_seconds=300, include_null_heartbeat=False)


def test_stale_predicate_ignores_terminal_and_queued_status() -> None:
    for status in (
        PipelineRunStatus.QUEUED,
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLED,
    ):
        run = make_run(status, heartbeat_at=None)
        assert not _is_stale(run, now=NOW, stale_after_seconds=300, include_null_heartbeat=True)


# --- RUNNING recovery: legacy B4 no-step detection --------------------------


def test_recover_running_legacy_no_steps_fails_worker_lost() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, attempt=1, heartbeat_at=None)

    service._recover_running(run, [], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    assert run.finished_at == NOW


def test_recover_running_null_heartbeat_with_existing_steps_fails_closed_never_queued() -> None:
    """ADR-0009 SS I "Pre-B5 legacy rollout exception": heartbeat_at IS NULL
    is never valid liveness evidence for the normal stale-reclaim decision --
    this applies uniformly whether steps are absent (legacy no-plan) or
    present (an invalid/unproven state no correctly-functioning B5 claim can
    produce, since claim always sets heartbeat_at alongside RUNNING). Neither
    case may return to QUEUED; both terminalize FAILED/WORKER_LOST, even
    with attempts remaining."""

    service = make_service(retry_policy=RetryPolicy(max_run_attempts=5, jitter_source=lambda: 0.0))
    run = make_run(PipelineRunStatus.RUNNING, attempt=1, heartbeat_at=None)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id)

    service._recover_running(run, [orphan], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    assert run.status != PipelineRunStatus.QUEUED
    assert orphan.status == PipelineStepStatus.FAILED
    assert orphan.error is not None
    assert orphan.error["code"] == WORKER_LOST_ERROR_CODE


# --- RUNNING recovery: config fingerprint -----------------------------------


def test_recover_running_config_fingerprint_mismatch_fails_run_and_orphan_step() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, config_fingerprint=OTHER_CONFIG_FINGERPRINT)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id)

    service._recover_running(run, [orphan], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE
    assert orphan.status == PipelineStepStatus.FAILED
    assert orphan.error is not None
    assert orphan.error["code"] == CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE


# --- RUNNING recovery: attempt ceiling --------------------------------------


def test_recover_running_attempts_exhausted_fails_worker_lost_never_requeues() -> None:
    service = make_service(retry_policy=RetryPolicy(max_run_attempts=5, jitter_source=lambda: 0.0))
    run = make_run(PipelineRunStatus.RUNNING, attempt=5)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id)

    service._recover_running(run, [orphan], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    assert run.next_attempt_at is None
    assert orphan.status == PipelineStepStatus.FAILED


def test_recover_running_attempts_remaining_requeues_with_deterministic_backoff() -> None:
    policy = RetryPolicy(
        max_run_attempts=5,
        backoff_base_seconds=1.0,
        backoff_cap_seconds=60.0,
        jitter_source=lambda: 0.0,
    )
    service = make_service(retry_policy=policy)
    run = make_run(PipelineRunStatus.RUNNING, attempt=2)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id)

    service._recover_running(run, [orphan], now=NOW)

    assert run.status == PipelineRunStatus.QUEUED
    expected_delay = policy.backoff_seconds(2)
    assert run.next_attempt_at == NOW + timedelta(seconds=expected_delay)
    assert orphan.status == PipelineStepStatus.QUEUED
    assert orphan.error is None
    # ADR-0009 SS 21: attempt/worker_id/heartbeat_at/started_at preserved historically.
    assert run.attempt == 2
    assert run.worker_id == "wk-old"
    assert run.started_at is not None


# --- RUNNING recovery: orphan step vs. between-steps ------------------------


def test_recover_running_orphan_step_reset_to_queued() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, attempt=1)
    succeeded = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=0, name="a")
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, ordinal=1, name="b")
    queued = make_step(PipelineStepStatus.QUEUED, run_id=run.id, ordinal=2, name="c")

    service._recover_running(run, [succeeded, orphan, queued], now=NOW)

    assert run.status == PipelineRunStatus.QUEUED
    assert succeeded.status == PipelineStepStatus.SUCCEEDED  # untouched
    assert orphan.status == PipelineStepStatus.QUEUED
    assert orphan.started_at is None
    assert orphan.finished_at is None
    assert queued.status == PipelineStepStatus.QUEUED  # untouched, still queued


def test_recover_running_between_steps_no_orphan_requeues_without_inventing_one() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, attempt=1)
    succeeded = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=0, name="a")
    queued = make_step(PipelineStepStatus.QUEUED, run_id=run.id, ordinal=1, name="b")

    service._recover_running(run, [succeeded, queued], now=NOW)

    assert run.status == PipelineRunStatus.QUEUED
    assert succeeded.status == PipelineStepStatus.SUCCEEDED
    assert queued.status == PipelineStepStatus.QUEUED


# --- RUNNING recovery: invariant violations ---------------------------------


def test_recover_running_multiple_running_steps_fails_safe() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, attempt=1)
    running_a = make_step(PipelineStepStatus.RUNNING, run_id=run.id, ordinal=0, name="a")
    running_b = make_step(PipelineStepStatus.RUNNING, run_id=run.id, ordinal=1, name="b")

    service._recover_running(run, [running_a, running_b], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    assert running_a.status == PipelineStepStatus.FAILED
    assert running_b.status == PipelineStepStatus.FAILED


def test_recover_running_all_succeeded_invariant_fails_safe_never_fabricates_success() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.RUNNING, attempt=1)
    a = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=0, name="a")
    b = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=1, name="b")

    service._recover_running(run, [a, b], now=NOW)

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    assert run.status != PipelineRunStatus.SUCCEEDED


# --- CANCELLING recovery: legacy B4 no-step detection -----------------------


def test_recover_cancelling_legacy_no_steps_fails_ambiguous_never_fabricates_cancelled() -> None:
    """ADR-0009 SS I "Pre-B5 legacy rollout exception": a pre-B5 CANCELLING
    run has no PipelineStep to classify into case A/B/C, so recovery must
    never assume vacuous case A and report CANCELLED -- it fails safe."""

    service = make_service()
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert run.status != PipelineRunStatus.CANCELLED


def test_recover_cancelling_null_heartbeat_with_existing_steps_fails_closed_never_cancelled() -> (
    None
):
    """ADR-0009 SS I "Pre-B5 legacy rollout exception" case C: heartbeat_at
    IS NULL with a persisted step present must never be resolved via normal
    A/B/C classification (even a step declared ATOMIC) and must never be
    reported CANCELLED -- there is no valid liveness evidence to trust any
    classification against."""

    registry = _registry_with_mode(CancellationRecoveryMode.ATOMIC)
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1, heartbeat_at=None)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert run.status != PipelineRunStatus.CANCELLED
    assert orphan.status == PipelineStepStatus.FAILED
    assert orphan.error is not None
    assert orphan.error["code"] == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE


# --- CANCELLING recovery: no running step -----------------------------------


def test_recover_cancelling_no_running_step_cancels_queued_and_run() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    succeeded = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=0, name="a")
    queued = make_step(PipelineStepStatus.QUEUED, run_id=run.id, ordinal=1, name="b")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [succeeded, queued], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.CANCELLED
    assert succeeded.status == PipelineStepStatus.SUCCEEDED
    assert queued.status == PipelineStepStatus.CANCELLED


def test_recover_cancelling_all_steps_already_succeeded_reports_progress_one() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1, progress=0.5)
    a = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=0, name="a")
    b = make_step(PipelineStepStatus.SUCCEEDED, run_id=run.id, ordinal=1, name="b")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [a, b], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.CANCELLED
    assert run.progress == 1.0


# --- CANCELLING recovery: case A/B/C, with a real registry ------------------


class _NoopStep:
    async def execute(self, context: object) -> StepResult:
        del context
        return StepResult()

    async def persist(self, context: object, result: StepResult, uow: object) -> None:
        no_op_persist(context, result, uow)  # type: ignore[arg-type]

    async def compensate(self, context: object, result: StepResult) -> None:
        no_op_compensate(context, result)  # type: ignore[arg-type]


async def _reconcile_safe(
    context: PipelineCancellationRecoveryContext, uow: object
) -> CancellationRecoveryOutcome:
    del context, uow
    return CancellationRecoveryOutcome.SAFE


async def _reconcile_inconclusive(
    context: PipelineCancellationRecoveryContext, uow: object
) -> CancellationRecoveryOutcome:
    del context, uow
    return CancellationRecoveryOutcome.INCONCLUSIVE


async def _reconcile_raises(
    context: PipelineCancellationRecoveryContext, uow: object
) -> CancellationRecoveryOutcome:
    del context, uow
    raise RuntimeError("cannot reach durable evidence")


def _registry_with_mode(
    mode: CancellationRecoveryMode,
    *,
    reconcile: object | None = None,
) -> PipelineRegistry:
    step_def = PipelineStepDefinition(
        name="do_thing",
        definition_id="do_thing@v1",
        step=_NoopStep(),
        input_deriver=lambda run_input, step_outputs: {},
        cancellation_recovery_mode=mode,
        cancellation_reconcile=reconcile,  # type: ignore[arg-type]
    )
    definition = PipelineDefinition(pipeline_type=PipelineType.REMEMBER, steps=(step_def,))
    return PipelineRegistry([definition])


def test_recover_cancelling_case_a_atomic_cancels_without_calling_reconcile() -> None:
    registry = _registry_with_mode(CancellationRecoveryMode.ATOMIC)
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")
    queued = make_step(PipelineStepStatus.QUEUED, run_id=run.id, ordinal=1, name="other")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan, queued], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.CANCELLED
    assert orphan.status == PipelineStepStatus.CANCELLED
    assert queued.status == PipelineStepStatus.CANCELLED


def test_recover_cancelling_case_b_reconcilable_safe_cancels() -> None:
    registry = _registry_with_mode(CancellationRecoveryMode.RECONCILABLE, reconcile=_reconcile_safe)
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.CANCELLED
    assert orphan.status == PipelineStepStatus.CANCELLED


def test_recover_cancelling_case_b_reconcilable_inconclusive_fails_ambiguous() -> None:
    registry = _registry_with_mode(
        CancellationRecoveryMode.RECONCILABLE, reconcile=_reconcile_inconclusive
    )
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert orphan.status == PipelineStepStatus.FAILED
    assert orphan.error is not None
    assert orphan.error["code"] == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE


def test_recover_cancelling_case_b_reconcile_callback_exception_is_fail_safe() -> None:
    registry = _registry_with_mode(
        CancellationRecoveryMode.RECONCILABLE, reconcile=_reconcile_raises
    )
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE


def test_recover_cancelling_case_c_ambiguous_default_fails_never_reports_cancelled() -> None:
    registry = _registry_with_mode(CancellationRecoveryMode.AMBIGUOUS)
    service = make_service(registry=registry)
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="do_thing")
    queued = make_step(PipelineStepStatus.QUEUED, run_id=run.id, ordinal=1, name="other")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan, queued], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert orphan.status == PipelineStepStatus.FAILED
    # Remaining QUEUED steps are left as unexecuted history, matching the
    # engine's own precedent for a permanent failure observed while CANCELLING.
    assert queued.status == PipelineStepStatus.QUEUED


def test_recover_cancelling_missing_registry_definition_is_ambiguous() -> None:
    service = make_service(registry=PipelineRegistry([]))
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1, pipeline_type=PipelineType.FORGET)
    orphan = make_step(PipelineStepStatus.RUNNING, run_id=run.id, name="forget_source")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [orphan], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE


def test_recover_cancelling_multiple_running_steps_fails_ambiguous() -> None:
    service = make_service()
    run = make_run(PipelineRunStatus.CANCELLING, attempt=1)
    running_a = make_step(PipelineStepStatus.RUNNING, run_id=run.id, ordinal=0, name="a")
    running_b = make_step(PipelineStepStatus.RUNNING, run_id=run.id, ordinal=1, name="b")

    import asyncio

    asyncio.run(service._recover_cancelling(object(), run, [running_a, running_b], now=NOW))  # type: ignore[arg-type]

    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert running_a.status == PipelineStepStatus.FAILED
    assert running_b.status == PipelineStepStatus.FAILED


# --- PipelineStepDefinition contract ----------------------------------------


def test_reconcilable_without_callback_raises_at_definition_time() -> None:
    with pytest.raises(ValueError, match="RECONCILABLE"):
        PipelineStepDefinition(
            name="do_thing",
            definition_id="do_thing@v1",
            step=_NoopStep(),
            input_deriver=lambda run_input, step_outputs: {},
            cancellation_recovery_mode=CancellationRecoveryMode.RECONCILABLE,
            cancellation_reconcile=None,
        )


def test_default_cancellation_recovery_mode_is_ambiguous() -> None:
    step_def = PipelineStepDefinition(
        name="do_thing",
        definition_id="do_thing@v1",
        step=_NoopStep(),
        input_deriver=lambda run_input, step_outputs: {},
    )
    assert step_def.cancellation_recovery_mode == CancellationRecoveryMode.AMBIGUOUS
