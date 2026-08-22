"""Real-PostgreSQL tests for the internal worker coordinator (SM-505,
ADR-0009 SS T/U/G/H and related sections): queue -> engine end-to-end
dispatch, per-boot worker identity, concurrency capacity, heartbeat
(RUNNING/CANCELLING/fencing), retry releasing a capacity slot, cooperative
shutdown (between steps, during the last step, and forced after grace),
the claim-in-progress shutdown race, and unexpected-exception task
isolation.

Requires a dedicated, discardable PostgreSQL database with migrations
already applied through 0008. Uses fake, deterministic in-process pipeline
steps only -- no Neo4j, no LLM/embedding provider, no filesystem.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.registry import (
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
)
from sofias_memory.services.pipeline_lifecycle import StepPlan, create_run_with_steps
from sofias_memory.services.pipeline_queue_claimer import PipelineRunClaimer
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

WORKER_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_PIPELINE_WORKER_POSTGRES_TESTS"
WORKER_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_PIPELINE_WORKER_TEST_DATABASE_URL"
WORKER_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_pipeline_worker_test"

TEST_TIMEOUT = 10.0
POLL_INTERVAL_MS = 20


def worker_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(WORKER_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {WORKER_POSTGRES_TESTS_ENV}=1 to run pipeline worker PostgreSQL tests")

    database_url = env.get(WORKER_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {WORKER_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_worker_test_database_url(database_url)
    return database_url


def _validate_worker_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("pipeline worker PostgreSQL test database URL is invalid")

    if parsed_url.database != WORKER_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "pipeline worker PostgreSQL tests require the exact dedicated database "
            f"{WORKER_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = worker_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_worker_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_worker_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != WORKER_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated pipeline worker test database"
        )


def test_worker_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        worker_test_database_url({})


def test_worker_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        worker_test_database_url(
            {
                WORKER_POSTGRES_TESTS_ENV: "1",
                WORKER_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fake, deterministic steps -----------------------------------------------


def const_deriver(key: str) -> Any:
    def _deriver(
        run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del step_outputs
        return {"value": run_input.get(key)}

    return _deriver


@dataclass
class RecordingStep:
    name: str
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class RetryOnceStep:
    name: str
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        from sofias_memory.pipelines.errors import RetryablePipelineStepError

        del context
        self.calls += 1
        if self.calls == 1:
            raise RetryablePipelineStepError("TRANSIENT", "Simulated transient failure.")
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class PausableStep:
    """Blocks on ``proceed`` after signalling ``entered``, letting a test
    mutate/observe state from an independent connection while ``execute()``
    is in flight -- same pattern as SM-504's own engine integration tests."""

    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.proceed.wait(), timeout=TEST_TIMEOUT)
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class NeverFinishesStep:
    """Never returns -- used to prove forced cancellation after grace expiry."""

    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class PausableTransactionalStep:
    """``execute()`` returns immediately; ``persist()`` applies a real
    authoritative PostgreSQL mutation (an extra marker ``Dataset`` row, same
    technique as SM-504's own ``TransactionalStep``) and then pauses --
    signalling ``entered_persist`` first -- so a test can race a forced
    ``task.cancel()`` against this exact in-flight, already-open
    transaction (SM-505 forced-shutdown audit, scenario M)."""

    name: str
    marker_dataset_id: UUID = field(default_factory=uuid4)
    entered_persist: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    persist_calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result
        self.persist_calls += 1
        await uow.datasets.add(
            Dataset(
                id=self.marker_dataset_id,
                name=f"marker-{self.marker_dataset_id}",
                slug=f"marker-{self.marker_dataset_id}",
            )
        )
        self.entered_persist.set()
        await asyncio.wait_for(self.proceed.wait(), timeout=TEST_TIMEOUT)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


def step_definition(name: str, step: Any) -> PipelineStepDefinition:
    return PipelineStepDefinition(
        name=name, definition_id=f"{name}:v1", step=step, input_deriver=const_deriver("seed")
    )


class FlakyClaimer:
    """Wraps a real :class:`PipelineRunClaimer`; its first ``try_claim_one``
    call raises a simulated infrastructure error, every subsequent call
    delegates normally -- proves SM-505 SS 29 (a transient claim-scan
    failure is consumed/logged, never busy-spun, and the next poll tick
    retries normally)."""

    def __init__(self, inner: PipelineRunClaimer) -> None:
        self._inner = inner
        self.calls = 0
        self.call_times: list[float] = []

    async def try_claim_one(self, *, worker_id: str) -> Any:
        self.calls += 1
        self.call_times.append(asyncio.get_event_loop().time())
        if self.calls == 1:
            raise RuntimeError("simulated transient claim infrastructure failure")
        return await self._inner.try_claim_one(worker_id=worker_id)


class DelayedClaimer:
    """Wraps a real :class:`PipelineRunClaimer`, holding a claim-in-flight
    open until a test releases it -- proves SM-505 SS 26 (a claim already
    started when shutdown is requested must still be dispatched, never
    discarded)."""

    def __init__(self, inner: PipelineRunClaimer) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.used = False

    async def try_claim_one(self, *, worker_id: str) -> Any:
        if not self.used:
            self.used = True
            self.entered.set()
            await asyncio.wait_for(self.proceed.wait(), timeout=TEST_TIMEOUT)
        return await self._inner.try_claim_one(worker_id=worker_id)


# --- fixtures/helpers ---------------------------------------------------------


@dataclass
class WorkerTestIds:
    dataset_ids: list[UUID] = field(default_factory=list)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_dataset(session_factory: Any, ids: WorkerTestIds) -> UUID:
    dataset_id = uuid4()
    ids.dataset_ids.append(dataset_id)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(id=dataset_id, name=f"worker-{dataset_id}", slug=f"worker-{dataset_id}")
        )
        await uow.commit()
    return dataset_id


async def cleanup_worker_fixture(engine: AsyncEngine, ids: WorkerTestIds) -> None:
    async with engine.begin() as connection:
        if ids.run_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"), {"ids": ids.run_ids}
            )
        if ids.dataset_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE dataset_id = ANY(:ids)"),
                {"ids": ids.dataset_ids},
            )
            await connection.execute(
                text("DELETE FROM datasets WHERE id = ANY(:ids)"), {"ids": ids.dataset_ids}
            )


async def submit_run(
    session_factory: Any,
    ids: WorkerTestIds,
    *,
    registry: PipelineRegistry,
    dataset_id: UUID | None,
    pipeline_type: PipelineType = PipelineType.COGNIFY,
) -> UUID:
    run_input = {"seed": "x"}
    plan = registry.build_step_plan(pipeline_type, run_input=run_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=pipeline_type,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=run_input,
            config_fingerprint="b" * 64,
            steps=plan,
        )
        await uow.commit()
    ids.run_ids.append(run.id)
    return run.id


async def submit_run_with_unregistered_type(
    session_factory: Any,
    ids: WorkerTestIds,
    *,
    dataset_id: UUID | None,
    step_names: list[str],
) -> UUID:
    """Materializes a run whose ``pipeline_type`` has no matching
    :class:`PipelineDefinition` in the worker's registry -- simulating a run
    claimable by SM-503 but not executable by the engine (SM-505 SS 32)."""

    plan = [StepPlan(name=name, ordinal=index) for index, name in enumerate(step_names)]
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.FORGET,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input={"seed": "x"},
            config_fingerprint="b" * 64,
            steps=plan,
        )
        await uow.commit()
    ids.run_ids.append(run.id)
    return run.id


async def read_run(session_factory: Any, run_id: UUID) -> PipelineRun:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        assert run is not None
        # Detached, plain copy (same pattern as SM-504's own engine
        # integration tests): the session closes at the end of this ``async
        # with`` block, so returning the ORM instance itself would raise
        # DetachedInstanceError the moment a caller touches any attribute.
        return PipelineRun(
            id=run.id,
            pipeline_type=run.pipeline_type,
            dataset_id=run.dataset_id,
            source_id=run.source_id,
            status=run.status,
            idempotency_key=run.idempotency_key,
            payload_hash=run.payload_hash,
            input=run.input,
            progress=run.progress,
            current_step=run.current_step,
            attempt=run.attempt,
            worker_id=run.worker_id,
            heartbeat_at=run.heartbeat_at,
            config_fingerprint=run.config_fingerprint,
            error_code=run.error_code,
            error_message=run.error_message,
            metrics=run.metrics,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            next_attempt_at=run.next_attempt_at,
            retry_of_run_id=run.retry_of_run_id,
        )


async def read_steps(session_factory: Any, run_id: UUID) -> list[dict[str, Any]]:
    async with PostgresUnitOfWork(session_factory) as uow:
        steps = await uow.pipeline_steps.list_for_run(run_id)
        return [{"name": s.name, "status": s.status} for s in steps]


async def dataset_exists(session_factory: Any, dataset_id: UUID) -> bool:
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        return dataset is not None


async def wait_until(predicate: Callable[[], Any], *, timeout: float = TEST_TIMEOUT) -> None:
    async def _poll() -> None:
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)


def make_coordinator(
    session_factory: Any,
    registry: PipelineRegistry,
    *,
    max_concurrent_datasets: int = 1,
    stale_after_seconds: int = 1,
    shutdown_grace_seconds: float = 5.0,
    claimer: Any = None,
) -> PipelineWorkerCoordinator:
    return PipelineWorkerCoordinator(
        session_factory,
        registry,
        enabled=True,
        poll_interval_ms=POLL_INTERVAL_MS,
        stale_after_seconds=stale_after_seconds,
        max_concurrent_datasets=max_concurrent_datasets,
        claimer=claimer,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )


# === A. end-to-end queue -> engine ============================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_end_to_end_queue_to_engine(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry)
        await coordinator.start()
        try:
            await wait_until(lambda: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
        assert step_a.calls == 1
        assert coordinator.is_running is False
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


async def _run_is_terminal(session_factory: Any, run_id: UUID) -> bool:
    run = await read_run(session_factory, run_id)
    return run.status in (
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLED,
    )


# === B. same-boot worker id ====================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_same_boot_worker_id_new_id_per_new_coordinator(
    postgres_engine: AsyncEngine,
) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_1 = await insert_dataset(session_factory, ids)
    dataset_2 = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run1 = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_1)
        run2 = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_2)

        coordinator = make_coordinator(session_factory, registry, max_concurrent_datasets=2)
        await coordinator.start()
        try:
            await wait_until(lambda: _run_is_terminal(session_factory, run1))
            await wait_until(lambda: _run_is_terminal(session_factory, run2))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run1_row = await read_run(session_factory, run1)
        run2_row = await read_run(session_factory, run2)
        assert run1_row.worker_id == coordinator.worker_id
        assert run2_row.worker_id == coordinator.worker_id

        other_coordinator = make_coordinator(session_factory, registry)
        assert other_coordinator.worker_id != coordinator.worker_id
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === C. concurrency limit ======================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c_concurrency_limit_third_run_waits_for_a_free_slot(
    postgres_engine: AsyncEngine,
) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    datasets = [await insert_dataset(session_factory, ids) for _ in range(3)]
    try:
        # PipelineRegistry maps one PipelineDefinition per PipelineType, so all
        # three dataset-scoped runs below share one COGNIFY definition/step
        # instance -- its single ``proceed`` Event releases every in-flight
        # call at once, which is enough to prove the capacity ceiling itself.
        shared_step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", shared_step_a),)
                )
            ]
        )
        run_ids = [
            await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
            for dataset_id in datasets
        ]

        coordinator = make_coordinator(session_factory, registry, max_concurrent_datasets=2)
        await coordinator.start()
        try:
            await wait_until(lambda: len(coordinator._active_tasks) == 2)  # noqa: SLF001
            await asyncio.sleep(0.1)

            statuses = [(await read_run(session_factory, run_id)).status for run_id in run_ids]
            running_count = sum(1 for status in statuses if status == PipelineRunStatus.RUNNING)
            queued_count = sum(1 for status in statuses if status == PipelineRunStatus.QUEUED)
            assert running_count == 2
            assert queued_count == 1

            # Release one PausableStep call at a time is not directly possible
            # (all share the same Event); release the shared proceed Event and
            # let both currently-executing steps finish, then confirm the
            # third dataset's run is claimed and executes to completion.
            shared_step_a.proceed.set()

            for run_id in run_ids:
                await wait_until(lambda run_id=run_id: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        for run_id in run_ids:
            run = await read_run(session_factory, run_id)
            assert run.status == PipelineRunStatus.SUCCEEDED
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === D. heartbeat during a long step ===========================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_heartbeat_advances_while_step_is_in_flight(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, stale_after_seconds=1)
        await coordinator.start()
        try:
            await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

            async with postgres_engine.connect() as connection:
                initial_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
                status = await connection.scalar(
                    text("SELECT status FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
                assert status == "running"

            await asyncio.sleep(0.6)  # > heartbeat interval (stale_after/3 ~= 0.33s)

            async with postgres_engine.connect() as connection:
                later_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
            assert later_heartbeat > initial_heartbeat

            step_a.proceed.set()
            await wait_until(lambda: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === E. heartbeat continues through CANCELLING, then converges ================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_heartbeat_continues_while_cancelling_then_converges(
    postgres_engine: AsyncEngine,
) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, stale_after_seconds=1)
        await coordinator.start()
        try:
            await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

            async with postgres_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE pipeline_runs SET status = 'cancelling' WHERE id = :id"),
                    {"id": run_id},
                )
            async with postgres_engine.connect() as connection:
                initial_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )

            await asyncio.sleep(0.6)

            async with postgres_engine.connect() as connection:
                later_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
            assert later_heartbeat > initial_heartbeat

            step_a.proceed.set()
            await wait_until(lambda: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.CANCELLED
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === F. heartbeat fencing: superseded ownership stops heartbeating ============


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_heartbeat_stops_once_ownership_is_superseded(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, stale_after_seconds=1)
        await coordinator.start()
        try:
            await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

            # Simulate a reclaim by a different worker/attempt directly, the
            # way SM-507 stale recovery would eventually produce it -- not
            # implementing stale recovery itself, just constructing the same
            # post-condition (ADR-0009 SS 16 fencing token no longer matches).
            async with postgres_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE pipeline_runs "
                        "SET worker_id = 'wk-superseded', attempt = attempt + 1 "
                        "WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                stamped_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )

            await asyncio.sleep(0.6)

            async with postgres_engine.connect() as connection:
                later_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
            assert later_heartbeat == stamped_heartbeat  # this worker's heartbeat loop stopped

            step_a.proceed.set()
            await asyncio.sleep(0.1)  # let the (now-abandoned) execution unwind harmlessly
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === G. retry releases the capacity slot for another dataset ==================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_retry_releases_capacity_slot_for_another_dataset(
    postgres_engine: AsyncEngine,
) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_1 = await insert_dataset(session_factory, ids)
    dataset_2 = await insert_dataset(session_factory, ids)
    try:
        # A worker coordinator is bound to exactly one PipelineRegistry
        # instance for its whole lifetime (ADR-0009 SS O: the registry is
        # closed/code-level, not swapped at runtime) -- both runs below are
        # COGNIFY-typed against the SAME registry/step object, so ``flaky``'s
        # own call count is what proves run2 actually executed.
        flaky = RetryOnceStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", flaky),)
                )
            ]
        )
        run1 = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_1)

        coordinator = make_coordinator(session_factory, registry, max_concurrent_datasets=1)
        await coordinator.start()
        try:
            # run1 fails retryably and requeues -- its execution task ends,
            # freeing the single capacity slot immediately (no need to wait
            # for the retry's own backoff to become due). Wait on the fake
            # step's own call count first: a freshly-submitted run also
            # starts out QUEUED, so waiting on status alone would resolve
            # instantly, before the worker ever claims it.
            await wait_until(lambda: flaky.calls >= 1)
            await wait_until(
                lambda: _run_status_is(session_factory, run1, PipelineRunStatus.QUEUED)
            )
            await wait_until(lambda: not coordinator._active_tasks)  # noqa: SLF001
            assert flaky.calls == 1

            run2 = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_2)
            await wait_until(lambda: _run_is_terminal(session_factory, run2))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run2_row = await read_run(session_factory, run2)
        assert run2_row.status == PipelineRunStatus.SUCCEEDED
        assert flaky.calls == 2  # run1's failed attempt (1) + run2's successful attempt (2)
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


async def _run_status_is(session_factory: Any, run_id: UUID, status: PipelineRunStatus) -> bool:
    run = await read_run(session_factory, run_id)
    return run.status == status


# === H. cooperative shutdown between steps =====================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_h_cooperative_shutdown_between_steps(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        step_b = RecordingStep("b")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY,
                    steps=(step_definition("a", step_a), step_definition("b", step_b)),
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, shutdown_grace_seconds=5.0)
        await coordinator.start()
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        stop_task = asyncio.ensure_future(coordinator.stop())
        await asyncio.sleep(0.1)
        step_a.proceed.set()
        await asyncio.wait_for(stop_task, timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.RUNNING  # SS 17: paused, never re-mutated
        assert step_b.calls == 0

        steps = await read_steps(session_factory, run_id)
        by_name = {s["name"]: s for s in steps}
        assert by_name["a"]["status"] == PipelineStepStatus.SUCCEEDED
        assert by_name["b"]["status"] == PipelineStepStatus.QUEUED
        assert coordinator.is_running is False
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === I. shutdown during the (only, last) step still completes =================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_i_shutdown_during_last_step_still_completes(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, shutdown_grace_seconds=5.0)
        await coordinator.start()
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        stop_task = asyncio.ensure_future(coordinator.stop())
        await asyncio.sleep(0.1)
        step_a.proceed.set()
        await asyncio.wait_for(stop_task, timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === J. grace expiration forcibly cancels a hung task ==========================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_grace_expiration_cancels_hung_task_without_marking_terminal(
    postgres_engine: AsyncEngine,
) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = NeverFinishesStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, shutdown_grace_seconds=0.1)
        await coordinator.start()
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        assert not coordinator._active_tasks  # noqa: SLF001 - no orphaned asyncio.Task

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.RUNNING  # never falsely SUCCEEDED/FAILED/CANCELLED
        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.RUNNING
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === K. claim-in-progress shutdown race ========================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_claim_in_progress_shutdown_race_still_dispatches(
    postgres_engine: AsyncEngine,
) -> None:
    """SM-505 SS 26: a claim already in flight when ``stop()`` is called must
    still be dispatched as a tracked execution task -- never discarded, never
    left owning a row with no task watching it. Because the shutdown signal
    is already set by the time this newly-claimed run reaches its very first
    per-step checkpoint, the engine's own cooperative-shutdown checkpoint
    (ADR-0009 SS T amendment) correctly pauses it *before* starting step "a"
    -- this is the intended graceful outcome, not a failure of the race
    handling. The property under test is narrower and stronger: the task was
    tracked and cleanly awaited by ``stop()`` (no orphan, no hang, no
    unretrieved exception), and the row is left legitimately RUNNING/paused,
    never abandoned mid-claim."""

    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        delayed_claimer = DelayedClaimer(PipelineRunClaimer(session_factory))
        coordinator = make_coordinator(session_factory, registry, claimer=delayed_claimer)
        await coordinator.start()

        await asyncio.wait_for(delayed_claimer.entered.wait(), timeout=TEST_TIMEOUT)
        stop_task = asyncio.ensure_future(coordinator.stop())
        await asyncio.sleep(0.05)
        delayed_claimer.proceed.set()  # claim completes strictly after stop() was requested

        await asyncio.wait_for(stop_task, timeout=TEST_TIMEOUT)

        # Dispatched and tracked to completion by stop() itself -- no
        # lingering task, no hang, nothing left for the caller to await.
        assert not coordinator._active_tasks  # noqa: SLF001
        assert coordinator.is_running is False

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.RUNNING  # claimed, paused at its first checkpoint
        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.QUEUED  # never started
        assert step_a.calls == 0
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === L. unexpected task/engine exception isolation =============================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_l_unexpected_exception_is_isolated_other_runs_still_process(
    postgres_engine: AsyncEngine,
) -> None:
    """SM-505 SS 32 audit finding: a claimed run whose ``pipeline_type`` has
    no matching :class:`PipelineDefinition` in the worker's registry causes
    ``PipelineEngine.execute()`` to raise ``UnknownPipelineTypeError`` before
    touching the run at all. The worker must consume that exception, leave
    the row RUNNING for future stale recovery (SM-507), and keep processing
    other, healthy runs -- never crash the poll loop."""

    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    broken_dataset_id = await insert_dataset(session_factory, ids)
    healthy_dataset_id = await insert_dataset(session_factory, ids)
    try:
        healthy_step = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", healthy_step),)
                )
            ]
        )
        broken_run_id = await submit_run_with_unregistered_type(
            session_factory, ids, dataset_id=broken_dataset_id, step_names=["a"]
        )
        healthy_run_id = await submit_run(
            session_factory, ids, registry=registry, dataset_id=healthy_dataset_id
        )

        coordinator = make_coordinator(session_factory, registry, max_concurrent_datasets=2)
        await coordinator.start()
        try:
            await wait_until(lambda: _run_is_terminal(session_factory, healthy_run_id))
            await asyncio.sleep(0.1)  # let the broken run's task settle
            assert coordinator.is_running is True
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        healthy_run = await read_run(session_factory, healthy_run_id)
        assert healthy_run.status == PipelineRunStatus.SUCCEEDED

        broken_run = await read_run(session_factory, broken_run_id)
        assert broken_run.status == PipelineRunStatus.RUNNING  # left for SM-507 stale recovery
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === M. forced shutdown during an in-flight persist() transaction =============


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_forced_shutdown_during_persist_transaction_completes_atomically(
    postgres_engine: AsyncEngine,
) -> None:
    """SM-505 forced-shutdown audit: a grace expiring while ``persist()`` is
    mid-transaction must never split the business mutation from the
    ``PipelineStep`` SUCCEEDED transition. ``_run_transactional_phase``
    defers the forced ``task.cancel()`` until the transaction reaches its own
    conclusion, so the only possible outcome here is A (both committed
    together) -- never the step left RUNNING while the mutation is already
    visible, and never a "Task exception was never retrieved" warning."""

    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableTransactionalStep("a")
        ids.dataset_ids.append(step_a.marker_dataset_id)
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        coordinator = make_coordinator(session_factory, registry, shutdown_grace_seconds=0.1)
        await coordinator.start()

        await asyncio.wait_for(step_a.entered_persist.wait(), timeout=TEST_TIMEOUT)

        stop_task = asyncio.ensure_future(coordinator.stop())
        await asyncio.sleep(0.3)  # grace (0.1s) has already expired; task.cancel() was issued
        assert not stop_task.done()  # stop() is correctly still waiting on the open transaction
        assert not await dataset_exists(session_factory, step_a.marker_dataset_id)

        step_a.proceed.set()  # let persist() -- and its transaction -- reach its own conclusion
        await asyncio.wait_for(stop_task, timeout=TEST_TIMEOUT)

        # Atomic outcome A: both landed together, never split.
        assert await dataset_exists(session_factory, step_a.marker_dataset_id)
        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.SUCCEEDED
        assert not coordinator._active_tasks  # noqa: SLF001 - no orphaned task
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === N. forced shutdown during the step-start (QUEUED -> RUNNING) checkpoint ===


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_forced_shutdown_during_step_start_checkpoint_completes_atomically(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same mechanism as scenario M, for the *other* short transactional
    phase the audit named explicitly: the step-start checkpoint
    (``QUEUED -> RUNNING``, ADR-0009 SS B). The checkpoint has no test-facing
    pause hook of its own, so this test opens one by monkeypatching
    ``PipelineRunRepository.get_database_now`` -- called once, deep inside
    the checkpoint's already-open, row-locked transaction, to pause there.
    Reverted automatically by pytest's ``monkeypatch`` fixture; not a change
    to production code.

    The chosen pause point sits *before* the checkpoint's own cooperative
    ``stop_requested()`` read (SM-505 SS 18 ordering), so by the time this
    call resumes, the worker's already-set shutdown signal is correctly
    observed by the checkpoint itself, which takes its normal graceful-pause
    branch: nothing is mutated, the transaction rolls back cleanly (a
    same-shape no-op as an unclaimed candidate). This is still exactly the
    property under audit -- the forced ``task.cancel()`` never landed inside
    the open transaction and never produced a half-written state -- proven
    here by the step never leaving ``QUEUED`` and the run never leaving
    ``RUNNING``, with no orphaned task left behind either."""

    from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import (
        PipelineRunRepository,
    )

    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        entered_checkpoint = asyncio.Event()
        proceed = asyncio.Event()
        original_get_database_now = PipelineRunRepository.get_database_now

        async def paused_get_database_now(self: PipelineRunRepository) -> Any:
            entered_checkpoint.set()
            await asyncio.wait_for(proceed.wait(), timeout=TEST_TIMEOUT)
            return await original_get_database_now(self)

        monkeypatch.setattr(PipelineRunRepository, "get_database_now", paused_get_database_now)

        coordinator = make_coordinator(session_factory, registry, shutdown_grace_seconds=0.1)
        await coordinator.start()

        await asyncio.wait_for(entered_checkpoint.wait(), timeout=TEST_TIMEOUT)

        stop_task = asyncio.ensure_future(coordinator.stop())
        await asyncio.sleep(0.3)  # grace (0.1s) has already expired; task.cancel() was issued
        assert (
            not stop_task.done()
        )  # stop() correctly still waits on the open checkpoint transaction

        # Mid-transaction: the step must not yet show RUNNING to an
        # independent reader -- the checkpoint's own UPDATE has not
        # committed while we deliberately hold it open above.
        steps_mid_transaction = await read_steps(session_factory, run_id)
        assert steps_mid_transaction[0]["status"] == PipelineStepStatus.QUEUED

        proceed.set()  # let the checkpoint transaction reach its own conclusion
        await asyncio.wait_for(stop_task, timeout=TEST_TIMEOUT)

        # Atomic outcome: the checkpoint's own cooperative stop_requested()
        # check (observed only once the transaction resumes) correctly took
        # the graceful-pause branch -- nothing committed, never half-written.
        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.RUNNING
        steps_final = await read_steps(session_factory, run_id)
        assert steps_final[0]["status"] == PipelineStepStatus.QUEUED
        assert step_a.calls == 0
        assert not coordinator._active_tasks  # noqa: SLF001 - no orphaned task
        assert coordinator.is_running is False
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === O. transient heartbeat failure recovers on the next cadence ==============


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_transient_heartbeat_failure_recovers_next_cadence(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import (
        PipelineRunRepository,
    )

    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        original_heartbeat_if_owned = PipelineRunRepository.heartbeat_if_owned
        call_count = 0

        async def flaky_heartbeat_if_owned(
            self: PipelineRunRepository, *args: Any, **kwargs: Any
        ) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated transient heartbeat infrastructure failure")
            return await original_heartbeat_if_owned(self, *args, **kwargs)

        monkeypatch.setattr(PipelineRunRepository, "heartbeat_if_owned", flaky_heartbeat_if_owned)

        coordinator = make_coordinator(session_factory, registry, stale_after_seconds=1)
        await coordinator.start()
        try:
            await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

            async with postgres_engine.connect() as connection:
                initial_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )

            # Wait long enough for the first (failing) cadence plus at least
            # one more successful one (~0.33s each at stale_after_seconds=1).
            await asyncio.sleep(0.9)

            assert call_count >= 2  # the flaky first attempt, then at least one recovery
            async with postgres_engine.connect() as connection:
                later_heartbeat = await connection.scalar(
                    text("SELECT heartbeat_at FROM pipeline_runs WHERE id = :id"), {"id": run_id}
                )
            assert later_heartbeat > initial_heartbeat  # recovered, not permanently stopped

            step_a.proceed.set()
            await wait_until(lambda: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)


# === P. transient claim failure recovers on the next poll =====================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p_transient_claim_failure_recovers_next_poll(postgres_engine: AsyncEngine) -> None:
    ids = WorkerTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        registry = PipelineRegistry(
            [
                PipelineDefinition(
                    pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
                )
            ]
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        flaky_claimer = FlakyClaimer(PipelineRunClaimer(session_factory))
        coordinator = make_coordinator(session_factory, registry, claimer=flaky_claimer)
        await coordinator.start()
        try:
            await wait_until(lambda: _run_is_terminal(session_factory, run_id))
        finally:
            await asyncio.wait_for(coordinator.stop(), timeout=TEST_TIMEOUT)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
        assert step_a.calls == 1

        # Not busy-spun: the failed attempt and the next (successful) one are
        # separated by roughly a poll interval, not a tight retry loop.
        assert len(flaky_claimer.call_times) >= 2
        gap_seconds = flaky_claimer.call_times[1] - flaky_claimer.call_times[0]
        assert gap_seconds >= (POLL_INTERVAL_MS / 1000.0) * 0.5

        # Worker stayed alive/ready through the transient failure.
        assert coordinator.is_running is False  # stopped cleanly afterward, not crashed mid-run
    finally:
        await cleanup_worker_fixture(postgres_engine, ids)
