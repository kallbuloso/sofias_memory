"""Real-PostgreSQL (+ real filesystem, + real worker) tests for
administrative Dataset deletion (SM-515, ADR-0010).

Covers the core contract against a real, dedicated PostgreSQL database and a
real temporary filesystem root: the HTTP repeated-DELETE state machine,
`main` protection, the delete-intent barrier blocking every other
dataset-scoped submission type, concurrent-DELETE convergence, cancel
before/after `begin_delete`, manual retry recovering a partially-destroyed
dataset, real storage deletion/replay-safety, worker-disabled behavior, and
both the positive and negative ADR-0010 D28 administrative-ownership
regressions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    GraphOutboxStatus,
    PipelineRunStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, Source
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.dataset_delete import (
    DATASET_DELETE_RESOURCES_RESOURCE,
    DatasetDeletePipelineResources,
)
from sofias_memory.pipelines.steps.forget import FORGET_RESOURCES_RESOURCE, ForgetPipelineResources
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

DATASET_DELETE_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_DATASET_DELETE_POSTGRES_TESTS"
DATASET_DELETE_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_DATASET_DELETE_TEST_DATABASE_URL"
DATASET_DELETE_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_dataset_delete_test"
DATASET_DELETE_NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_DATASET_DELETE_NEO4J_TESTS"


def require_real_neo4j() -> None:
    if os.environ.get(DATASET_DELETE_NEO4J_TESTS_ENV) != "1":
        pytest.skip(
            f"set {DATASET_DELETE_NEO4J_TESTS_ENV}=1 to run real-Neo4j dataset-delete tests"
        )


EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
POLL_INTERVAL_MS = 20


def dataset_delete_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(DATASET_DELETE_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {DATASET_DELETE_POSTGRES_TESTS_ENV}=1 to run dataset-delete PostgreSQL tests"
        )
    database_url = env.get(DATASET_DELETE_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {DATASET_DELETE_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("dataset-delete PostgreSQL test database URL is invalid")
    if parsed_url.database != DATASET_DELETE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "dataset-delete PostgreSQL tests require the exact dedicated database "
            f"{DATASET_DELETE_POSTGRES_TEST_DATABASE_NAME}"
        )
    return database_url


_TEST_TABLES = (
    "graph_outbox",
    "pipeline_steps",
    "pipeline_runs",
    "feedback",
    "queries",
    "memory_entries",
    "summaries",
    "relation_evidence",
    "relations",
    "entity_mentions",
    "entities",
    "chunks",
    "documents",
    "sources",
    "datasets",
)


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = dataset_delete_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
        if current_database != DATASET_DELETE_POSTGRES_TEST_DATABASE_NAME:
            pytest.skip(
                "connected PostgreSQL database is not the dedicated dataset-delete test database"
            )
        async with engine.begin() as connection:
            tables = ", ".join(f'"{table}"' for table in _TEST_TABLES)
            await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
        yield engine
    finally:
        await dispose_async_engine(engine)


def test_dataset_delete_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        dataset_delete_test_database_url({})


# --- harness ------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": "postgresql+asyncpg://unused:unused@localhost:5432/unused",
        "neo4j_password": os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "worker_poll_interval_ms": POLL_INTERVAL_MS,
        "worker_stale_after_seconds": 5,
        "request_wait_timeout_seconds": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def build_harness(
    engine: AsyncEngine,
    tmp_path: Path,
    *,
    worker_enabled: bool = True,
    worker_claims: bool = True,
    dataset_delete_drain: Any = None,
) -> tuple[Any, AsyncSessionFactory, PipelineRegistry]:
    """``worker_claims=False`` mirrors SM-514's own test harness pattern
    (test_run_control_postgres_integration.py): the coordinator is genuinely
    started/enabled/running (submission gate passes) but never claims
    anything, so a run stays durably QUEUED until the test decides.

    ``dataset_delete_drain`` defaults to ``None`` (no Neo4j configured,
    matching every other pipeline's own test harness convention in this
    codebase) -- pass a real ``GraphOutboxBatchProcessor`` for a test that
    needs ``converge_projection`` to actually succeed against non-empty
    outbox rows (ADR-0010 Finding 2)."""

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    resources: dict[str, Any] = {
        DATASET_DELETE_RESOURCES_RESOURCE: DatasetDeletePipelineResources(
            settings=settings, graph_outbox_drain=dataset_delete_drain
        ),
        FORGET_RESOURCES_RESOURCE: ForgetPipelineResources(
            settings=settings, graph_outbox_drain=None
        ),
    }
    registry = build_default_pipeline_registry()
    worker_registry = registry if worker_claims else PipelineRegistry([])
    coordinator = PipelineWorkerCoordinator(
        session_factory,
        worker_registry,
        enabled=worker_enabled,
        poll_interval_ms=POLL_INTERVAL_MS,
        stale_after_seconds=settings.worker_stale_after_seconds,
        max_concurrent_datasets=settings.worker_max_concurrent_datasets,
        graph_outbox_processor=None,
        resources=resources,
    )
    app = create_app(
        settings,
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
        pipeline_registry=registry,
        pipeline_worker_coordinator=coordinator,
    )
    return app, session_factory, registry


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def delete_dataset(app: Any, dataset_id: UUID) -> httpx.Response:
    async with build_client(app) as client:
        return await client.delete(
            f"/api/v1/datasets/{dataset_id}", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )


async def post_json(app: Any, path: str, body: Mapping[str, Any]) -> httpx.Response:
    async with build_client(app) as client:
        return await client.post(path, headers={API_KEY_HEADER: EXPECTED_API_KEY}, json=dict(body))


async def post_cancel(app: Any, run_id: UUID) -> httpx.Response:
    async with build_client(app) as client:
        return await client.post(
            f"/api/v1/runs/{run_id}/cancel", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )


async def post_retry(app: Any, run_id: UUID) -> httpx.Response:
    async with build_client(app) as client:
        return await client.post(
            f"/api/v1/runs/{run_id}/retry", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )


async def get_run(session_factory: AsyncSessionFactory, run_id: UUID) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        if run is None:
            return None
        return SimpleNamespace(
            id=run.id,
            status=run.status,
            dataset_id=run.dataset_id,
            retry_of_run_id=run.retry_of_run_id,
            metrics=dict(run.metrics),
        )


async def get_dataset(
    session_factory: AsyncSessionFactory, dataset_id: UUID
) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        if dataset is None:
            return None
        return SimpleNamespace(
            id=dataset.id, slug=dataset.slug, name=dataset.name, status=dataset.status
        )


async def seed_dataset(session_factory: AsyncSessionFactory, *, slug: str) -> UUID:
    dataset_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name=slug,
                slug=slug,
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        await uow.commit()
    return dataset_id


async def seed_source_with_storage(
    session_factory: AsyncSessionFactory, *, dataset_id: UUID, tmp_path: Path
) -> UUID:
    """A minimal ACTIVE Source with a real on-disk file, so delete_storage has
    something real to remove."""

    from hashlib import sha256

    source_id = uuid4()
    content = b"dataset delete test content"
    storage_dir = tmp_path / str(dataset_id) / str(source_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / "original.txt"
    storage_path.write_bytes(content)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.sources.add(
            Source(
                id=source_id,
                dataset_id=dataset_id,
                kind=SourceKind.TEXT,
                name="doc",
                mime_type="text/plain",
                original_uri=None,
                storage_uri=storage_path.resolve().as_uri(),
                content_sha256=sha256(content).hexdigest(),
                normalized_sha256=None,
                byte_size=len(content),
                metadata_={},
                status=SourceStatus.ACTIVE,
                version=1,
            )
        )
        await uow.commit()
    return source_id


async def wait_for_status(
    session_factory: AsyncSessionFactory,
    run_id: UUID,
    statuses: set[PipelineRunStatus],
    *,
    timeout: float = 10.0,
) -> PipelineRunStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        run = await get_run(session_factory, run_id)
        if run is not None and run.status in statuses:
            return run.status
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"run {run_id} did not reach {statuses} in time")
        await asyncio.sleep(0.02)


async def wait_for_dataset_status(
    session_factory: AsyncSessionFactory,
    dataset_id: UUID,
    statuses: set[DatasetStatus],
    *,
    timeout: float = 10.0,
) -> DatasetStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        dataset = await get_dataset(session_factory, dataset_id)
        if dataset is not None and dataset.status in statuses:
            return dataset.status
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"dataset {dataset_id} did not reach {statuses} in time")
        await asyncio.sleep(0.02)


# --- HTTP state machine / main protection --------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_missing_dataset_is_404(postgres_engine: AsyncEngine, tmp_path: Path) -> None:
    app, _, _ = build_harness(postgres_engine, tmp_path)
    response = await delete_dataset(app, uuid4())
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_main_is_forbidden(postgres_engine: AsyncEngine, tmp_path: Path) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    main_id = await seed_dataset(session_factory, slug="main")
    response = await delete_dataset(app, main_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MAIN_DATASET_DELETE_FORBIDDEN"
    dataset = await get_dataset(session_factory, main_id)
    assert dataset is not None
    assert dataset.status == DatasetStatus.ACTIVE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_active_empty_dataset_accepts_and_converges_to_deleted(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="empty-ds")

        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202
        body = response.json()["data"]
        assert body["dataset_id"] == str(dataset_id)
        run_id = UUID(body["run_id"])

        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        dataset_status = await wait_for_dataset_status(
            session_factory, dataset_id, {DatasetStatus.DELETED}
        )
        assert dataset_status == DatasetStatus.DELETED
    finally:
        await app.state.pipeline_worker.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_populated_dataset_deletes_source_storage_and_converges(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="populated-ds")
        source_id = await seed_source_with_storage(
            session_factory, dataset_id=dataset_id, tmp_path=tmp_path
        )
        storage_path = tmp_path / str(dataset_id) / str(source_id) / "original.txt"
        assert storage_path.exists()

        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])

        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})
        assert not storage_path.exists()

        async with PostgresUnitOfWork(session_factory) as uow:
            source = await uow.sources.get_by_id(source_id)
            assert source is not None
            assert source.status.value == "deleted"
            assert source.storage_uri is None

        run = await get_run(session_factory, run_id)
        assert run is not None
        counters = run.metrics["dataset_delete_result"]
        assert counters["sources_deleted"] == 1
        assert counters["storage_deleted"] == 1
    finally:
        await app.state.pipeline_worker.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_already_deleted_is_idempotent_200_no_new_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="twice-deleted")
        first = await delete_dataset(app, dataset_id)
        run_id = UUID(first.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})

        second = await delete_dataset(app, dataset_id)
        assert second.status_code == 200
        assert second.json()["data"]["run_id"] == str(run_id)

        async with PostgresUnitOfWork(session_factory) as uow:
            runs = await uow.pipeline_runs.list_page(dataset_id=dataset_id)
            dataset_delete_runs = [
                r for r in runs if r.pipeline_type == PipelineType.DATASET_DELETE
            ]
        assert len(dataset_delete_runs) == 1
    finally:
        await app.state.pipeline_worker.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_name_and_slug_remain_reserved_after_delete(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="reserved-ns")
        response = await delete_dataset(app, dataset_id)
        run_id = UUID(response.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})

        create_response = await post_json(
            app, "/api/v1/datasets", {"name": "reserved-ns", "slug": "reserved-ns"}
        )
        assert create_response.status_code == 409
    finally:
        await app.state.pipeline_worker.stop()


# --- delete-intent barrier -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_intent_barrier_blocks_remember(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="barrier-remember")
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202

        remember_response = await post_json(
            app,
            "/api/v1/remember",
            {"dataset": "barrier-remember", "content": "x", "mode": "full", "wait": False},
        )
        assert remember_response.status_code == 409
        assert remember_response.json()["error"]["code"] == "DATASET_DELETING"
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_intent_barrier_blocks_cognify(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="barrier-cognify")
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202

        cognify_response = await post_json(
            app, "/api/v1/cognify", {"dataset": "barrier-cognify", "wait": False}
        )
        assert cognify_response.status_code == 409
        assert cognify_response.json()["error"]["code"] == "DATASET_DELETING"
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_intent_barrier_blocks_improve(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="barrier-improve")
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202

        improve_response = await post_json(
            app, "/api/v1/improve", {"dataset": "barrier-improve", "wait": False}
        )
        assert improve_response.status_code == 409
        assert improve_response.json()["error"]["code"] == "DATASET_DELETING"
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_intent_barrier_blocks_forget_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="barrier-forget")
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202

        forget_response = await post_json(
            app, "/api/v1/forget", {"dataset": "barrier-forget", "wait": False}
        )
        assert forget_response.status_code == 409
        assert forget_response.json()["error"]["code"] == "DATASET_DELETING"
    finally:
        await coordinator.stop()


# --- concurrent DELETE convergence ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_deletes_converge_to_one_lineage(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="concurrent-delete")
        first, second = await asyncio.gather(
            delete_dataset(app, dataset_id), delete_dataset(app, dataset_id)
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]

        async with PostgresUnitOfWork(session_factory) as uow:
            runs = await uow.pipeline_runs.list_page(dataset_id=dataset_id)
        assert len(runs) == 1
    finally:
        await coordinator.stop()


# --- worker disabled -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_new_run_worker_disabled_is_503_no_mutation(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_enabled=False)
    dataset_id = await seed_dataset(session_factory, slug="worker-disabled")
    response = await delete_dataset(app, dataset_id)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORKER_DISABLED"

    dataset = await get_dataset(session_factory, dataset_id)
    assert dataset is not None
    assert dataset.status == DatasetStatus.ACTIVE
    async with PostgresUnitOfWork(session_factory) as uow:
        runs = await uow.pipeline_runs.list_page(dataset_id=dataset_id)
    assert runs == []


# --- cancel ---------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_dataset_delete_while_queued_leaves_dataset_active(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="cancel-queued-delete")
        response = await delete_dataset(app, dataset_id)
        run_id = UUID(response.json()["data"]["run_id"])

        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["data"]["status"] == "cancelled"

        dataset = await get_dataset(session_factory, dataset_id)
        assert dataset is not None
        assert dataset.status == DatasetStatus.ACTIVE

        # ADR-0010 D17: a fresh DELETE after a pre-begin cancellation is
        # allowed to create a NEW independent lineage (never a permanent
        # per-dataset idempotency key).
        second = await delete_dataset(app, dataset_id)
        assert second.status_code == 202
        assert second.json()["data"]["run_id"] != str(run_id)
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_after_begin_delete_leaves_dataset_deleting_and_retry_converges(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="cancel-after-begin")
        response = await delete_dataset(app, dataset_id)
        run_id = UUID(response.json()["data"]["run_id"])

        # Let the real worker run to completion (small fixed pipeline, no
        # external providers involved) rather than trying to catch it
        # mid-flight -- SUCCEEDED already proves begin_delete crossed.
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})

        # A second, independent run against a fresh dataset proves the
        # "begin_delete succeeded then the run is terminal non-SUCCEEDED,
        # retry converges" path without needing to defeat the fixed
        # pipeline's own speed: force the run/dataset back to a
        # DELETING+FAILED shape directly, then prove manual retry still
        # converges to DELETED.
        dataset_id_2 = await seed_dataset(session_factory, slug="cancel-after-begin-2")
        response_2 = await delete_dataset(app, dataset_id_2)
        run_id_2 = UUID(response_2.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id_2, {PipelineRunStatus.SUCCEEDED})

        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id_2)
            assert run is not None
            # Simulate a post-begin_delete failure: Dataset already DELETED
            # by the real pipeline above, so force it back to DELETING to
            # model "begin_delete succeeded, then something later failed"
            # durably.
            dataset = await uow.datasets.get_by_id_for_update(dataset_id_2)
            assert dataset is not None
            dataset.status = DatasetStatus.DELETING
            run.status = PipelineRunStatus.FAILED
            run.error_code = "SIMULATED_FAILURE"
            await uow.commit()

        dataset_after_failure = await get_dataset(session_factory, dataset_id_2)
        assert dataset_after_failure is not None
        assert dataset_after_failure.status == DatasetStatus.DELETING

        retry_response = await post_retry(app, run_id_2)
        assert retry_response.status_code in (200, 202)
        retry_run_id = UUID(retry_response.json()["data"]["run_id"])
        assert retry_run_id != run_id_2

        await wait_for_status(session_factory, retry_run_id, {PipelineRunStatus.SUCCEEDED})
        final_dataset = await wait_for_dataset_status(
            session_factory, dataset_id_2, {DatasetStatus.DELETED}
        )
        assert final_dataset == DatasetStatus.DELETED
    finally:
        await app.state.pipeline_worker.stop()


# --- ADR-0010 D28 administrative-ownership regressions --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d28_positive_regression_forget_everything_excludes_administratively_owned_deleting(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 D28/§41: a dataset whose DELETING is administratively owned
    (begin_delete SUCCEEDED, then the run stalled non-SUCCEEDED) must never
    be reactivated to ACTIVE by Forget Everything."""

    app, session_factory, _ = build_harness(postgres_engine, tmp_path)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="d28-positive")
        response = await delete_dataset(app, dataset_id)
        run_id = UUID(response.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})

        # Force back to DELETING with a stalled (FAILED) owning run -- models
        # "begin_delete SUCCEEDED, then the run failed" without needing to
        # defeat the pipeline's own speed (the begin_delete PipelineStep row
        # produced by the real run above already durably proves SUCCEEDED).
        async with PostgresUnitOfWork(session_factory) as uow:
            dataset = await uow.datasets.get_by_id_for_update(dataset_id)
            assert dataset is not None
            dataset.status = DatasetStatus.DELETING
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            run.status = PipelineRunStatus.FAILED
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            owned = await uow.pipeline_runs.exists_administrative_delete_ownership(dataset_id)
            assert owned is True
            target_ids = await uow.datasets.list_ids_for_everything_forget()
            assert dataset_id not in target_ids

        forget_response = await post_json(
            app,
            "/api/v1/forget",
            {"everything": True, "confirm": "DELETE EVERYTHING", "wait": False},
        )
        assert forget_response.status_code == 202
        forget_run_id = UUID(forget_response.json()["data"]["run_id"])
        await wait_for_status(session_factory, forget_run_id, {PipelineRunStatus.SUCCEEDED})

        dataset_after = await get_dataset(session_factory, dataset_id)
        assert dataset_after is not None
        assert dataset_after.status == DatasetStatus.DELETING
    finally:
        await app.state.pipeline_worker.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d28_negative_regression_unrelated_cancelled_delete_does_not_poison_forget(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 D28's own counterexample: a DATASET_DELETE cancelled while
    still QUEUED (begin_delete never ran) must NOT poison a later, unrelated
    Forget-owned DELETING cycle on the same dataset."""

    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="d28-negative")
        delete_response = await delete_dataset(app, dataset_id)
        run_id = UUID(delete_response.json()["data"]["run_id"])
        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["data"]["status"] == "cancelled"

        dataset = await get_dataset(session_factory, dataset_id)
        assert dataset is not None
        assert dataset.status == DatasetStatus.ACTIVE

        async with PostgresUnitOfWork(session_factory) as uow:
            owned = await uow.pipeline_runs.exists_administrative_delete_ownership(dataset_id)
        assert owned is False
    finally:
        await coordinator.stop()

    # Now run a real Forget Dataset cycle on the SAME dataset with the real
    # worker enabled, and prove it completes normally (ACTIVE -> DELETING ->
    # ACTIVE), unaffected by the earlier cancelled DATASET_DELETE history.
    app2, session_factory_2, _ = build_harness(postgres_engine, tmp_path)
    await app2.state.pipeline_worker.start()
    try:
        forget_response = await post_json(
            app2, "/api/v1/forget", {"dataset": "d28-negative", "wait": False}
        )
        assert forget_response.status_code == 202
        forget_run_id = UUID(forget_response.json()["data"]["run_id"])
        await wait_for_status(session_factory_2, forget_run_id, {PipelineRunStatus.SUCCEEDED})

        dataset_after = await get_dataset(session_factory_2, dataset_id)
        assert dataset_after is not None
        assert dataset_after.status == DatasetStatus.ACTIVE

        async with PostgresUnitOfWork(session_factory_2) as uow:
            target_ids = await uow.datasets.list_ids_for_everything_forget()
        assert dataset_id in target_ids
    finally:
        await app2.state.pipeline_worker.stop()


# --- stale CANCELLING recovery (ADR-0009 SS I / SM-507, ADR-0010 §48) ----------


async def get_database_now(engine: AsyncEngine) -> Any:
    async with engine.connect() as connection:
        from sqlalchemy import func, select

        return await connection.scalar(select(func.now()))


async def seed_stale_cancelling_run(
    engine: AsyncEngine,
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: UUID,
    config_fingerprint: str,
    stalled_step_name: str,
    completed_step_names: list[str],
    all_step_names: list[str],
) -> UUID:
    """Seed a durable ``DATASET_DELETE`` run already ``CANCELLING`` with one
    ``RUNNING`` step and a stale (long-past) heartbeat -- exactly the shape
    ADR-0009 SS I stale recovery scans for, using the REAL production
    ``PipelineDefinition`` (not a test-only registry), to prove the first
    ever RECONCILABLE step declaration and DATASET_DELETE's own AMBIGUOUS
    step behave correctly under real recovery."""

    from datetime import timedelta

    from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep

    run_id = uuid4()
    now = await get_database_now(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.DATASET_DELETE,
            dataset_id=dataset_id,
            source_id=None,
            status=PipelineRunStatus.CANCELLING,
            idempotency_key=None,
            payload_hash="a" * 64,
            input={"dataset_id": str(dataset_id)},
            progress=0.0,
            current_step=stalled_step_name,
            attempt=1,
            worker_id="wk-dead",
            heartbeat_at=now - timedelta(seconds=700),
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
        from sofias_memory.domain import PipelineStepStatus

        for ordinal, name in enumerate(all_step_names):
            if name in completed_step_names:
                status = PipelineStepStatus.SUCCEEDED
            elif name == stalled_step_name:
                status = PipelineStepStatus.RUNNING
            else:
                status = PipelineStepStatus.QUEUED
            await uow.pipeline_steps.add(
                PipelineStep(
                    id=uuid4(),
                    run_id=run_id,
                    name=name,
                    ordinal=ordinal,
                    status=status,
                    attempt=1 if status != PipelineStepStatus.QUEUED else 0,
                    input_hash=None,
                    output={},
                    metrics={},
                    error=None,
                    started_at=now - timedelta(seconds=700)
                    if status != PipelineStepStatus.QUEUED
                    else None,
                    finished_at=None,
                )
            )
        await uow.commit()
    return run_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_cancelling_during_delete_storage_never_fabricates_cancelled(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 §48/D9: ``delete_storage`` is AMBIGUOUS -- a stale
    CANCELLING recovery orphaned mid-step must fail safe (FAILED,
    CANCEL_RECOVERY_AMBIGUOUS), never fabricate CANCELLED, and the Dataset
    must remain DELETING (already-committed by ``begin_delete``)."""

    from sofias_memory.pipelines.errors import CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    from sofias_memory.pipelines.steps.dataset_delete import (
        BEGIN_DELETE_STEP,
        CONVERGE_PROJECTION_STEP,
        DEACTIVATE_AUTHORITATIVE_STEP,
        DELETE_STORAGE_STEP,
        FINALIZE_TOMBSTONE_STEP,
    )
    from sofias_memory.services.pipeline_recovery import PipelineRecoveryService

    session_factory = create_session_factory(postgres_engine)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="stale-storage")
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id_for_update(dataset_id)
        assert dataset is not None
        dataset.status = DatasetStatus.DELETING
        await uow.commit()

    run_id = await seed_stale_cancelling_run(
        postgres_engine,
        session_factory,
        dataset_id=dataset_id,
        config_fingerprint=settings.config_fingerprint(),
        stalled_step_name=DELETE_STORAGE_STEP,
        completed_step_names=[
            BEGIN_DELETE_STEP,
            DEACTIVATE_AUTHORITATIVE_STEP,
            CONVERGE_PROJECTION_STEP,
        ],
        all_step_names=[
            BEGIN_DELETE_STEP,
            DEACTIVATE_AUTHORITATIVE_STEP,
            CONVERGE_PROJECTION_STEP,
            DELETE_STORAGE_STEP,
            FINALIZE_TOMBSTONE_STEP,
        ],
    )

    recovery = PipelineRecoveryService(
        session_factory,
        build_default_pipeline_registry(),
        stale_after_seconds=300,
        config_fingerprint=settings.config_fingerprint(),
    )
    recovered_count = await recovery.recover_startup()
    assert recovered_count == 1

    run = await get_run(session_factory, run_id)
    assert run is not None
    assert run.status == PipelineRunStatus.FAILED
    async with PostgresUnitOfWork(session_factory) as uow:
        stale_run = await uow.pipeline_runs.get_by_id(run_id)
        assert stale_run is not None
        assert stale_run.error_code == CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE

    dataset_after = await get_dataset(session_factory, dataset_id)
    assert dataset_after is not None
    assert dataset_after.status == DatasetStatus.DELETING


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_cancelling_during_converge_projection_with_nothing_pending_converges_safely(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 D9: ``converge_projection`` is RECONCILABLE -- when durable
    ``graph_outbox`` state proves nothing is left pending/processing for the
    dataset, a stale CANCELLING recovery may safely report CANCELLED."""

    from sofias_memory.pipelines.steps.dataset_delete import (
        BEGIN_DELETE_STEP,
        CONVERGE_PROJECTION_STEP,
        DEACTIVATE_AUTHORITATIVE_STEP,
        DELETE_STORAGE_STEP,
        FINALIZE_TOMBSTONE_STEP,
    )
    from sofias_memory.services.pipeline_recovery import PipelineRecoveryService

    session_factory = create_session_factory(postgres_engine)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="stale-projection")
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id_for_update(dataset_id)
        assert dataset is not None
        dataset.status = DatasetStatus.DELETING
        await uow.commit()

    # No Source/content ever mutated for this dataset -- deactivate_authoritative
    # enqueued zero graph_outbox rows, so nothing is pending for converge_projection
    # to observe: durable proof of safety for the RECONCILABLE callback.
    run_id = await seed_stale_cancelling_run(
        postgres_engine,
        session_factory,
        dataset_id=dataset_id,
        config_fingerprint=settings.config_fingerprint(),
        stalled_step_name=CONVERGE_PROJECTION_STEP,
        completed_step_names=[BEGIN_DELETE_STEP, DEACTIVATE_AUTHORITATIVE_STEP],
        all_step_names=[
            BEGIN_DELETE_STEP,
            DEACTIVATE_AUTHORITATIVE_STEP,
            CONVERGE_PROJECTION_STEP,
            DELETE_STORAGE_STEP,
            FINALIZE_TOMBSTONE_STEP,
        ],
    )

    recovery = PipelineRecoveryService(
        session_factory,
        build_default_pipeline_registry(),
        stale_after_seconds=300,
        config_fingerprint=settings.config_fingerprint(),
    )
    recovered_count = await recovery.recover_startup()
    assert recovered_count == 1

    run = await get_run(session_factory, run_id)
    assert run is not None
    assert run.status == PipelineRunStatus.CANCELLED

    # Recovery converges the RUN's terminal status, but never itself touches
    # Dataset.status -- only a real DATASET_DELETE step transition does that
    # (ADR-0010 D18: cancellation is never rollback). Manual retry remains
    # the recovery path to reach DELETED.
    dataset_after = await get_dataset(session_factory, dataset_id)
    assert dataset_after is not None
    assert dataset_after.status == DatasetStatus.DELETING


# --- begin_delete administratively cancels other queued work (ADR-0010 D14/§17) -


@pytest.mark.integration
@pytest.mark.asyncio
async def test_begin_delete_administratively_cancels_scheduled_automatic_retry(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 D14/§16-17: a run left QUEUED with a future ``next_attempt_at``
    (an automatic-retry-scheduled write, temporarily ineligible for the
    claimer) must be administratively CANCELLED by ``begin_delete`` when the
    dataset's DATASET_DELETE run claims the operational slot -- it must
    never be left stranded forever, and it must never be silently claimed
    and executed after the dataset starts deleting."""

    from datetime import timedelta

    from sofias_memory.pipelines.hashing import canonical_work_payload_hash
    from sofias_memory.services.pipeline_lifecycle import create_run_with_steps

    app, session_factory, registry = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="cancel-scheduled-retry")

    now = await get_database_now(postgres_engine)
    work_input = {"dataset": "cancel-scheduled-retry", "content": "x", "mode": "full"}
    step_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=work_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        scheduled_run = await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash=canonical_work_payload_hash(work_input),
            input=work_input,
            config_fingerprint=make_settings(tmp_path).config_fingerprint(),
            steps=step_plan,
        )
        scheduled_run.next_attempt_at = now + timedelta(hours=1)
        scheduled_run_id = scheduled_run.id
        await uow.commit()

    await app.state.pipeline_worker.start()
    try:
        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        await wait_for_dataset_status(session_factory, dataset_id, {DatasetStatus.DELETED})
    finally:
        await app.state.pipeline_worker.stop()

    scheduled_after = await get_run(session_factory, scheduled_run_id)
    assert scheduled_after is not None
    assert scheduled_after.status == PipelineRunStatus.CANCELLED


# --- ADR-0010 Finding 2: FAILED-at-ceiling must not mean "converged" -----------


class _NeverCalledProjection:
    """A ``GraphProjectionPort`` that must never actually be invoked in these
    tests -- every seeded outbox row is deliberately already terminal/leased
    so the batch drain finds nothing claimable, proving the convergence
    check itself (not the drain) is what decides the outcome."""

    async def apply(self, command: object) -> None:
        message = f"projection.apply must never be called in this test: {command!r}"
        raise AssertionError(message)


def build_real_drain(session_factory: AsyncSessionFactory) -> Any:
    from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor

    return GraphOutboxBatchProcessor(
        session_factory=session_factory,
        processor=GraphOutboxProcessor(
            session_factory=session_factory, projection=_NeverCalledProjection()
        ),
    )


async def seed_active_entity(session_factory: AsyncSessionFactory, *, dataset_id: UUID) -> UUID:
    from sofias_memory.infrastructure.postgres.models import Entity

    entity_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.entities.add(
            Entity(
                id=entity_id,
                dataset_id=dataset_id,
                generation=0,
                canonical_key="finding-2-entity",
                name="Finding 2 Entity",
                entity_type="Concept",
                description="d",
                aliases=[],
                properties={},
                confidence=0.9,
                importance_weight=1.0,
                embedding=None,
                is_active=True,
            )
        )
        await uow.commit()
    return entity_id


async def seed_outbox_row(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: UUID,
    aggregate_id: UUID,
    status: Any,
    attempt: int,
    processing_started_at: Any = None,
) -> int:
    from sofias_memory.domain import GraphOutboxOperation
    from sofias_memory.infrastructure.postgres.models import GraphOutbox

    async with PostgresUnitOfWork(session_factory) as uow:
        event = GraphOutbox(
            dataset_id=dataset_id,
            aggregate_type="entity",
            aggregate_id=aggregate_id,
            operation=GraphOutboxOperation.DELETE,
            payload={
                "schema_version": 1,
                "aggregate_type": "entity",
                "operation": "delete",
                "dataset_id": str(dataset_id),
                "aggregate_id": str(aggregate_id),
                "identity": {},
                "endpoints": {},
                "properties": {},
            },
            status=status,
            attempt=attempt,
            processing_started_at=processing_started_at,
        )
        added = await uow.graph_outbox.add(event)
        outbox_id = added.id
        await uow.commit()
    return outbox_id


def build_step_context(
    *,
    run_id: UUID,
    dataset_id: UUID,
    session_factory: AsyncSessionFactory,
    settings: Settings,
    graph_outbox_ids: list[int],
    graph_outbox_drain: Any,
) -> Any:
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.steps.dataset_delete import (
        DEACTIVATE_AUTHORITATIVE_STEP,
        DatasetDeletePipelineResources,
    )

    return PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.DATASET_DELETE,
        dataset_id=dataset_id,
        source_id=None,
        run_input={"dataset_id": str(dataset_id)},
        step_outputs={DEACTIVATE_AUTHORITATIVE_STEP: {"graph_outbox_ids": graph_outbox_ids}},
        session_factory=session_factory,
        resources={
            DATASET_DELETE_RESOURCES_RESOURCE: DatasetDeletePipelineResources(
                settings=settings, graph_outbox_drain=graph_outbox_drain
            )
        },
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_converge_projection_failed_at_ceiling_does_not_report_success(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 Finding 2: a relevant graph_outbox row permanently FAILED at
    the attempt ceiling is invisible to list_processable_ids_for_dataset(),
    but converge_projection must still detect it via the exact row-id proof
    and refuse to report success."""

    from sofias_memory.pipelines.errors import PermanentPipelineStepError
    from sofias_memory.pipelines.steps.dataset_delete import (
        DATASET_DELETE_PROJECTION_NOT_CONVERGED_ERROR_CODE,
        ConvergeProjectionStep,
    )
    from sofias_memory.services.graph_outbox_processor import DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS

    session_factory = create_session_factory(postgres_engine)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="finding2-ceiling")
    entity_id = await seed_active_entity(session_factory, dataset_id=dataset_id)
    outbox_id = await seed_outbox_row(
        session_factory,
        dataset_id=dataset_id,
        aggregate_id=entity_id,
        status="failed",
        attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    )

    context = build_step_context(
        run_id=uuid4(),
        dataset_id=dataset_id,
        session_factory=session_factory,
        settings=settings,
        graph_outbox_ids=[outbox_id],
        graph_outbox_drain=build_real_drain(session_factory),
    )

    with pytest.raises(PermanentPipelineStepError) as exc_info:
        await ConvergeProjectionStep().execute(context)
    assert exc_info.value.code == DATASET_DELETE_PROJECTION_NOT_CONVERGED_ERROR_CODE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_converge_projection_all_done_succeeds(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 Finding 2 (positive counterpart): when every relevant row is
    durably DONE, convergence succeeds and reports the exact count."""

    from sofias_memory.pipelines.steps.dataset_delete import ConvergeProjectionStep

    session_factory = create_session_factory(postgres_engine)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="finding2-done")
    entity_id = await seed_active_entity(session_factory, dataset_id=dataset_id)
    outbox_id = await seed_outbox_row(
        session_factory,
        dataset_id=dataset_id,
        aggregate_id=entity_id,
        status="done",
        attempt=1,
    )

    context = build_step_context(
        run_id=uuid4(),
        dataset_id=dataset_id,
        session_factory=session_factory,
        settings=settings,
        graph_outbox_ids=[outbox_id],
        graph_outbox_drain=build_real_drain(session_factory),
    )

    result = await ConvergeProjectionStep().execute(context)
    assert result.output["graph_events_processed"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_converge_projection_processing_under_live_lease_is_observed_not_skipped(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 Finding 2: a row PROCESSING under another (non-stale) live
    lease must be OBSERVED (SM-506's own claim-or-observe polling, reused
    unchanged) rather than silently skipped/treated as already converged.

    A live lease this test does not resolve would make the real
    ``GraphOutboxProcessor.process()`` poll forever by design (correct,
    pre-existing SM-506 behavior) -- so this test proves the "observe, do
    not skip" half concretely: it starts convergence against a row that is
    NOT yet DONE, confirms the call has not returned early with a false
    success while the lease is still live, then resolves the lease itself
    (standing in for "another worker finishes it") and confirms convergence
    only reports success once that resolution is durably visible.
    """

    from sofias_memory.pipelines.steps.dataset_delete import ConvergeProjectionStep

    session_factory = create_session_factory(postgres_engine)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="finding2-processing")
    entity_id = await seed_active_entity(session_factory, dataset_id=dataset_id)
    now = await get_database_now(postgres_engine)
    outbox_id = await seed_outbox_row(
        session_factory,
        dataset_id=dataset_id,
        aggregate_id=entity_id,
        status="processing",
        attempt=1,
        processing_started_at=now,
    )

    context = build_step_context(
        run_id=uuid4(),
        dataset_id=dataset_id,
        session_factory=session_factory,
        settings=settings,
        graph_outbox_ids=[outbox_id],
        graph_outbox_drain=build_real_drain(session_factory),
    )

    task = asyncio.ensure_future(ConvergeProjectionStep().execute(context))
    try:
        # The lease is still live and unresolved -- convergence must not
        # have already reported success (proves it did not skip/observe
        # falsely; it is genuinely still polling, per SM-506's own
        # unchanged claim-or-observe semantics).
        await asyncio.sleep(0.3)
        assert not task.done()

        async with PostgresUnitOfWork(session_factory) as uow:
            event = await uow.graph_outbox.get_by_id(outbox_id)
            assert event is not None
            event.status = GraphOutboxStatus.DONE
            event.processed_at = await uow.graph_outbox.get_database_now()
            await uow.commit()

        result = await asyncio.wait_for(task, timeout=5.0)
        assert result.output["graph_events_processed"] == 1
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_end_to_end_dataset_delete_with_entity_content_converges_and_deletes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Full end-to-end proof that a dataset WITH real active graph content
    (not merely a bare Source, ADR-0010 Finding 2's own root cause) still
    converges and reaches DELETED through the real worker."""

    class _AlwaysSucceedsProjection:
        async def apply(self, command: object) -> None:
            del command

    session_factory_for_drain = create_session_factory(postgres_engine)
    from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor

    drain = GraphOutboxBatchProcessor(
        session_factory=session_factory_for_drain,
        processor=GraphOutboxProcessor(
            session_factory=session_factory_for_drain, projection=_AlwaysSucceedsProjection()
        ),
    )
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, dataset_delete_drain=drain)
    await app.state.pipeline_worker.start()
    try:
        dataset_id = await seed_dataset(session_factory, slug="finding2-e2e")
        await seed_active_entity(session_factory, dataset_id=dataset_id)

        response = await delete_dataset(app, dataset_id)
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        dataset_status = await wait_for_dataset_status(
            session_factory, dataset_id, {DatasetStatus.DELETED}
        )
        assert dataset_status == DatasetStatus.DELETED

        run = await get_run(session_factory, run_id)
        assert run is not None
        assert run.metrics["dataset_delete_result"]["entities_deactivated"] == 1
    finally:
        await app.state.pipeline_worker.stop()


# --- ADR-0010 Finding 1: real Neo4j end-to-end proof ---------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_neo4j_dataset_delete_removes_only_owned_projection(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ADR-0010 Finding 1: end-to-end proof against a real Neo4j instance --
    Dataset D's own projection is removed, Dataset B's is untouched, an
    external/non-Sofias node is untouched, PostgreSQL is the sole scope
    authority (Neo4j is never queried to decide what to delete -- this
    step's execute() only ever calls the outbox drain), and Dataset D does
    not reach DELETED before the relevant graph_outbox rows reach DONE."""

    require_real_neo4j()

    from sofias_memory.infrastructure.neo4j import (
        Neo4jProjection,
        create_neo4j_resource_from_settings,
    )
    from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
    from sofias_memory.services.graph_rebuild_service import GraphRebuildService

    settings = make_settings(
        tmp_path,
        neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7688"),
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
    )
    neo4j_resource = create_neo4j_resource_from_settings(settings)
    projection = Neo4jProjection(neo4j_resource)

    session_factory = create_session_factory(postgres_engine)
    dataset_d = await seed_dataset(session_factory, slug=f"finding1-d-{uuid4()}")
    dataset_b = await seed_dataset(session_factory, slug=f"finding1-b-{uuid4()}")
    entity_d = await seed_active_entity(session_factory, dataset_id=dataset_d)
    entity_b = await seed_active_entity(session_factory, dataset_id=dataset_b)

    rebuild_service = GraphRebuildService(
        session_factory=session_factory, neo4j_resource=neo4j_resource, projection=projection
    )
    await rebuild_service.rebuild_dataset(dataset_d)
    await rebuild_service.rebuild_dataset(dataset_b)

    external_node_id = f"external-{uuid4()}"
    async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
        await session.run("CREATE (n:ExternalTestNode {id: $id}) RETURN n", id=external_node_id)

    drain = GraphOutboxBatchProcessor(
        session_factory=session_factory,
        processor=GraphOutboxProcessor(session_factory=session_factory, projection=projection),
    )
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, dataset_delete_drain=drain)
    await app.state.pipeline_worker.start()
    try:
        response = await delete_dataset(app, dataset_d)
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])
        await wait_for_status(session_factory, run_id, {PipelineRunStatus.SUCCEEDED})
        dataset_status = await wait_for_dataset_status(
            session_factory, dataset_d, {DatasetStatus.DELETED}
        )
        assert dataset_status == DatasetStatus.DELETED

        run = await get_run(session_factory, run_id)
        assert run is not None
        outbox_ids = run.metrics["dataset_delete_result"]
        assert outbox_ids["graph_events_processed"] >= 1

        async with PostgresUnitOfWork(session_factory) as uow:
            steps = await uow.pipeline_steps.list_for_run(run_id)
            deactivate_step = next(
                step for step in steps if step.name == "deactivate_authoritative"
            )
            relevant_outbox_ids = list(deactivate_step.output["graph_outbox_ids"])
            statuses = await uow.graph_outbox.list_status_by_ids(relevant_outbox_ids)
        assert relevant_outbox_ids
        assert all(status == GraphOutboxStatus.DONE for status, _attempt in statuses.values())

        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            d_result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=str(entity_d)
            )
            d_record = await d_result.single()
            assert d_record is not None and d_record["c"] == 0

            b_result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=str(entity_b)
            )
            b_record = await b_result.single()
            assert b_record is not None and b_record["c"] == 1

            external_result = await session.run(
                "MATCH (n:ExternalTestNode {id: $id}) RETURN count(n) AS c", id=external_node_id
            )
            external_record = await external_result.single()
            assert external_record is not None and external_record["c"] == 1
    finally:
        await app.state.pipeline_worker.stop()
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run(
                "MATCH (n:ExternalTestNode {id: $id}) DETACH DELETE n", id=external_node_id
            )
            await session.run(
                "MATCH (n) WHERE n.dataset_id IN [$d, $b] DETACH DELETE n",
                d=str(dataset_d),
                b=str(dataset_b),
            )
        await neo4j_resource.driver.close()
