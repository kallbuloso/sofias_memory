"""Real-PostgreSQL tests for stale PipelineRun/PipelineStep recovery (SM-507,
ADR-0009 SS I). Requires a dedicated, discardable database at Alembic head
0009 -- SM-507 adds no migration.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.engine import PipelineEngine
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
from sofias_memory.services.pipeline_lifecycle import create_run_with_steps
from sofias_memory.services.pipeline_queue_claimer import PipelineRunClaimer
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService

RECOVERY_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_PIPELINE_RECOVERY_POSTGRES_TESTS"
RECOVERY_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_PIPELINE_RECOVERY_TEST_DATABASE_URL"
RECOVERY_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_pipeline_recovery_test"

CONFIG_FINGERPRINT = "a" * 64
OTHER_CONFIG_FINGERPRINT = "b" * 64
STALE_AFTER_SECONDS = 300


def recovery_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(RECOVERY_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {RECOVERY_POSTGRES_TESTS_ENV}=1 to run pipeline recovery PostgreSQL tests"
        )
    database_url = env.get(RECOVERY_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {RECOVERY_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    _validate_recovery_test_database_url(database_url)
    return database_url


def _validate_recovery_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("pipeline recovery PostgreSQL test database URL is invalid")
    if parsed_url.database != RECOVERY_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "pipeline recovery PostgreSQL tests require the exact dedicated database "
            f"{RECOVERY_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = recovery_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_recovery_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_recovery_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != RECOVERY_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated pipeline recovery test database"
        )


def test_recovery_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        recovery_test_database_url({})


def test_recovery_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        recovery_test_database_url({RECOVERY_POSTGRES_TESTS_ENV: "1"})


def test_recovery_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        recovery_test_database_url(
            {
                RECOVERY_POSTGRES_TESTS_ENV: "1",
                RECOVERY_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fixtures / helpers ------------------------------------------------------


@dataclass
class RecoveryIds:
    dataset_ids: list[UUID] = field(default_factory=list)
    run_ids: list[UUID] = field(default_factory=list)


@pytest_asyncio.fixture()
async def recovery_ids(postgres_engine: AsyncEngine) -> AsyncIterator[RecoveryIds]:
    ids = RecoveryIds()
    yield ids
    async with postgres_engine.begin() as connection:
        if ids.run_ids:
            await connection.execute(
                text("DELETE FROM pipeline_steps WHERE run_id = ANY(:ids)"),
                {"ids": ids.run_ids},
            )
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"),
                {"ids": ids.run_ids},
            )
        if ids.dataset_ids:
            await connection.execute(
                text("DELETE FROM datasets WHERE id = ANY(:ids)"),
                {"ids": ids.dataset_ids},
            )


async def insert_dataset(engine: AsyncEngine, ids: RecoveryIds) -> UUID:
    dataset_id = uuid4()
    ids.dataset_ids.append(dataset_id)
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(id=dataset_id, name=f"recovery-{dataset_id}", slug=f"recovery-{dataset_id}")
        )
        await uow.commit()
    return dataset_id


async def get_database_now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as connection:
        value = await connection.scalar(text("SELECT now()"))
    assert value is not None
    return cast(datetime, value)


async def insert_run(
    engine: AsyncEngine,
    ids: RecoveryIds,
    *,
    dataset_id: UUID | None,
    status: PipelineRunStatus,
    pipeline_type: PipelineType = PipelineType.REMEMBER,
    attempt: int = 1,
    worker_id: str | None = "wk-dead",
    heartbeat_at: datetime | None,
    config_fingerprint: str = CONFIG_FINGERPRINT,
    progress: float = 0.0,
) -> UUID:
    run_id = uuid4()
    ids.run_ids.append(run_id)
    now = await get_database_now(engine)
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = PipelineRun(
            id=run_id,
            pipeline_type=pipeline_type,
            dataset_id=dataset_id,
            source_id=None,
            status=status,
            idempotency_key=None,
            payload_hash="a" * 64,
            input={},
            progress=progress,
            current_step=None,
            attempt=attempt,
            worker_id=worker_id,
            heartbeat_at=heartbeat_at,
            config_fingerprint=config_fingerprint,
            error_code=None,
            error_message=None,
            metrics={},
            started_at=now - timedelta(seconds=700),
            finished_at=None,
            next_attempt_at=None,
            retry_of_run_id=None,
        )
        await uow.pipeline_runs.add(run)
        await uow.commit()
    return run_id


async def insert_step(
    engine: AsyncEngine,
    *,
    run_id: UUID,
    name: str,
    ordinal: int,
    status: PipelineStepStatus,
    attempt: int = 1,
    output: dict[str, Any] | None = None,
) -> UUID:
    step_id = uuid4()
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        step = PipelineStep(
            id=step_id,
            run_id=run_id,
            name=name,
            ordinal=ordinal,
            status=status,
            attempt=attempt,
            input_hash=None,
            output=output or {},
            metrics={},
            error=None,
            started_at=None,
            finished_at=None,
        )
        await uow.pipeline_steps.add(step)
        await uow.commit()
    return step_id


@dataclass(frozen=True)
class RunSnapshot:
    status: PipelineRunStatus
    error_code: str | None
    error_message: str | None
    attempt: int
    next_attempt_at: datetime | None
    worker_id: str | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    progress: float


@dataclass(frozen=True)
class StepSnapshot:
    name: str
    status: PipelineStepStatus
    error: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    attempt: int


async def read_run(engine: AsyncEngine, run_id: UUID) -> RunSnapshot:
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        assert run is not None
        # Detach a plain snapshot while the session is still open: this
        # read-only UnitOfWork never calls commit(), so __aexit__ rolls back
        # on the way out, which expires every ORM attribute (SM-506's own
        # documented DetachedInstanceError pitfall).
        return RunSnapshot(
            status=run.status,
            error_code=run.error_code,
            error_message=run.error_message,
            attempt=run.attempt,
            next_attempt_at=run.next_attempt_at,
            worker_id=run.worker_id,
            heartbeat_at=run.heartbeat_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            progress=run.progress,
        )


async def read_steps(engine: AsyncEngine, run_id: UUID) -> list[StepSnapshot]:
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        steps = await uow.pipeline_steps.list_for_run(run_id)
        return [
            StepSnapshot(
                name=step.name,
                status=step.status,
                error=step.error,
                started_at=step.started_at,
                finished_at=step.finished_at,
                attempt=step.attempt,
            )
            for step in steps
        ]


def make_service(
    engine: AsyncEngine,
    *,
    registry: PipelineRegistry | None = None,
    config_fingerprint: str = CONFIG_FINGERPRINT,
    retry_policy: RetryPolicy | None = None,
) -> PipelineRecoveryService:
    return PipelineRecoveryService(
        create_session_factory(engine),
        registry or PipelineRegistry([]),
        stale_after_seconds=STALE_AFTER_SECONDS,
        config_fingerprint=config_fingerprint,
        retry_policy=retry_policy or RetryPolicy(jitter_source=lambda: 0.0),
    )


async def backdate_heartbeat(engine: AsyncEngine, run_id: UUID, *, seconds: float) -> None:
    """Push ``heartbeat_at`` into the stale window using PostgreSQL's own
    clock (never the host's), simulating a worker that stopped heartbeating
    without needing a real sleep."""

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE pipeline_runs SET heartbeat_at = now() - make_interval(secs => :seconds) "
                "WHERE id = :id"
            ),
            {"id": run_id, "seconds": seconds},
        )


# --- Scenario A: healthy RUNNING is never touched ---------------------------


@pytest.mark.asyncio
async def test_scenario_a_healthy_running_not_touched(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        heartbeat_at=now - timedelta(seconds=5),
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 0
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.RUNNING
    assert run.worker_id == "wk-dead"
    assert run.heartbeat_at is not None


# --- Scenario D: attempts exhausted -----------------------------------------


@pytest.mark.asyncio
async def test_scenario_d_attempts_exhausted_fails_worker_lost(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        attempt=5,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service = make_service(postgres_engine, retry_policy=RetryPolicy(max_run_attempts=5))
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    steps = await read_steps(postgres_engine, run_id)
    assert steps[0].status == PipelineStepStatus.FAILED


# --- Scenario E: config fingerprint mismatch --------------------------------


@pytest.mark.asyncio
async def test_scenario_e_config_fingerprint_mismatch_fails_safely(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        heartbeat_at=now - timedelta(seconds=600),
        config_fingerprint=OTHER_CONFIG_FINGERPRINT,
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service = make_service(postgres_engine, config_fingerprint=CONFIG_FINGERPRINT)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE
    steps = await read_steps(postgres_engine, run_id)
    assert steps[0].status == PipelineStepStatus.FAILED


# --- Scenario B/C: RUNNING stale mid-step / between-steps -> QUEUED --------


@pytest.mark.asyncio
async def test_scenario_b_running_stale_mid_step_requeues_and_resumes_via_normal_claim(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        attempt=1,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.SUCCEEDED
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="b", ordinal=1, status=PipelineStepStatus.RUNNING
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="c", ordinal=2, status=PipelineStepStatus.QUEUED
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.QUEUED
    assert run.next_attempt_at is not None
    assert run.attempt == 1  # unchanged by recovery; claim increments it later
    steps = {step.name: step for step in await read_steps(postgres_engine, run_id)}
    assert steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert steps["b"].status == PipelineStepStatus.QUEUED
    assert steps["c"].status == PipelineStepStatus.QUEUED


@pytest.mark.asyncio
async def test_scenario_c_running_stale_between_steps_requeues_without_inventing_orphan(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.SUCCEEDED
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="b", ordinal=1, status=PipelineStepStatus.QUEUED
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.QUEUED
    steps = {step.name: step for step in await read_steps(postgres_engine, run_id)}
    assert steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert steps["b"].status == PipelineStepStatus.QUEUED


# --- Scenario F/G/H: CANCELLING cases A/B/C ---------------------------------


@pytest.mark.asyncio
async def test_scenario_f_cancelling_case_a_atomic_cancels_run(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    registry = _registry_with_mode(CancellationRecoveryMode.ATOMIC)
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.SUCCEEDED
    )
    await insert_step(
        postgres_engine,
        run_id=run_id,
        name="do_thing",
        ordinal=1,
        status=PipelineStepStatus.RUNNING,
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="c", ordinal=2, status=PipelineStepStatus.QUEUED
    )

    service = make_service(postgres_engine, registry=registry)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.CANCELLED
    steps = {step.name: step for step in await read_steps(postgres_engine, run_id)}
    assert steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert steps["do_thing"].status == PipelineStepStatus.CANCELLED
    assert steps["c"].status == PipelineStepStatus.CANCELLED


@pytest.mark.asyncio
async def test_scenario_g_cancelling_case_b_reconcilable_safe_cancels(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    registry = _registry_with_mode(CancellationRecoveryMode.RECONCILABLE, reconcile=_reconcile_safe)
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine,
        run_id=run_id,
        name="do_thing",
        ordinal=0,
        status=PipelineStepStatus.RUNNING,
    )

    service = make_service(postgres_engine, registry=registry)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_scenario_g_cancelling_case_b_reconcilable_inconclusive_fails_ambiguous(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    registry = _registry_with_mode(
        CancellationRecoveryMode.RECONCILABLE, reconcile=_reconcile_inconclusive
    )
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine,
        run_id=run_id,
        name="do_thing",
        ordinal=0,
        status=PipelineStepStatus.RUNNING,
    )

    service = make_service(postgres_engine, registry=registry)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE


@pytest.mark.asyncio
async def test_scenario_h_cancelling_case_c_ambiguous_never_reports_cancelled(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine,
        run_id=run_id,
        name="do_thing",
        ordinal=0,
        status=PipelineStepStatus.RUNNING,
    )

    # No registry definition at all -> AMBIGUOUS by construction.
    service = make_service(postgres_engine, registry=PipelineRegistry([]))
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert run.status != PipelineRunStatus.CANCELLED


# --- Scenario J: CANCELLING after all steps already succeeded --------------


@pytest.mark.asyncio
async def test_scenario_j_cancelling_after_final_step_never_succeeded(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
        progress=0.5,
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.SUCCEEDED
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="b", ordinal=1, status=PipelineStepStatus.SUCCEEDED
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.CANCELLED
    assert run.progress == 1.0


# --- Scenario K: concurrent recovery ----------------------------------------


@pytest.mark.asyncio
async def test_scenario_k_concurrent_recovery_reconciles_exactly_once(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        attempt=1,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service_1 = make_service(postgres_engine)
    service_2 = make_service(postgres_engine)

    results = await asyncio.gather(service_1.recover_startup(), service_2.recover_startup())

    assert sum(results) == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.QUEUED
    steps = await read_steps(postgres_engine, run_id)
    assert steps[0].status == PipelineStepStatus.QUEUED
    assert steps[0].attempt == 1  # never duplicated/incremented by recovery


# --- Scenario L: restart idempotency ----------------------------------------


@pytest.mark.asyncio
async def test_scenario_l_restart_idempotency_second_pass_recovers_nothing(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service = make_service(postgres_engine)
    first = await service.recover_startup()
    run_after_first = await read_run(postgres_engine, run_id)

    second = await service.recover_startup()
    run_after_second = await read_run(postgres_engine, run_id)

    assert first == 1
    assert second == 0
    assert run_after_first.status == run_after_second.status == PipelineRunStatus.QUEUED
    assert run_after_first.next_attempt_at == run_after_second.next_attempt_at


# --- Legacy B4 Forget: obligatory scenario (SM-507 SS 51) -------------------


@pytest.mark.asyncio
async def test_legacy_b4_forget_run_abandoned_recovers_as_worker_lost_and_unblocks_retry(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    # Exactly what ForgetService._create_running_run persists: RUNNING,
    # zero PipelineStep rows, heartbeat_at=NULL (Gate A, confirmed by direct
    # inspection of sofias_memory/services/forget.py).
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        attempt=1,
        worker_id=None,
        heartbeat_at=None,
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    # No phantom RUNNING owner remains -- a fresh Forget request for this
    # same dataset is free to proceed under FR-090's own conflict rules
    # (find_running_forget_for_dataset_except finds nothing RUNNING).
    del now


# --- Legacy B4: heartbeat_at NULL + existing steps fails closed (case C) --


@pytest.mark.asyncio
async def test_null_heartbeat_with_existing_steps_fails_closed_never_queued(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    """ADR-0009 SS I "Pre-B5 legacy rollout exception" case C: heartbeat_at
    IS NULL is never valid liveness evidence for the normal stale-reclaim
    decision, whether steps are absent (case B, legacy no-plan) or present
    (case C, invalid/unproven state) -- neither may return to QUEUED."""

    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        attempt=1,
        heartbeat_at=None,
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == WORKER_LOST_ERROR_CODE
    steps = await read_steps(postgres_engine, run_id)
    assert steps[0].status == PipelineStepStatus.FAILED


@pytest.mark.asyncio
async def test_cancelling_null_heartbeat_with_existing_steps_fails_closed_never_cancelled(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    """ADR-0009 SS I "Pre-B5 legacy rollout exception" case C for CANCELLING:
    heartbeat_at IS NULL with a persisted step present must never be
    resolved via normal A/B/C classification and must never be reported
    CANCELLED."""

    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        attempt=1,
        heartbeat_at=None,
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    steps = await read_steps(postgres_engine, run_id)
    assert steps[0].status == PipelineStepStatus.FAILED


# --- Scenario I: CANCELLING at safe boundary, no orphan RUNNING step -------


@pytest.mark.asyncio
async def test_scenario_i_cancelling_at_safe_boundary_no_orphan_step(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.CANCELLING,
        heartbeat_at=now - timedelta(seconds=600),
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.SUCCEEDED
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="b", ordinal=1, status=PipelineStepStatus.QUEUED
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="c", ordinal=2, status=PipelineStepStatus.QUEUED
    )

    service = make_service(postgres_engine)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.CANCELLED
    steps = {step.name: step for step in await read_steps(postgres_engine, run_id)}
    assert steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert steps["b"].status == PipelineStepStatus.CANCELLED
    assert steps["c"].status == PipelineStepStatus.CANCELLED


# --- Config-change restart: recovered run is never reclaimed ---------------


@pytest.mark.asyncio
async def test_config_change_restart_failed_run_is_never_reclaimed_by_claimer(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    """BOOT A fingerprint A, BOOT B fingerprint B, stale run -> FAILED
    CONFIG_FINGERPRINT_MISMATCH -> the real PipelineRunClaimer never reclaims it."""

    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    now = await get_database_now(postgres_engine)
    run_id = await insert_run(
        postgres_engine,
        recovery_ids,
        dataset_id=dataset_id,
        status=PipelineRunStatus.RUNNING,
        heartbeat_at=now - timedelta(seconds=600),
        config_fingerprint=OTHER_CONFIG_FINGERPRINT,  # boot A's fingerprint
    )
    await insert_step(
        postgres_engine, run_id=run_id, name="a", ordinal=0, status=PipelineStepStatus.RUNNING
    )

    # BOOT B: current process fingerprint differs from what boot A claimed under.
    service = make_service(postgres_engine, config_fingerprint=CONFIG_FINGERPRINT)
    recovered = await service.recover_startup()

    assert recovered == 1
    run = await read_run(postgres_engine, run_id)
    assert run.status == PipelineRunStatus.FAILED
    assert run.error_code == CONFIG_FINGERPRINT_MISMATCH_ERROR_CODE

    claimer = PipelineRunClaimer(create_session_factory(postgres_engine))
    claimed = await claimer.try_claim_one(worker_id="wk-boot-b")

    assert claimed is None  # FAILED is not queued/eligible -- never reclaimed.


# --- Restart-equivalent end-to-end: real claim + real engine resume --------


@dataclass
class _RecordingStep:
    name: str
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: object) -> None:
        no_op_persist(context, result, uow)  # type: ignore[arg-type]

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)  # type: ignore[arg-type]


@dataclass
class _PausableStep:
    """``execute()`` signals ``entered`` then blocks on ``proceed`` -- lets a
    test simulate a crash mid-step by cancelling the engine's execution task
    while this step is genuinely RUNNING and persisted as such."""

    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.proceed.wait(), timeout=30.0)
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: object) -> None:
        no_op_persist(context, result, uow)  # type: ignore[arg-type]

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)  # type: ignore[arg-type]


def _const_deriver(
    run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]
) -> Mapping[str, Any]:
    del step_outputs
    return {"value": run_input.get("seed")}


@pytest.mark.asyncio
async def test_restart_equivalent_e2e_orphan_step_resumes_via_normal_claim_and_engine(
    postgres_engine: AsyncEngine, recovery_ids: RecoveryIds
) -> None:
    """ADR-0009 SS 54: BOOT A claims, step A succeeds, step B is mid-flight
    when the process crashes (heartbeat stops advancing); BOOT B's startup
    recovery requeues it, the real :class:`PipelineRunClaimer` reclaims it
    (new worker_id, attempt incremented), and the real :class:`PipelineEngine`
    resumes exactly at step B -- step A is never re-executed, step B executes
    exactly once more, and the run reaches SUCCEEDED."""

    step_a = _RecordingStep(name="a")
    step_b = _PausableStep(name="b")
    step_c = _RecordingStep(name="c")
    registry = PipelineRegistry(
        [
            PipelineDefinition(
                pipeline_type=PipelineType.REMEMBER,
                steps=(
                    PipelineStepDefinition(
                        name="a", definition_id="a:v1", step=step_a, input_deriver=_const_deriver
                    ),
                    PipelineStepDefinition(
                        name="b", definition_id="b:v1", step=step_b, input_deriver=_const_deriver
                    ),
                    PipelineStepDefinition(
                        name="c", definition_id="c:v1", step=step_c, input_deriver=_const_deriver
                    ),
                ),
            )
        ]
    )

    dataset_id = await insert_dataset(postgres_engine, recovery_ids)
    session_factory = create_session_factory(postgres_engine)
    run_input = {"seed": "x"}
    plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=run_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=run_input,
            config_fingerprint=CONFIG_FINGERPRINT,
            steps=plan,
        )
        await uow.commit()
    run_id = run.id
    recovery_ids.run_ids.append(run_id)

    # -- BOOT A ---------------------------------------------------------
    claimer_a = PipelineRunClaimer(session_factory)
    claimed_a = await claimer_a.try_claim_one(worker_id="wk-boot-a")
    assert claimed_a is not None
    assert claimed_a.attempt == 1

    engine_a = PipelineEngine(session_factory, registry)
    execute_task = asyncio.create_task(engine_a.execute(claimed_a))
    await asyncio.wait_for(step_b.entered.wait(), timeout=10.0)

    # Simulate process death: abandon the in-flight execution (business
    # execute() phase is never wrapped in the cancellation-safe transactional
    # shield -- ADR-0009 SS 17/18 -- so this leaves step B genuinely
    # persisted RUNNING with no further mutation).
    execute_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execute_task
    await backdate_heartbeat(postgres_engine, run_id, seconds=600)

    pre_recovery_steps = {s.name: s for s in await read_steps(postgres_engine, run_id)}
    assert pre_recovery_steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert pre_recovery_steps["b"].status == PipelineStepStatus.RUNNING
    assert pre_recovery_steps["c"].status == PipelineStepStatus.QUEUED

    # -- BOOT B: startup recovery -----------------------------------------
    # Zero backoff base so the requeued run is immediately claim-eligible
    # (next_attempt_at <= now()) without the test waiting out real backoff.
    recovery_service = make_service(
        postgres_engine,
        registry=registry,
        retry_policy=RetryPolicy(backoff_base_seconds=0.0, jitter_source=lambda: 0.0),
    )
    recovered = await recovery_service.recover_startup()
    assert recovered == 1

    after_recovery_run = await read_run(postgres_engine, run_id)
    assert after_recovery_run.status == PipelineRunStatus.QUEUED
    after_recovery_steps = {s.name: s for s in await read_steps(postgres_engine, run_id)}
    assert after_recovery_steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert after_recovery_steps["b"].status == PipelineStepStatus.QUEUED

    # -- BOOT B: normal claim + normal engine resume ----------------------
    claimer_b = PipelineRunClaimer(session_factory)
    claimed_b = await claimer_b.try_claim_one(worker_id="wk-boot-b")
    assert claimed_b is not None
    assert claimed_b.attempt == 2  # incremented by the normal claim, not by recovery
    assert claimed_b.worker_id == "wk-boot-b"

    step_b.proceed.set()  # let the resumed step B complete immediately this time
    engine_b = PipelineEngine(session_factory, registry)
    result = await engine_b.execute(claimed_b)

    assert result.status == PipelineRunStatus.SUCCEEDED
    assert step_a.calls == 1  # never re-executed
    assert step_b.calls == 2  # once aborted mid-flight, once resumed
    assert step_c.calls == 1

    final_run = await read_run(postgres_engine, run_id)
    assert final_run.status == PipelineRunStatus.SUCCEEDED
    assert final_run.worker_id == "wk-boot-b"
    assert final_run.attempt == 2
    final_steps = {s.name: s for s in await read_steps(postgres_engine, run_id)}
    assert final_steps["a"].status == PipelineStepStatus.SUCCEEDED
    assert final_steps["b"].status == PipelineStepStatus.SUCCEEDED
    assert final_steps["b"].attempt == 2  # step-level attempt: RUNNING twice
    assert final_steps["c"].status == PipelineStepStatus.SUCCEEDED


# --- Registry helpers --------------------------------------------------------


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
    other_step = PipelineStepDefinition(
        name="c",
        definition_id="c@v1",
        step=_NoopStep(),
        input_deriver=lambda run_input, step_outputs: {},
    )
    a_step = PipelineStepDefinition(
        name="a",
        definition_id="a@v1",
        step=_NoopStep(),
        input_deriver=lambda run_input, step_outputs: {},
    )
    definition = PipelineDefinition(
        pipeline_type=PipelineType.REMEMBER, steps=(a_step, step_def, other_step)
    )
    return PipelineRegistry([definition])
