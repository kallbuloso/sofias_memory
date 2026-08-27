"""Real-PostgreSQL tests for the SM-509 shared submission/wait contract.

Proves ADR-0009 SS C (atomic durable submission), SS G/SS S (idempotency,
including genuine concurrent-transaction races against the real
``uq_pipeline_runs_idempotency_key`` partial unique index), SS R (waiter
polling against real concurrent writes, no long transaction), and SS U
(worker-availability gate) against a real, dedicated PostgreSQL database.
Requires migrations already applied through 0009.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import (
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
)
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.pipeline_lifecycle import transition_run
from sofias_memory.services.pipeline_submission import (
    PipelineSubmissionService,
    SubmissionTargets,
    worker_disabled_error,
)
from sofias_memory.services.pipeline_waiter import PipelineRunWaiter

SUBMISSION_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_SUBMISSION_POSTGRES_TESTS"
SUBMISSION_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_SUBMISSION_TEST_DATABASE_URL"
SUBMISSION_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_submission_test"

CONFIG_FINGERPRINT = "f" * 64


def submission_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(SUBMISSION_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {SUBMISSION_POSTGRES_TESTS_ENV}=1 to run submission PostgreSQL tests")

    database_url = env.get(SUBMISSION_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {SUBMISSION_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_submission_test_database_url(database_url)
    return database_url


def _validate_submission_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("submission PostgreSQL test database URL is invalid")

    if parsed_url.database != SUBMISSION_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "submission PostgreSQL tests require the exact dedicated database "
            f"{SUBMISSION_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = submission_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_submission_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_submission_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != SUBMISSION_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated submission test database")


def test_submission_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        submission_test_database_url({})


def test_submission_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        submission_test_database_url({SUBMISSION_POSTGRES_TESTS_ENV: "1"})


def test_submission_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        submission_test_database_url(
            {
                SUBMISSION_POSTGRES_TESTS_ENV: "1",
                SUBMISSION_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fixtures/helpers ---------------------------------------------------------


@dataclass
class SubmissionIds:
    dataset_id: UUID = field(default_factory=uuid4)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_dataset(engine: AsyncEngine, ids: SubmissionIds) -> None:
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=ids.dataset_id,
                name=f"submission-{ids.dataset_id}",
                slug=f"submission-{ids.dataset_id}",
            )
        )
        await uow.commit()


async def cleanup_submission_fixture(engine: AsyncEngine, ids: SubmissionIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"),
            {"ids": ids.run_ids},
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )


class NoOpStep:
    async def execute(self, context: Any) -> StepResult:
        raise AssertionError("submission must never execute a step")

    async def persist(self, context: Any, result: StepResult, uow: Any) -> None:
        raise AssertionError("submission must never persist a step")

    async def compensate(self, context: Any, result: StepResult) -> None:
        raise AssertionError("submission must never compensate a step")


def const_deriver(run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]) -> Any:
    del step_outputs
    return {"seed": run_input.get("seed")}


def make_test_registry() -> PipelineRegistry:
    steps = (
        PipelineStepDefinition(
            name="step_a", definition_id="step_a:v1", step=NoOpStep(), input_deriver=const_deriver
        ),
        PipelineStepDefinition(
            name="step_b", definition_id="step_b:v1", step=NoOpStep(), input_deriver=const_deriver
        ),
    )
    return PipelineRegistry([PipelineDefinition(pipeline_type=PipelineType.REMEMBER, steps=steps)])


@dataclass
class StaticWorker:
    enabled: bool = True
    is_operational: bool = True


async def dataset_row_count(engine: AsyncEngine, dataset_id: UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(
            text("SELECT count(*) FROM datasets WHERE id = :id"), {"id": dataset_id}
        )
        return int(result or 0)


async def run_count_for_key(engine: AsyncEngine, idempotency_key: str) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(
            text("SELECT count(*) FROM pipeline_runs WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        )
        return int(result or 0)


async def step_count_for_run(engine: AsyncEngine, run_id: UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(
            text("SELECT count(*) FROM pipeline_steps WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        return int(result or 0)


def service_for(
    postgres_engine: AsyncEngine,
    ids: SubmissionIds,
    *,
    worker: StaticWorker | None = None,
) -> PipelineSubmissionService:
    session_factory = create_session_factory(postgres_engine)
    return PipelineSubmissionService(
        session_factory=session_factory,
        registry=make_test_registry(),
        worker=worker or StaticWorker(),
        config_fingerprint=CONFIG_FINGERPRINT,
    )


def prepare_with_dataset(ids: SubmissionIds) -> Any:
    async def prepare(uow: Any) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=ids.dataset_id, source_id=None)

    return prepare


# --- A. atomic durable submission ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_atomic_durable_submission_commits_run_and_steps_together(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    try:
        outcome = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "atomic"},
            idempotency_key=None,
            prepare=prepare_with_dataset(ids),
        )
        ids.run_ids.append(outcome.run_id)

        assert outcome.created is True
        assert outcome.status == PipelineRunStatus.QUEUED
        assert await step_count_for_run(postgres_engine, outcome.run_id) == 2
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- B. idempotency (sequential) ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_sequential_same_key_same_hash_resolves_one_run(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    prepare = prepare_with_dataset(ids)
    try:
        first = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "seq"},
            idempotency_key="seq-key",
            prepare=prepare,
        )
        second = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "seq"},
            idempotency_key="seq-key",
            prepare=prepare,
        )
        ids.run_ids.append(first.run_id)

        assert first.run_id == second.run_id
        assert await run_count_for_key(postgres_engine, "seq-key") == 1
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_sequential_same_key_different_hash_returns_409(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    prepare = prepare_with_dataset(ids)
    try:
        original = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "A"},
            idempotency_key="conflict-key",
            prepare=prepare,
        )
        ids.run_ids.append(original.run_id)

        with pytest.raises(SofiasMemoryError) as error:
            await service.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "B"},
                idempotency_key="conflict-key",
                prepare=prepare,
            )

        assert error.value.status_code == 409
        assert await run_count_for_key(postgres_engine, "conflict-key") == 1
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_no_key_same_payload_creates_two_runs(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    prepare = prepare_with_dataset(ids)
    try:
        first = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "dup"},
            idempotency_key=None,
            prepare=prepare,
        )
        second = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "dup"},
            idempotency_key=None,
            prepare=prepare,
        )
        ids.run_ids.extend([first.run_id, second.run_id])

        assert first.run_id != second.run_id
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- C/D. concurrent same key ---------------------------------------------


async def _run_concurrent_same_key(
    postgres_engine: AsyncEngine,
    ids: SubmissionIds,
    *,
    work_input_a: dict[str, Any],
    work_input_b: dict[str, Any],
    idempotency_key: str,
) -> tuple[Any, Any | SofiasMemoryError]:
    service_a = service_for(postgres_engine, ids)
    service_b = service_for(postgres_engine, ids)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare_a(uow: Any) -> SubmissionTargets:
        del uow
        entered.set()
        await release.wait()
        return SubmissionTargets(dataset_id=ids.dataset_id, source_id=None)

    async def prepare_b(uow: Any) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=ids.dataset_id, source_id=None)

    task_a = asyncio.create_task(
        service_a.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input=work_input_a,
            idempotency_key=idempotency_key,
            prepare=prepare_a,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    # B's full submission (its own lookup, insert, commit) completes while
    # A is deliberately paused inside its own prepare() -- A already
    # performed its own pre-insert lookup and found nothing, exactly
    # mirroring a genuine race between two independent transactions.
    outcome_b = await service_b.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input_b,
        idempotency_key=idempotency_key,
        prepare=prepare_b,
    )
    release.set()

    try:
        outcome_a = await asyncio.wait_for(task_a, timeout=5)
        return outcome_a, outcome_b
    except SofiasMemoryError as error:
        return error, outcome_b


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_same_hash_resolves_to_exactly_one_run(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    try:
        work_input = {"seed": "concurrent-same"}
        result_a, outcome_b = await _run_concurrent_same_key(
            postgres_engine,
            ids,
            work_input_a=work_input,
            work_input_b=work_input,
            idempotency_key="concurrent-key-same",
        )
        assert not isinstance(result_a, SofiasMemoryError)
        ids.run_ids.append(outcome_b.run_id)

        assert result_a.run_id == outcome_b.run_id
        assert await run_count_for_key(postgres_engine, "concurrent-key-same") == 1
        assert await step_count_for_run(postgres_engine, outcome_b.run_id) == 2
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_different_hash_exactly_one_wins(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    try:
        result_a, outcome_b = await _run_concurrent_same_key(
            postgres_engine,
            ids,
            work_input_a={"seed": "loser"},
            work_input_b={"seed": "winner"},
            idempotency_key="concurrent-key-diff",
        )
        ids.run_ids.append(outcome_b.run_id)

        assert isinstance(result_a, SofiasMemoryError)
        assert result_a.status_code == 409
        assert await run_count_for_key(postgres_engine, "concurrent-key-diff") == 1
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- E. unrelated IntegrityError is never masked as a conflict ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unrelated_integrity_error_with_key_reraises_not_masked(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    bogus_dataset_id = uuid4()

    async def prepare_with_bogus_dataset(uow: Any) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=bogus_dataset_id, source_id=None)

    try:
        with pytest.raises(IntegrityError):
            await service.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "fk-violation"},
                idempotency_key="unrelated-key",
                prepare=prepare_with_bogus_dataset,
            )

        assert await run_count_for_key(postgres_engine, "unrelated-key") == 0
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unrelated_integrity_error_without_key_always_reraises(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    bogus_dataset_id = uuid4()

    async def prepare_with_bogus_dataset(uow: Any) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=bogus_dataset_id, source_id=None)

    try:
        with pytest.raises(IntegrityError):
            await service.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "fk-violation-2"},
                idempotency_key=None,
                prepare=prepare_with_bogus_dataset,
            )
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- E2. concurrent: an unrelated failure never confuses an unrelated sibling
# submission's success (SM-509 audit Finding 2) ------------------------------
#
# Note on why this is the closest achievable "concurrent + coincidental
# winner + wrong constraint" scenario: if B's insert used the SAME
# Idempotency-Key as an already-committed A, PostgreSQL itself would always
# surface the UNIQUE index violation (checked synchronously during index
# insertion) before any FK violation could ever be observed (FK checks run
# as an AFTER ROW trigger, which never fires once the unique index insert
# has already aborted the statement) -- so "B sees a non-idempotency
# constraint violation while a winner already exists FOR B'S OWN KEY" is not
# a reachable combination against a real unique index, by construction. The
# exact-constraint-matching branch for that specific combination is
# therefore proven with a controlled fake DBAPI error at the unit level
# (``test_integrity_error_unrelated_constraint_reraises_despite_coincidence``);
# this test instead proves the surrounding real-world concurrent behavior:
# two independent, concurrently-committing submissions under DIFFERENT keys
# never cross-contaminate, even when one of them fails.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_unrelated_failure_does_not_affect_sibling_submission(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service_a = service_for(postgres_engine, ids)
    service_b = service_for(postgres_engine, ids)
    bogus_dataset_id = uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare_a(uow: Any) -> SubmissionTargets:
        del uow
        entered.set()
        await release.wait()
        return SubmissionTargets(dataset_id=ids.dataset_id, source_id=None)

    async def prepare_b_bogus(uow: Any) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=bogus_dataset_id, source_id=None)

    try:
        task_a = asyncio.create_task(
            service_a.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "sibling-a"},
                idempotency_key="sibling-key-a",
                prepare=prepare_a,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # B, concurrently, submits under a completely different key and
        # deliberately hits a real FK violation -- nothing to do with A's
        # key or A's in-flight commit.
        with pytest.raises(IntegrityError):
            await service_b.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "sibling-b"},
                idempotency_key="sibling-key-b",
                prepare=prepare_b_bogus,
            )

        release.set()
        outcome_a = await asyncio.wait_for(task_a, timeout=5)
        ids.run_ids.append(outcome_a.run_id)

        assert outcome_a.created is True
        assert await run_count_for_key(postgres_engine, "sibling-key-a") == 1
        assert await run_count_for_key(postgres_engine, "sibling-key-b") == 0
    finally:
        release.set()
        await cleanup_submission_fixture(postgres_engine, ids)


# --- cross-pipeline idempotency (SM-509 audit Finding 1) ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_same_hash_different_pipeline_type_returns_409(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    prepare = prepare_with_dataset(ids)
    work_input = {"seed": "cross-pipeline"}
    try:
        original = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input=work_input,
            idempotency_key="cross-pipeline-key",
            prepare=prepare,
        )
        ids.run_ids.append(original.run_id)

        # NOTE: FORGET is not registered by make_test_registry(), so if the
        # request ever reached the new-submission path it would fail with
        # UnknownPipelineTypeError, not proceed to an INSERT -- reaching a
        # 409 here instead is itself proof the existing-run branch (which
        # never touches the registry, Finding 5) is what rejected it, not a
        # coincidental registry validation failure.
        with pytest.raises(SofiasMemoryError) as error:
            await service.submit(
                pipeline_type=PipelineType.FORGET,
                work_input=work_input,
                idempotency_key="cross-pipeline-key",
                prepare=prepare,
            )

        assert error.value.status_code == 409
        assert error.value.code.value == "IDEMPOTENCY_CONFLICT"
        assert await run_count_for_key(postgres_engine, "cross-pipeline-key") == 1
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- existing run resolution needs neither the worker nor the registry
# (SM-509 audit Finding 3/5) ---------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_queued_run_resolves_with_worker_disabled_and_empty_registry(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    available_service = service_for(postgres_engine, ids)
    try:
        original = await available_service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "still-queued"},
            idempotency_key="still-queued-key",
            prepare=prepare_with_dataset(ids),
        )
        ids.run_ids.append(original.run_id)
        assert original.status == PipelineRunStatus.QUEUED

        # A second service instance with the worker disabled AND an empty
        # registry (no PipelineDefinition at all) must still resolve the
        # exact same run -- neither dependency is needed to replay an
        # already-existing match.
        unavailable_service = PipelineSubmissionService(
            session_factory=create_session_factory(postgres_engine),
            registry=PipelineRegistry([]),
            worker=StaticWorker(enabled=False, is_operational=False),
            config_fingerprint=CONFIG_FINGERPRINT,
        )

        async def unreachable_prepare(uow: Any) -> SubmissionTargets:
            del uow
            raise AssertionError("prepare() must never run for an existing-run resolution")

        outcome = await unavailable_service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "still-queued"},
            idempotency_key="still-queued-key",
            prepare=unreachable_prepare,
        )

        assert outcome.created is False
        assert outcome.run_id == original.run_id
        assert outcome.status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- F. transactional preparation rolls back on failure -----------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preparation_failure_rolls_back_preparation_and_creates_no_run(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    marker_dataset_id = uuid4()

    async def failing_prepare(uow: Any) -> SubmissionTargets:
        await uow.datasets.add(
            Dataset(
                id=marker_dataset_id,
                name=f"marker-{marker_dataset_id}",
                slug=f"marker-{marker_dataset_id}",
            )
        )
        raise RuntimeError("Simulated preparation failure after staging a mutation.")

    try:
        with pytest.raises(RuntimeError):
            await service.submit(
                pipeline_type=PipelineType.REMEMBER,
                work_input={"seed": "rollback"},
                idempotency_key="rollback-key",
                prepare=failing_prepare,
            )

        assert await dataset_row_count(postgres_engine, marker_dataset_id) == 0
        assert await run_count_for_key(postgres_engine, "rollback-key") == 0
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- G/H. waiter against real PostgreSQL, no long transaction ----------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_waiter_observes_a_concurrent_write_without_holding_a_transaction(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        outcome = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "waiter"},
            idempotency_key=None,
            prepare=prepare_with_dataset(ids),
        )
        ids.run_ids.append(outcome.run_id)
        assert outcome.status == PipelineRunStatus.QUEUED

        waiter = PipelineRunWaiter(session_factory=session_factory, poll_interval_seconds=0.02)
        wait_task = asyncio.create_task(
            waiter.wait_for_terminal(outcome.run_id, timeout_seconds=5.0)
        )

        # A concurrent writer, on its OWN session, must be free to claim and
        # finish this run while the waiter sleeps -- if the waiter held a
        # transaction/lock across its sleep, this would hang until the
        # waiter's own timeout instead of succeeding quickly.
        await asyncio.sleep(0.05)
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(outcome.run_id)
            assert run is not None
            now = utc_now()
            transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="test-worker")
            transition_run(run, PipelineRunStatus.SUCCEEDED, now=now)
            await uow.commit()

        result = await asyncio.wait_for(wait_task, timeout=5.0)

        assert result.terminal is True
        assert result.status == PipelineRunStatus.SUCCEEDED
        assert result.timed_out is False
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- I. timeout leaves the row untouched -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_waiter_timeout_leaves_the_queued_row_untouched(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        outcome = await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "timeout"},
            idempotency_key=None,
            prepare=prepare_with_dataset(ids),
        )
        ids.run_ids.append(outcome.run_id)

        waiter = PipelineRunWaiter(session_factory=session_factory, poll_interval_seconds=0.02)
        result = await waiter.wait_for_terminal(outcome.run_id, timeout_seconds=0.1)

        assert result.timed_out is True
        assert result.status == PipelineRunStatus.QUEUED

        async with PostgresUnitOfWork(session_factory) as uow:
            reloaded = await uow.pipeline_runs.get_by_id(outcome.run_id)
            assert reloaded is not None
            assert reloaded.status == PipelineRunStatus.QUEUED
            assert reloaded.attempt == 0
    finally:
        await cleanup_submission_fixture(postgres_engine, ids)


# --- J. worker unavailable creates no row -------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_unavailable_creates_no_pipeline_run_row(
    postgres_engine: AsyncEngine,
) -> None:
    ids = SubmissionIds()
    await insert_dataset(postgres_engine, ids)
    service = service_for(
        postgres_engine, ids, worker=StaticWorker(enabled=False, is_operational=False)
    )

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "no-worker"},
            idempotency_key="no-worker-key",
            prepare=prepare_with_dataset(ids),
        )

    assert error.value.status_code == 503
    assert await run_count_for_key(postgres_engine, "no-worker-key") == 0
    await cleanup_submission_fixture(postgres_engine, ids)


def test_worker_disabled_error_helper_is_stable() -> None:
    error = worker_disabled_error()
    assert error.status_code == 503
    assert error.code.value == "WORKER_DISABLED"
