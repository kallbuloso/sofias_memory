"""Real-PostgreSQL tests for the SM-508 Runs read API.

Proves the Runs read path (RunService / PipelineRunRepository.list_page /
PipelineRunRepository.count_page / PipelineStepRepository.list_for_run)
observes persisted PostgreSQL state -- not a mock, not the worker, not
Neo4j. Requires a dedicated, discardable PostgreSQL database with migrations
already applied through 0009.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from sofias_memory.services.runs import RunService

RUNS_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_RUNS_POSTGRES_TESTS"
RUNS_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_RUNS_TEST_DATABASE_URL"
RUNS_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_runs_test"


def runs_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(RUNS_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {RUNS_POSTGRES_TESTS_ENV}=1 to run Runs API PostgreSQL tests")

    database_url = env.get(RUNS_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {RUNS_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_runs_test_database_url(database_url)
    return database_url


def _validate_runs_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("Runs API PostgreSQL test database URL is invalid")

    if parsed_url.database != RUNS_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            f"Runs API PostgreSQL tests require the exact dedicated database "
            f"{RUNS_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = runs_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_runs_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_runs_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != RUNS_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated Runs API test database")


def test_runs_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        runs_test_database_url({})


def test_runs_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        runs_test_database_url({RUNS_POSTGRES_TESTS_ENV: "1"})


def test_runs_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        runs_test_database_url(
            {
                RUNS_POSTGRES_TESTS_ENV: "1",
                RUNS_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


@dataclass
class RunsFixtureIds:
    dataset_id: UUID = field(default_factory=uuid4)
    other_dataset_id: UUID = field(default_factory=uuid4)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_datasets(engine: AsyncEngine, ids: RunsFixtureIds) -> None:
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=ids.dataset_id,
                name=f"runs-{ids.dataset_id}",
                slug=f"runs-{ids.dataset_id}",
            )
        )
        await uow.datasets.add(
            Dataset(
                id=ids.other_dataset_id,
                name=f"runs-other-{ids.other_dataset_id}",
                slug=f"runs-other-{ids.other_dataset_id}",
            )
        )
        await uow.commit()


async def cleanup_runs_fixture(engine: AsyncEngine, ids: RunsFixtureIds) -> None:
    async with engine.begin() as connection:
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
    run_id: UUID | None = None,
    dataset_id: UUID | None,
    pipeline_type: PipelineType = PipelineType.REMEMBER,
    status: PipelineRunStatus = PipelineRunStatus.QUEUED,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    metrics: dict[str, object] | None = None,
    attempt: int = 0,
) -> PipelineRun:
    run = PipelineRun(
        id=run_id or uuid4(),
        pipeline_type=pipeline_type,
        dataset_id=dataset_id,
        source_id=None,
        status=status,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={},
        progress=0.0,
        current_step=None,
        attempt=attempt,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint="b" * 64,
        error_code=error_code,
        error_message=error_message,
        metrics=metrics or {},
        started_at=started_at,
        finished_at=finished_at,
        next_attempt_at=None,
        retry_of_run_id=None,
    )
    if created_at is not None:
        run.created_at = created_at
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_service_lists_filters_paginates_and_orders_persisted_runs(
    postgres_engine: AsyncEngine,
) -> None:
    ids = RunsFixtureIds()
    await insert_datasets(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        older = build_run(
            dataset_id=ids.dataset_id,
            pipeline_type=PipelineType.REMEMBER,
            status=PipelineRunStatus.SUCCEEDED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        newer = build_run(
            dataset_id=ids.dataset_id,
            pipeline_type=PipelineType.COGNIFY,
            status=PipelineRunStatus.FAILED,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            error_code="STEP_INPUT_DRIFT",
            error_message="drift detected",
        )
        other_dataset_run = build_run(
            dataset_id=ids.other_dataset_id,
            pipeline_type=PipelineType.REMEMBER,
            status=PipelineRunStatus.RUNNING,
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        no_dataset_run = build_run(
            dataset_id=None,
            pipeline_type=PipelineType.FORGET,
            status=PipelineRunStatus.CANCELLED,
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
        )
        ids.run_ids.extend([older.id, newer.id, other_dataset_run.id, no_dataset_run.id])

        async with PostgresUnitOfWork(session_factory) as uow:
            for run in (older, newer, other_dataset_run, no_dataset_run):
                await uow.pipeline_runs.add(run)
            await uow.commit()

        service = RunService(session_factory=session_factory)

        all_runs = await service.list_runs(limit=50, offset=0)
        assert [item.run_id for item in all_runs.items] == [
            no_dataset_run.id,
            other_dataset_run.id,
            newer.id,
            older.id,
        ]
        assert all_runs.total == 4

        first_page = await service.list_runs(limit=2, offset=0)
        second_page = await service.list_runs(limit=2, offset=2)
        assert [item.run_id for item in first_page.items] == [
            no_dataset_run.id,
            other_dataset_run.id,
        ]
        assert [item.run_id for item in second_page.items] == [newer.id, older.id]
        assert first_page.total == 4
        assert second_page.total == 4

        dataset_scoped = await service.list_runs(limit=50, offset=0, dataset_id=ids.dataset_id)
        assert {item.run_id for item in dataset_scoped.items} == {older.id, newer.id}
        assert dataset_scoped.total == 2

        by_status = await service.list_runs(limit=50, offset=0, statuses=[PipelineRunStatus.FAILED])
        assert [item.run_id for item in by_status.items] == [newer.id]
        failed_item = by_status.items[0]
        assert failed_item.error_code == "STEP_INPUT_DRIFT"
        assert failed_item.error_message == "drift detected"

        by_type = await service.list_runs(limit=50, offset=0, pipeline_type=PipelineType.FORGET)
        assert [item.run_id for item in by_type.items] == [no_dataset_run.id]

        combined = await service.list_runs(
            limit=50,
            offset=0,
            dataset_id=ids.dataset_id,
            pipeline_type=PipelineType.COGNIFY,
            statuses=[PipelineRunStatus.FAILED],
        )
        assert [item.run_id for item in combined.items] == [newer.id]
    finally:
        await cleanup_runs_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_service_get_run_returns_persisted_steps_in_ordinal_order(
    postgres_engine: AsyncEngine,
) -> None:
    ids = RunsFixtureIds()
    await insert_datasets(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        run = build_run(
            dataset_id=ids.dataset_id,
            status=PipelineRunStatus.RUNNING,
            metrics={"chunks": 3},
        )
        ids.run_ids.append(run.id)

        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(run)
            await uow.pipeline_steps.add_many(
                [
                    PipelineStep(
                        id=uuid4(),
                        run_id=run.id,
                        name="embed",
                        ordinal=1,
                        status=PipelineStepStatus.QUEUED,
                        attempt=0,
                        input_hash=None,
                        output={},
                        metrics={},
                        error=None,
                        started_at=None,
                        finished_at=None,
                    ),
                    PipelineStep(
                        id=uuid4(),
                        run_id=run.id,
                        name="chunk",
                        ordinal=0,
                        status=PipelineStepStatus.SUCCEEDED,
                        attempt=1,
                        input_hash="c" * 64,
                        output={"raw": "never-public"},
                        metrics={"tokens": 42},
                        error=None,
                        started_at=datetime(2026, 1, 1, tzinfo=UTC),
                        finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                    ),
                ]
            )
            await uow.commit()

        service = RunService(session_factory=session_factory)
        detail = await service.get_run(run.id)

        assert [step.name for step in detail.steps] == ["chunk", "embed"]
        assert detail.metrics == {"chunks": 3}
        chunk_step = detail.steps[0]
        assert chunk_step.status == PipelineStepStatus.SUCCEEDED
        assert chunk_step.metrics == {"tokens": 42}
        dumped_step = chunk_step.model_dump()
        assert "output" not in dumped_step
        assert "input_hash" not in dumped_step
    finally:
        await cleanup_runs_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_service_get_run_legacy_b4_run_has_zero_persisted_steps(
    postgres_engine: AsyncEngine,
) -> None:
    ids = RunsFixtureIds()
    await insert_datasets(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        legacy_run = build_run(
            dataset_id=ids.dataset_id,
            status=PipelineRunStatus.RUNNING,
            attempt=1,
        )
        ids.run_ids.append(legacy_run.id)

        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(legacy_run)
            await uow.commit()

        service = RunService(session_factory=session_factory)
        detail = await service.get_run(legacy_run.id)

        assert detail.steps == []
    finally:
        await cleanup_runs_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_service_get_run_discards_extra_keys_persisted_in_step_error_jsonb(
    postgres_engine: AsyncEngine,
) -> None:
    """A real, committed ``PipelineStep.error`` JSONB row with keys beyond
    ``code``/``message`` must never surface those extra keys through the
    Runs API -- proves the public whitelist against real persisted state,
    not just an in-memory dict."""

    ids = RunsFixtureIds()
    await insert_datasets(postgres_engine, ids)
    session_factory = create_session_factory(postgres_engine)
    try:
        run = build_run(dataset_id=ids.dataset_id, status=PipelineRunStatus.FAILED)
        ids.run_ids.append(run.id)

        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(run)
            await uow.pipeline_steps.add_many(
                [
                    PipelineStep(
                        id=uuid4(),
                        run_id=run.id,
                        name="chunk",
                        ordinal=0,
                        status=PipelineStepStatus.FAILED,
                        attempt=1,
                        input_hash="c" * 64,
                        output={},
                        metrics={},
                        error={
                            "code": "PROVIDER_ERROR",
                            "message": "Safe message.",
                            "traceback": "must-not-leak",
                            "provider_response": {"secret": "must-not-leak"},
                            "debug": "must-not-leak",
                        },
                        started_at=datetime(2026, 1, 1, tzinfo=UTC),
                        finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                    )
                ]
            )
            await uow.commit()

        service = RunService(session_factory=session_factory)
        detail = await service.get_run(run.id)

        dumped_error = detail.steps[0].model_dump()["error"]
        assert dumped_error == {"code": "PROVIDER_ERROR", "message": "Safe message."}
    finally:
        await cleanup_runs_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_service_get_run_missing_run_raises_404(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    service = RunService(session_factory=session_factory)

    from sofias_memory.api.errors import SofiasMemoryError

    with pytest.raises(SofiasMemoryError) as error:
        await service.get_run(uuid4())

    assert error.value.status_code == 404
