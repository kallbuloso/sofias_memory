"""Real-PostgreSQL tests for ADR-0009's PipelineRun/PipelineStep persistence
layer (SM-502): migration 0008 constraints, atomic run+step materialization,
and concurrent-transition safety.

Does not test queue claiming, advisory locks, or worker polling -- those are
SM-503's opt-in suite. Requires a dedicated, discardable PostgreSQL database
with migrations already applied through 0008.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import (
    PipelineRunStatus,
    PipelineRunTransitionError,
    PipelineStepStatus,
    PipelineType,
)
from sofias_memory.infrastructure.postgres import (
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.pipeline_lifecycle import (
    StepPlan,
    create_run_with_steps,
    transition_run,
)

PIPELINE_LIFECYCLE_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_PIPELINE_LIFECYCLE_POSTGRES_TESTS"
PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_URL_ENV = (
    "SOFIAS_MEMORY_PIPELINE_LIFECYCLE_TEST_DATABASE_URL"
)
PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_pipeline_lifecycle_test"


def pipeline_lifecycle_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(PIPELINE_LIFECYCLE_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {PIPELINE_LIFECYCLE_POSTGRES_TESTS_ENV}=1 to run pipeline lifecycle "
            "PostgreSQL tests"
        )

    database_url = env.get(PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated "
            "discardable PostgreSQL database"
        )

    _validate_pipeline_lifecycle_test_database_url(database_url)
    return database_url


def _validate_pipeline_lifecycle_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("pipeline lifecycle PostgreSQL test database URL is invalid")

    if parsed_url.database != PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "pipeline lifecycle PostgreSQL tests require the exact dedicated database "
            f"{PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = pipeline_lifecycle_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_pipeline_lifecycle_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_pipeline_lifecycle_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated pipeline lifecycle test database"
        )


def test_pipeline_lifecycle_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        pipeline_lifecycle_test_database_url({})


def test_pipeline_lifecycle_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        pipeline_lifecycle_test_database_url({PIPELINE_LIFECYCLE_POSTGRES_TESTS_ENV: "1"})


def test_pipeline_lifecycle_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        pipeline_lifecycle_test_database_url(
            {
                PIPELINE_LIFECYCLE_POSTGRES_TESTS_ENV: "1",
                PIPELINE_LIFECYCLE_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


@dataclass
class LifecycleIds:
    dataset_id: UUID = field(default_factory=uuid4)
    other_dataset_id: UUID = field(default_factory=uuid4)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_dataset(engine: AsyncEngine, ids: LifecycleIds) -> None:
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=ids.dataset_id,
                name=f"lifecycle-{ids.dataset_id}",
                slug=f"lifecycle-{ids.dataset_id}",
            )
        )
        await uow.datasets.add(
            Dataset(
                id=ids.other_dataset_id,
                name=f"lifecycle-other-{ids.other_dataset_id}",
                slug=f"lifecycle-other-{ids.other_dataset_id}",
            )
        )
        await uow.commit()


async def cleanup_lifecycle_fixture(engine: AsyncEngine, ids: LifecycleIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE dataset_id = ANY(:ids)"),
            {"ids": [ids.dataset_id, ids.other_dataset_id]},
        )
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"),
            {"ids": ids.run_ids},
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = ANY(:ids)"),
            {"ids": [ids.dataset_id, ids.other_dataset_id]},
        )


def build_run(
    *,
    status: PipelineRunStatus,
    dataset_id: UUID | None,
    run_id: UUID | None = None,
    attempt: int = 0,
    payload_seed: str = "a",
) -> PipelineRun:
    return PipelineRun(
        id=run_id or uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        status=status,
        idempotency_key=None,
        payload_hash=(payload_seed * 64)[:64],
        input={},
        progress=0.0,
        current_step=None,
        attempt=attempt,
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


# --- 3. self-FK retry_of_run_id --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_of_run_id_self_fk_set_null_on_delete(postgres_engine: AsyncEngine) -> None:
    ids = LifecycleIds()
    await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        original = build_run(status=PipelineRunStatus.FAILED, dataset_id=None)
        retry = build_run(status=PipelineRunStatus.QUEUED, dataset_id=None)
        retry.retry_of_run_id = original.id
        ids.run_ids.extend([original.id, retry.id])

        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(original)
            await uow.pipeline_runs.add(retry)
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            reloaded = await uow.pipeline_runs.get_by_id(retry.id)
            assert reloaded is not None
            assert reloaded.retry_of_run_id == original.id

        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE id = :id"), {"id": original.id}
            )

        async with PostgresUnitOfWork(session_factory) as uow:
            reloaded_after_delete = await uow.pipeline_runs.get_by_id(retry.id)
            assert reloaded_after_delete is not None
            assert reloaded_after_delete.retry_of_run_id is None
    finally:
        await cleanup_lifecycle_fixture(postgres_engine, ids)


# --- 4. partial unique operational-run invariant ----------------------------
#
# ADR-0009 SS D freezes UNIQUE(dataset_id) WHERE dataset_id IS NOT NULL AND
# status IN ('running', 'cancelling') as a required defense-in-depth backstop.
# Its physical activation is deliberately DEFERRED past this migration/story:
# B4's still-synchronous Forget/Remember/Cognify/Improve create PipelineRun
# rows directly as RUNNING and rely on an application-level conflict check
# that runs *after* that insert (see test_forget_postgres_integration.py's
# reentrant/conflict scenarios); activating the constraint now would reject
# that insert before the app's own check ever runs. Tests for this constraint
# belong to the follow-up migration that activates it once the last
# direct-RUNNING B4 writer is migrated to the B5 runtime (tracked in
# docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B5.md, SM-502/SM-513,
# verified at GATE-B5) -- not here, while the index does not exist yet.


# --- 5. concurrency of state transitions on the same row -------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_terminal_transitions_do_not_corrupt_row(
    postgres_engine: AsyncEngine,
) -> None:
    """Two concurrent workers both try to finalize the same RUNNING row.

    The winner's row lock (get_by_id_for_update) serializes the two
    transactions; the loser observes the already-terminal status and the
    lifecycle guard rejects its own transition -- the row is never
    overwritten by the loser.
    """

    ids = LifecycleIds()
    run = build_run(status=PipelineRunStatus.RUNNING, dataset_id=None, payload_seed="c")
    ids.run_ids.append(run.id)
    try:
        session_factory = create_session_factory(postgres_engine)
        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(run)
            await uow.commit()

        winner_started = asyncio.Event()

        async def finalize_succeeded() -> None:
            async with PostgresUnitOfWork(session_factory) as uow:
                locked = await uow.pipeline_runs.get_by_id_for_update(run.id)
                assert locked is not None
                winner_started.set()
                await asyncio.sleep(0.2)  # hold the row lock so the loser blocks on it
                transition_run(locked, PipelineRunStatus.SUCCEEDED, now=datetime.now(UTC))
                await uow.commit()

        async def finalize_failed() -> PipelineRunTransitionError | None:
            await winner_started.wait()
            async with PostgresUnitOfWork(session_factory) as uow:
                locked = await uow.pipeline_runs.get_by_id_for_update(run.id)
                assert locked is not None
                try:
                    transition_run(
                        locked,
                        PipelineRunStatus.FAILED,
                        now=datetime.now(UTC),
                        error_code="LOSER",
                        error_message="lost the race",
                    )
                except PipelineRunTransitionError as exc:
                    await uow.rollback()
                    return exc
                await uow.commit()
                return None

        _, loser_error = await asyncio.gather(finalize_succeeded(), finalize_failed())

        assert loser_error is not None
        assert loser_error.current == PipelineRunStatus.SUCCEEDED
        assert loser_error.target == PipelineRunStatus.FAILED

        async with PostgresUnitOfWork(session_factory) as uow:
            final = await uow.pipeline_runs.get_by_id(run.id)
            assert final is not None
            assert final.status == PipelineRunStatus.SUCCEEDED  # winner's terminal state stands
            assert final.error_code is None  # loser never wrote its error fields
    finally:
        await cleanup_lifecycle_fixture(postgres_engine, ids)


# --- 6/7. atomic run+step materialization and rollback ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_and_steps_persisted_atomically(postgres_engine: AsyncEngine) -> None:
    ids = LifecycleIds()
    await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await create_run_with_steps(
                uow,
                pipeline_type=PipelineType.COGNIFY,
                dataset_id=ids.dataset_id,
                source_id=None,
                idempotency_key=None,
                payload_hash="d" * 64,
                input={},
                config_fingerprint="e" * 64,
                steps=[
                    StepPlan(name="chunk_document", ordinal=0),
                    StepPlan(name="embed_chunks", ordinal=1),
                ],
            )
            ids.run_ids.append(run.id)
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            reloaded_run = await uow.pipeline_runs.get_by_id(run.id)
            reloaded_steps = await uow.pipeline_steps.list_for_run(run.id)

            assert reloaded_run is not None
            assert reloaded_run.status == PipelineRunStatus.QUEUED
            assert len(reloaded_steps) == 2
            assert all(step.status == PipelineStepStatus.QUEUED for step in reloaded_steps)
            assert [step.ordinal for step in reloaded_steps] == [0, 1]
            assert [step.name for step in reloaded_steps] == ["chunk_document", "embed_chunks"]
    finally:
        await cleanup_lifecycle_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rollback_leaves_no_orphan_run_or_steps(postgres_engine: AsyncEngine) -> None:
    ids = LifecycleIds()
    await insert_dataset(postgres_engine, ids)
    run_id: UUID | None = None
    try:
        session_factory = create_session_factory(postgres_engine)
        with pytest.raises(RuntimeError):
            async with PostgresUnitOfWork(session_factory) as uow:
                run = await create_run_with_steps(
                    uow,
                    pipeline_type=PipelineType.FORGET,
                    dataset_id=ids.dataset_id,
                    source_id=None,
                    idempotency_key=None,
                    payload_hash="f" * 64,
                    input={},
                    config_fingerprint="1" * 64,
                    steps=[StepPlan(name="apply_forget", ordinal=0)],
                )
                run_id = run.id
                ids.run_ids.append(run.id)
                raise RuntimeError("simulated failure before commit")

        assert run_id is not None
        async with postgres_engine.connect() as connection:
            run_count = await connection.scalar(
                text("SELECT count(*) FROM pipeline_runs WHERE id = :id"),
                {"id": run_id},
            )
            step_count = await connection.scalar(
                text("SELECT count(*) FROM pipeline_steps WHERE run_id = :id"),
                {"id": run_id},
            )
        assert run_count == 0
        assert step_count == 0
    finally:
        await cleanup_lifecycle_fixture(postgres_engine, ids)


# --- 8. pipeline_steps unique (run_id, ordinal) -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_steps_unique_run_id_ordinal(postgres_engine: AsyncEngine) -> None:
    ids = LifecycleIds()
    await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await create_run_with_steps(
                uow,
                pipeline_type=PipelineType.IMPROVE,
                dataset_id=ids.dataset_id,
                source_id=None,
                idempotency_key=None,
                payload_hash="2" * 64,
                input={},
                config_fingerprint="3" * 64,
                steps=[StepPlan(name="feedback_weights", ordinal=0)],
            )
            ids.run_ids.append(run.id)
            await uow.commit()

        duplicate_step = PipelineStep(
            id=uuid4(),
            run_id=run.id,
            name="feedback_weights_again",
            ordinal=0,
            status=PipelineStepStatus.QUEUED,
            attempt=0,
            input_hash=None,
            output={},
            metrics={},
            error=None,
            started_at=None,
            finished_at=None,
        )
        with pytest.raises(IntegrityError):
            async with PostgresUnitOfWork(session_factory) as uow:
                await uow.pipeline_steps.add(duplicate_step)
                await uow.commit()
    finally:
        await cleanup_lifecycle_fixture(postgres_engine, ids)
