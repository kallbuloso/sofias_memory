"""Real-PostgreSQL (+ real filesystem, + optional real Neo4j) tests for
asynchronous Forget (SM-512).

Proves the B5 async lifecycle end to end against a real, dedicated
PostgreSQL database and a real temporary filesystem root: durable
submission, wait=false/wait=true, Idempotency-Key replay, a real worker
executing the five registered Forget steps to ``succeeded`` for SOURCE
(full/memory-only), DATASET (full/memory-only) and EVERYTHING scope, shared
entity/relation preservation across sources, B4-legacy ``DELETING``-target
recovery/conflict compatibility, and crash/resume convergence. Real Neo4j
coverage (projection removal, external-node preservation) is opt-in via its
own gate.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from io import StringIO
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
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.neo4j import Neo4jProjection, create_neo4j_resource_from_settings
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    PipelineRun,
    Source,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import clear_log_context, configure_logging
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.forget import (
    FORGET_RESOURCES_RESOURCE,
    AuthoritativeMutationStep,
    ForgetPipelineResources,
)
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

FORGET_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_FORGET_POSTGRES_TESTS"
FORGET_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_FORGET_TEST_DATABASE_URL"
FORGET_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_forget_test"
FORGET_NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_FORGET_NEO4J_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
EMBEDDING_DIMENSIONS = 3072
POLL_INTERVAL_MS = 20


def forget_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(FORGET_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {FORGET_POSTGRES_TESTS_ENV}=1 to run forget PostgreSQL tests")
    database_url = env.get(FORGET_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {FORGET_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    _validate_forget_test_database_url(database_url)
    return database_url


def _validate_forget_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("forget PostgreSQL test database URL is invalid")
    if parsed_url.database != FORGET_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "forget PostgreSQL tests require the exact dedicated database "
            f"{FORGET_POSTGRES_TEST_DATABASE_NAME}"
        )


def require_real_neo4j() -> None:
    if os.environ.get(FORGET_NEO4J_TESTS_ENV) != "1":
        pytest.skip(f"set {FORGET_NEO4J_TESTS_ENV}=1 to run real-Neo4j forget tests")


_FORGET_TEST_TABLES = (
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


async def _truncate_forget_test_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table}"' for table in _FORGET_TEST_TABLES)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = forget_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected(engine)
        # EVERYTHING-scope forget operates over every row in the database,
        # so this dedicated shared test database must start each test
        # function empty -- otherwise leftover sources from a prior test's
        # own (different) tmp_path data_directory would trip the storage
        # path containment guard when an EVERYTHING test walks all datasets.
        await _truncate_forget_test_tables(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != FORGET_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated forget test database")


def test_forget_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        forget_test_database_url({})


def test_forget_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        forget_test_database_url(
            {
                FORGET_POSTGRES_TESTS_ENV: "1",
                FORGET_POSTGRES_TEST_DATABASE_URL_ENV: "postgresql+asyncpg://u:p@localhost:5432/wrong",
            }
        )


# --- harness ------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": "postgresql+asyncpg://unused:unused@localhost:5432/unused",
        "neo4j_uri": os.environ.get("NEO4J_URI", "bolt://localhost:7688"),
        "neo4j_password": os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "worker_poll_interval_ms": POLL_INTERVAL_MS,
        "worker_stale_after_seconds": 5,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "request_wait_timeout_seconds": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def build_harness(
    engine: AsyncEngine, tmp_path: Path, *, with_neo4j: bool = False, worker_enabled: bool = True
) -> tuple[Any, AsyncSessionFactory, PipelineRegistry, object | None]:
    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    neo4j_resource = None
    graph_outbox_drain: GraphOutboxBatchProcessor | None = None
    if with_neo4j:
        neo4j_resource = create_neo4j_resource_from_settings(settings)
        projection = Neo4jProjection(neo4j_resource)
        graph_outbox_drain = GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=GraphOutboxProcessor(session_factory=session_factory, projection=projection),
        )
    resources: dict[str, Any] = {
        FORGET_RESOURCES_RESOURCE: ForgetPipelineResources(
            settings=settings, graph_outbox_drain=graph_outbox_drain
        )
    }
    registry = build_default_pipeline_registry()
    coordinator = PipelineWorkerCoordinator(
        session_factory,
        registry,
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
    return app, session_factory, registry, neo4j_resource


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def post_forget(
    app: Any, body: Mapping[str, Any], *, idempotency_key: str | None = None
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/forget", headers=headers, json=dict(body))


async def wait_for_terminal(
    session_factory: AsyncSessionFactory, run_id: UUID, *, timeout_seconds: float = 10.0
) -> PipelineRunStatus:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            status = run.status if run is not None else None
        if status in (
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.FAILED,
            PipelineRunStatus.CANCELLED,
        ):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach a terminal state within {timeout_seconds}s")


def vector_literal(dimensions: int) -> list[float]:
    return [0.0] * dimensions


async def seed_dataset(session_factory: AsyncSessionFactory, *, slug: str) -> Dataset:
    dataset = Dataset(id=uuid4(), name=slug, slug=slug, description=None, active_generation=0)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(dataset)
        await uow.commit()
    return dataset


async def seed_source_with_content(
    session_factory: AsyncSessionFactory,
    tmp_path: Path,
    *,
    dataset_id: UUID,
    entity: Entity | None = None,
) -> tuple[Source, Entity]:
    """One Source with a real on-disk file, one active Document/Chunk, and an
    active Entity mentioned in that chunk (reused across sources via
    ``entity`` to test shared-memory preservation, SM-512 SS 47)."""

    source_id = uuid4()
    storage_dir = tmp_path / str(dataset_id) / str(source_id)
    storage_dir.mkdir(parents=True)
    storage_file = storage_dir / "source.txt"
    storage_file.write_text("hello world")

    source = Source(
        id=source_id,
        dataset_id=dataset_id,
        kind="text",
        name="source",
        mime_type="text/plain",
        content_sha256=source_id.hex + "0" * (64 - len(source_id.hex)),
        byte_size=11,
        status=SourceStatus.ACTIVE,
        storage_uri=storage_file.as_uri(),
        metadata_={},
        version=1,
    )
    document = Document(
        id=uuid4(),
        dataset_id=dataset_id,
        source_id=source_id,
        generation=0,
        title="doc",
        language="en",
        normalized_text="hello world",
        text_sha256="b" * 64,
        token_count=2,
        is_active=True,
        metadata_={},
    )
    chunk = Chunk(
        id=uuid4(),
        dataset_id=dataset_id,
        document_id=document.id,
        source_id=source_id,
        generation=0,
        ordinal=0,
        text="hello world",
        content_sha256="c" * 64,
        token_count=2,
        start_char=0,
        end_char=11,
        section_path=[],
        embedding=vector_literal(EMBEDDING_DIMENSIONS),
        lexical="hello world",
        is_active=True,
        metadata_={},
    )
    entity = entity or Entity(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=0,
        canonical_key=f"c:{uuid4()}",
        name="Entity",
        entity_type="concept",
        description="d",
        aliases=[],
        properties={},
        confidence=0.9,
        importance_weight=0.5,
        embedding=None,
        is_active=True,
    )
    mention = EntityMention(
        id=uuid4(),
        entity_id=entity.id,
        chunk_id=chunk.id,
        surface_text="hello",
        start_char=0,
        end_char=5,
        confidence=0.9,
    )

    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.sources.add(source)
        await uow.documents.add(document)
        await uow.chunks.add(chunk)
        existing_entity = await uow.entities.get_active_by_canonical_key(
            dataset_id=dataset_id, canonical_key=entity.canonical_key
        )
        if existing_entity is None:
            await uow.entities.add(entity)
        await uow.entity_mentions.add(mention)
        await uow.commit()
    return source, entity


async def get_source(
    session_factory: AsyncSessionFactory, source_id: UUID
) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        source = await uow.sources.get_by_id(source_id)
        if source is None:
            return None
        return SimpleNamespace(id=source.id, status=source.status, storage_uri=source.storage_uri)


async def get_dataset(
    session_factory: AsyncSessionFactory, dataset_id: UUID
) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        if dataset is None:
            return None
        return SimpleNamespace(id=dataset.id, status=dataset.status)


# --- SOURCE scope ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_full_forget_deletes_storage_and_content(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-source-full-{uuid4()}")
    source, _entity = await seed_source_with_content(
        session_factory, tmp_path, dataset_id=dataset.id
    )

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["source_status"] == "deleted"
        assert data["storage_deleted"] is True
        assert data["documents_deactivated"] == 1
        assert data["chunks_deactivated"] == 1

        persisted = await get_source(session_factory, source.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.DELETED
        assert persisted.storage_uri is None
        storage_dir = tmp_path / str(dataset.id) / str(source.id)
        assert not (storage_dir / "source.txt").exists()
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_memory_only_forget_preserves_storage_and_resets_placeholder(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-source-memonly-{uuid4()}")
    source, _entity = await seed_source_with_content(
        session_factory, tmp_path, dataset_id=dataset.id
    )
    storage_path = tmp_path / str(dataset.id) / str(source.id) / "source.txt"

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app,
            {
                "dataset": dataset.slug,
                "source_id": str(source.id),
                "memory_only": True,
                "wait": True,
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["source_status"] == "pending"
        assert data["storage_deleted"] is False

        persisted = await get_source(session_factory, source.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.PENDING
        assert persisted.storage_uri is not None
        assert storage_path.exists()
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_forget_preserves_entity_still_referenced_by_another_source(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-512 SS 47: shared-memory preservation regression."""

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-shared-{uuid4()}")
    source_a, entity = await seed_source_with_content(
        session_factory, tmp_path, dataset_id=dataset.id
    )
    source_b, _ = await seed_source_with_content(
        session_factory, tmp_path, dataset_id=dataset.id, entity=entity
    )

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source_a.id), "wait": True}
        )
        assert response.status_code == 200
        assert response.json()["data"]["entities_deactivated"] == 0  # B still references it

        async with PostgresUnitOfWork(session_factory) as uow:
            entities = await uow.entities.list_active_current_by_ids(
                dataset_id=dataset.id, entity_ids=[entity.id]
            )
            entity_is_active = [entity.is_active for entity in entities]
        assert len(entity_is_active) == 1
        assert entity_is_active[0] is True

        response_b = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source_b.id), "wait": True}
        )
        assert response_b.status_code == 200
        assert response_b.json()["data"]["entities_deactivated"] == 1  # now orphaned
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_forget_idempotency_key_replay_resolves_same_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-idem-{uuid4()}")
    source, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        first = await post_forget(
            app,
            {"dataset": dataset.slug, "source_id": str(source.id), "wait": True},
            idempotency_key="forget-k1",
        )
        second = await post_forget(
            app,
            {"dataset": dataset.slug, "source_id": str(source.id), "wait": False},
            idempotency_key="forget-k1",
        )
        assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    finally:
        await coordinator.stop()


# --- DATASET scope ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_full_forget_deletes_all_sources_and_reactivates_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-dataset-full-{uuid4()}")
    source_a, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)
    source_b, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(app, {"dataset": dataset.slug, "wait": True})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["sources_affected"] == 2
        assert data["sources_deleted"] == 2
        assert data["storage_deleted"] == 2

        persisted_dataset = await get_dataset(session_factory, dataset.id)
        assert persisted_dataset is not None
        assert persisted_dataset.status == DatasetStatus.ACTIVE

        for source in (source_a, source_b):
            persisted_source = await get_source(session_factory, source.id)
            assert persisted_source is not None
            assert persisted_source.status == SourceStatus.DELETED
            assert persisted_source.storage_uri is None
            assert not (tmp_path / str(dataset.id) / str(source.id) / "source.txt").exists()
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_memory_only_forget_preserves_storage_for_all_sources(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-dataset-memonly-{uuid4()}")
    source, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "memory_only": True, "wait": True}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["sources_pending"] == 1
        assert data["storage_deleted"] == 0

        persisted_dataset = await get_dataset(session_factory, dataset.id)
        assert persisted_dataset is not None and persisted_dataset.status == DatasetStatus.ACTIVE
        persisted_source = await get_source(session_factory, source.id)
        assert persisted_source is not None
        assert persisted_source.status == SourceStatus.PENDING
        assert persisted_source.storage_uri is not None
        assert (tmp_path / str(dataset.id) / str(source.id) / "source.txt").exists()
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_does_not_affect_other_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset_a = await seed_dataset(session_factory, slug=f"forget-isolated-a-{uuid4()}")
    dataset_b = await seed_dataset(session_factory, slug=f"forget-isolated-b-{uuid4()}")
    source_b, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset_b.id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(app, {"dataset": dataset_a.slug, "wait": True})
        assert response.status_code == 200

        persisted_source_b = await get_source(session_factory, source_b.id)
        assert persisted_source_b is not None
        assert persisted_source_b.status == SourceStatus.ACTIVE
    finally:
        await coordinator.stop()


# --- EVERYTHING scope --------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_everything_forget_covers_all_datasets_and_does_not_create_main(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset_a = await seed_dataset(session_factory, slug=f"forget-everything-a-{uuid4()}")
    dataset_b = await seed_dataset(session_factory, slug=f"forget-everything-b-{uuid4()}")
    source_a, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset_a.id)
    source_b, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset_b.id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"everything": True, "confirm": "DELETE EVERYTHING", "wait": True}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["datasets_affected"] >= 2
        assert data["sources_deleted"] >= 2

        for source in (source_a, source_b):
            persisted = await get_source(session_factory, source.id)
            assert persisted is not None
            assert persisted.status == SourceStatus.DELETED

        async with PostgresUnitOfWork(session_factory) as uow:
            main = await uow.datasets.get_by_slug("main")
        assert main is None  # never created by everything forget
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_everything_forget_with_zero_datasets_succeeds_with_zero_counts(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Uses a dedicated worker registry claim with no pre-existing datasets
    left over from prior tests is not guaranteed on a shared test DB, so this
    asserts the *contract* (succeeds, non-negative counts) rather than an
    exact zero -- exact-zero is exercised at the unit level instead."""

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"everything": True, "confirm": "DELETE EVERYTHING", "wait": True}
        )
        assert response.status_code == 200
        assert response.json()["data"]["datasets_affected"] >= 0
    finally:
        await coordinator.stop()


# --- B4 legacy rollout compatibility (SM-512 SS 62) --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_b4_deleting_source_with_compatible_intent_resumes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-legacy-resume-{uuid4()}")
    source, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    # Simulate a B4-era abandoned attempt: Source left DELETING, a terminal
    # FAILED PipelineRun whose persisted `input` still carries the legacy
    # `wait` key and zero PipelineSteps (B4 never created any).
    legacy_run_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        persisted_source = await uow.sources.get_by_id_for_update(source.id)
        assert persisted_source is not None
        persisted_source.status = SourceStatus.DELETING
        await uow.pipeline_runs.add(
            PipelineRun(
                id=legacy_run_id,
                pipeline_type=PipelineType.FORGET,
                dataset_id=dataset.id,
                source_id=source.id,
                status=PipelineRunStatus.FAILED,
                idempotency_key=None,
                payload_hash="b" * 64,
                input={
                    "scope": "source",
                    "dataset": dataset.slug,
                    "source_id": str(source.id),
                    "memory_only": False,
                    "wait": True,
                },
                progress=1.0,
                current_step=None,
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="a" * 64,
                error_code="RuntimeError",
                error_message="Forget failed.",
                metrics={},
            )
        )
        await uow.commit()

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["source_status"] == "deleted"

        persisted = await get_source(session_factory, source.id)
        assert persisted is not None and persisted.status == SourceStatus.DELETED
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_b4_deleting_source_with_incompatible_intent_conflicts(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-legacy-conflict-{uuid4()}")
    source, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    legacy_run_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        persisted_source = await uow.sources.get_by_id_for_update(source.id)
        assert persisted_source is not None
        persisted_source.status = SourceStatus.DELETING
        await uow.pipeline_runs.add(
            PipelineRun(
                id=legacy_run_id,
                pipeline_type=PipelineType.FORGET,
                dataset_id=dataset.id,
                source_id=source.id,
                status=PipelineRunStatus.FAILED,
                idempotency_key=None,
                payload_hash="b" * 64,
                input={
                    "scope": "source",
                    "dataset": dataset.slug,
                    "source_id": str(source.id),
                    "memory_only": True,  # incompatible with the retry below
                    "wait": True,
                },
                progress=1.0,
                current_step=None,
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="a" * 64,
                error_code="RuntimeError",
                error_message="Forget failed.",
                metrics={},
            )
        )
        await uow.commit()

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app,
            {
                "dataset": dataset.slug,
                "source_id": str(source.id),
                "memory_only": False,
                "wait": True,
            },
        )
        assert response.status_code == 409

        # Neither storage nor status should have been touched by the rejected attempt.
        persisted = await get_source(session_factory, source.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.DELETING
        assert (tmp_path / str(dataset.id) / str(source.id) / "source.txt").exists()
    finally:
        await coordinator.stop()


# --- crash / resume convergence (SM-512 SS 40) -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_crash_after_mutation_before_finalize_converges_on_resume(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Simulates a crash between the authoritative mutation commit and
    finalization by driving the steps directly (bypassing the worker/engine
    loop) up through storage deletion, then letting the real worker finish
    the run from that point -- proving ``finalize_target`` correctly
    consumes durable state left by an earlier attempt."""

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset = await seed_dataset(session_factory, slug=f"forget-crash-resume-{uuid4()}")
    source, _ = await seed_source_with_content(session_factory, tmp_path, dataset_id=dataset.id)

    resources = app.state.pipeline_resources[FORGET_RESOURCES_RESOURCE]
    context = PipelineContext(
        run_id=uuid4(),
        pipeline_type=PipelineType.FORGET,
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": dataset.slug,
            "source_id": str(source.id),
            "memory_only": False,
        },
        step_outputs={},
        session_factory=session_factory,
        resources={FORGET_RESOURCES_RESOURCE: resources},
    )

    mutation_result = type("R", (), {"output": {}})()
    async with PostgresUnitOfWork(session_factory) as uow:
        await AuthoritativeMutationStep().persist(context, mutation_result, uow)  # type: ignore[arg-type]
        await uow.commit()

    # "Crash" here: the process dies before projection/storage/finalize run.
    persisted_mid_crash = await get_source(session_factory, source.id)
    assert persisted_mid_crash is not None and persisted_mid_crash.status == SourceStatus.DELETING
    storage_path = tmp_path / str(dataset.id) / str(source.id) / "source.txt"
    assert storage_path.exists()  # not yet deleted

    # Resume: a fresh, real submission with the SAME intent should recognize
    # the DELETING target (RESUMED) and converge through drain/storage/finalize.
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "succeeded"

        persisted = await get_source(session_factory, source.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.DELETED
        assert persisted.storage_uri is None
        assert not storage_path.exists()
    finally:
        await coordinator.stop()


# --- real Neo4j (opt-in) ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_forget_removes_neo4j_projection_and_preserves_external_nodes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    require_real_neo4j()
    app, session_factory, _, neo4j_resource = build_harness(
        postgres_engine, tmp_path, with_neo4j=True
    )
    assert neo4j_resource is not None
    dataset = await seed_dataset(session_factory, slug=f"forget-neo4j-{uuid4()}")
    source, entity = await seed_source_with_content(
        session_factory, tmp_path, dataset_id=dataset.id
    )

    from sofias_memory.services.graph_rebuild_service import GraphRebuildService

    projection = Neo4jProjection(neo4j_resource)
    rebuild_service = GraphRebuildService(
        session_factory=session_factory, neo4j_resource=neo4j_resource, projection=projection
    )
    await rebuild_service.rebuild_dataset(dataset.id)

    external_node_id = f"external-{uuid4()}"
    async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
        await session.run("CREATE (n:ExternalTestNode {id: $id}) RETURN n", id=external_node_id)

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_forget(
            app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
        )
        assert response.status_code == 200
        assert response.json()["data"]["graph_events_processed"] >= 1

        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            chunk_result = await session.run(
                "MATCH (n:Chunk {dataset_id: $ds}) RETURN count(n) AS c", ds=str(dataset.id)
            )
            chunk_record = await chunk_result.single()
            assert chunk_record is not None and chunk_record["c"] == 0

            external_result = await session.run(
                "MATCH (n:ExternalTestNode {id: $id}) RETURN count(n) AS c", id=external_node_id
            )
            external_record = await external_result.single()
            assert external_record is not None and external_record["c"] == 1
    finally:
        await coordinator.stop()
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run(
                "MATCH (n:ExternalTestNode {id: $id}) DETACH DELETE n", id=external_node_id
            )
            await session.run(
                "MATCH (n) WHERE n.dataset_id = $ds DETACH DELETE n", ds=str(dataset.id)
            )
        await neo4j_resource.driver.close()


# === SM-516 staging fix Finding 2: storage-path sentinel security regression ===


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


STORAGE_PATH_SECRET_SENTINEL = "STORAGE_PATH_SECRET_SENTINEL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_deletion_failure_never_leaks_path_through_logs_or_public_response(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-516 staging fix Finding 2: a real storage-path-safety rejection
    (traversal outside the sandboxed ``data_directory``) exercised end to
    end through the real Forget pipeline/worker/HTTP surface -- not just
    ``redact_sensitive_data()`` in isolation. The rejected absolute target
    embeds a unique sentinel; it must never appear in any captured JSON log
    line, in the persisted ``PipelineStep``/``PipelineRun`` error, or in the
    public ``GET /api/v1/runs/{run_id}`` response body. Only safe values
    (exception type, stable error_code, generic message) may survive.
    """

    log_stream = StringIO()
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    clear_log_context()
    configure_logging("INFO", stream=log_stream)

    try:
        app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
        dataset = await seed_dataset(session_factory, slug=f"forget-storage-leak-{uuid4()}")
        source, _entity = await seed_source_with_content(
            session_factory, tmp_path, dataset_id=dataset.id
        )

        # A real file genuinely exists OUTSIDE the sandboxed data_directory,
        # embedding the sentinel -- proving this is a real traversal
        # rejection (SS 25 path-safety guard), not just a "file missing"
        # no-op that never reaches the unsafe branch.
        escape_dir = tmp_path.parent / STORAGE_PATH_SECRET_SENTINEL
        escape_dir.mkdir(parents=True, exist_ok=True)
        escape_target = escape_dir / "escaped_source.txt"
        escape_target.write_text("outside the sandbox")
        traversal_path = (
            tmp_path
            / str(dataset.id)
            / str(source.id)
            / ".."
            / ".."
            / ".."
            / STORAGE_PATH_SECRET_SENTINEL
            / "escaped_source.txt"
        )
        malicious_uri = traversal_path.as_uri()
        assert STORAGE_PATH_SECRET_SENTINEL in malicious_uri

        async with PostgresUnitOfWork(session_factory) as uow:
            row = await uow.sources.get_by_id_for_update(source.id)
            assert row is not None
            row.storage_uri = malicious_uri
            await uow.commit()

        coordinator = app.state.pipeline_worker
        await coordinator.start()
        try:
            response = await post_forget(
                app, {"dataset": dataset.slug, "source_id": str(source.id), "wait": True}
            )
        finally:
            await coordinator.stop()

        # wait=true on a run that reaches FAILED surfaces as a generic 500
        # (forget.py's own _failed_run_error) -- never the path.
        assert response.status_code == 500
        error_body = response.json()
        run_id = UUID(error_body["error"]["details"]["run_id"])

        async with build_client(app) as client:
            run_response = await client.get(
                f"/api/v1/runs/{run_id}", headers={API_KEY_HEADER: EXPECTED_API_KEY}
            )
        assert run_response.status_code == 200
        run_body = run_response.json()

        public_surfaces = {
            "forget_error_response": error_body,
            "run_detail_response": run_body,
        }
        for surface_name, surface in public_surfaces.items():
            serialized = json.dumps(surface)
            assert STORAGE_PATH_SECRET_SENTINEL not in serialized, (
                f"path sentinel leaked into {surface_name}: {serialized}"
            )
            assert str(escape_target) not in serialized
            assert malicious_uri not in serialized

        captured_records = read_log_records(log_stream)
        assert captured_records, "expected at least one captured log line"
        for record in captured_records:
            serialized_record = json.dumps(record)
            assert STORAGE_PATH_SECRET_SENTINEL not in serialized_record, (
                f"path sentinel leaked into a log line: {serialized_record}"
            )
            assert str(escape_target) not in serialized_record
            assert malicious_uri not in serialized_record

        # The run/step DID fail because of this rejection (not some
        # unrelated error) -- confirms the test actually exercised the
        # path-safety guard, and only ever a safe, stable error_code
        # survived publicly.
        run_data = run_body["data"]
        assert run_data["status"] == "failed"
        assert run_data["error_code"] is not None
        step_errors = [step["error"] for step in run_data["steps"] if step["error"] is not None]
        assert step_errors, "expected at least one failed step with a safe error"
    finally:
        clear_log_context()
        httpx_logger.setLevel(previous_httpx_level)
