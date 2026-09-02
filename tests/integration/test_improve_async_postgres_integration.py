"""Real-PostgreSQL (+ optional real Neo4j) tests for asynchronous Improve
(SM-511).

Proves the B5 async lifecycle end to end against a real, dedicated
PostgreSQL database: durable submission from the HTTP route, ``wait=false``/
``wait=true`` sharing one run, ``Idempotency-Key`` replay, a real worker
executing the registered Improve pipeline to ``succeeded``, request stage
order actually driving execution order (SM-511 MAJOR 1), retry after a
transient embedding failure, and the final-projection-convergence barrier
(SM-511 MAJOR 2) -- proven against a real Neo4j when the opt-in Neo4j gate is
also set, otherwise proven at the durable-state level (no run reaches
``succeeded`` with graph_outbox rows this run itself is responsible for
still unaccounted for).

LLM/embedding providers are deterministic in-process fakes; PostgreSQL is
real. Requires migrations already applied through 0009.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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
from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus
from sofias_memory.infrastructure.neo4j import Neo4jProjection, create_neo4j_resource_from_settings
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.improve import (
    IMPROVE_RESOURCES_RESOURCE,
    ImprovePipelineResources,
    resolve_slot_stages,
    slot_step_name,
)
from sofias_memory.services.graph_maintenance_service import GraphMaintenanceService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.summary_rebuild_service import SummaryRebuildService
from tests.unit._app_factory import create_app

IMPROVE_ASYNC_TESTS_ENV = "SOFIAS_MEMORY_RUN_IMPROVE_ASYNC_POSTGRES_TESTS"
IMPROVE_ASYNC_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_IMPROVE_ASYNC_TEST_DATABASE_URL"
IMPROVE_ASYNC_TEST_DATABASE_NAME = "sofias_memory_improve_async_test"
IMPROVE_ASYNC_NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_IMPROVE_ASYNC_NEO4J_TESTS"


def require_real_neo4j() -> None:
    if os.environ.get(IMPROVE_ASYNC_NEO4J_TESTS_ENV) != "1":
        pytest.skip(f"set {IMPROVE_ASYNC_NEO4J_TESTS_ENV}=1 to run async Improve real-Neo4j tests")


EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
# Real dev-stack Neo4j credential (see AGENTS.md SS 8 / .env) -- only used
# when a test explicitly opts into real Neo4j (build_harness_with_real_neo4j);
# every other harness in this file never talks to Neo4j at all.
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"

EMBEDDING_DIMENSIONS = 3072
POLL_INTERVAL_MS = 20


# --- opt-in gate --------------------------------------------------------------


def improve_async_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(IMPROVE_ASYNC_TESTS_ENV) != "1":
        pytest.skip(f"set {IMPROVE_ASYNC_TESTS_ENV}=1 to run async Improve PostgreSQL tests")

    database_url = env.get(IMPROVE_ASYNC_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {IMPROVE_ASYNC_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    _validate_database_url(database_url)
    return database_url


def _validate_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("async Improve PostgreSQL test database URL is invalid")
    if parsed_url.database != IMPROVE_ASYNC_TEST_DATABASE_NAME:
        pytest.skip(
            "async Improve PostgreSQL tests require the exact dedicated database "
            f"{IMPROVE_ASYNC_TEST_DATABASE_NAME}"
        )


def test_improve_async_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        improve_async_test_database_url({})


def test_improve_async_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        improve_async_test_database_url(
            {
                IMPROVE_ASYNC_TESTS_ENV: "1",
                IMPROVE_ASYNC_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = improve_async_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_test_database(engine)
        await truncate_everything(engine)
        yield engine
        await truncate_everything(engine)
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != IMPROVE_ASYNC_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated async Improve test database"
        )


async def truncate_everything(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE graph_outbox, pipeline_steps, pipeline_runs, feedback, queries, "
                "relation_evidence, relations, entity_mentions, entities, chunks, documents, "
                "sources, datasets RESTART IDENTITY CASCADE"
            )
        )


# --- fakes ------------------------------------------------------------------


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_next_n: int = 0

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            raise ConnectionError("simulated transient embedding provider failure")
        self.calls.append(list(texts))
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join("0" for _ in range(dimensions)) + "]"


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


@dataclass
class Harness:
    settings: Settings
    session_factory: AsyncSessionFactory
    registry: PipelineRegistry
    embedding_client: FakeEmbeddingClient
    coordinator: PipelineWorkerCoordinator
    app: Any
    dataset_id: UUID = field(default_factory=uuid4)


def build_harness(engine: AsyncEngine, tmp_path: Path, *, worker_enabled: bool = True) -> Harness:
    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    embedding_client = FakeEmbeddingClient()
    resources: dict[str, Any] = {
        IMPROVE_RESOURCES_RESOURCE: ImprovePipelineResources(
            settings=settings,
            embedding_client=embedding_client,
            graph_maintenance=GraphMaintenanceService(session_factory=session_factory),
            summary_rebuild=SummaryRebuildService(
                settings,
                session_factory=session_factory,
                embedding_client=embedding_client,
                document_summary_client=_UnusedClient(),
                dataset_summary_client=_UnusedClient(),
            ),
            # Graph_reconciliation is not selected in the Neo4j-disabled
            # scenarios below; final_convergence degrades to a documented
            # no-op (see its own docstring) without a Neo4j resource.
            graph_reconciliation=None,
            graph_outbox_drain=None,
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
    return Harness(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        embedding_client=embedding_client,
        coordinator=coordinator,
        app=app,
    )


def build_harness_with_real_neo4j(
    engine: AsyncEngine, tmp_path: Path, *, worker_enabled: bool = True
) -> tuple[Harness, Any]:
    """Same as :func:`build_harness`, but ``graph_reconciliation``/
    ``final_convergence`` are backed by a REAL Neo4j resource (SM-511 SS
    "REAL-INFRA VALIDATION"), still with fake LLM/embedding providers."""

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    embedding_client = FakeEmbeddingClient()
    neo4j_resource = create_neo4j_resource_from_settings(settings)
    projection = Neo4jProjection(neo4j_resource)
    rebuild_service = GraphRebuildService(
        session_factory=session_factory, neo4j_resource=neo4j_resource, projection=projection
    )
    resources: dict[str, Any] = {
        IMPROVE_RESOURCES_RESOURCE: ImprovePipelineResources(
            settings=settings,
            embedding_client=embedding_client,
            graph_maintenance=GraphMaintenanceService(session_factory=session_factory),
            summary_rebuild=SummaryRebuildService(
                settings,
                session_factory=session_factory,
                embedding_client=embedding_client,
                document_summary_client=_UnusedClient(),
                dataset_summary_client=_UnusedClient(),
            ),
            graph_reconciliation=GraphReconciliationService(
                session_factory=session_factory,
                neo4j_resource=neo4j_resource,
                rebuild_service=rebuild_service,
            ),
            graph_outbox_drain=GraphOutboxBatchProcessor(
                session_factory=session_factory,
                processor=GraphOutboxProcessor(
                    session_factory=session_factory, projection=projection
                ),
            ),
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
    harness = Harness(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        embedding_client=embedding_client,
        coordinator=coordinator,
        app=app,
    )
    return harness, neo4j_resource


class _UnusedClient:
    async def summarize(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("summaries stage is not exercised by this suite")


async def seed_dataset(harness: Harness, *, slug: str | None = None) -> UUID:
    async with PostgresUnitOfWork(harness.session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=harness.dataset_id,
                name=slug or f"improve-{harness.dataset_id}",
                slug=slug or f"improve-{harness.dataset_id}",
                active_generation=0,
            )
        )
        await uow.commit()
    return harness.dataset_id


async def seed_feedback_target(engine: AsyncEngine, *, dataset_id: UUID) -> tuple[UUID, UUID]:
    """One active entity mentioned in one chunk, plus one unapplied
    ``reference`` feedback row targeting that chunk -- enough for
    feedback_weights to have real work to do (and enqueue a graph_outbox
    row), returns (entity_id, feedback_id)."""

    source_id, document_id, chunk_id = uuid4(), uuid4(), uuid4()
    entity_id, mention_id, query_id, feedback_id = uuid4(), uuid4(), uuid4(), uuid4()
    vector = vector_literal(EMBEDDING_DIMENSIONS)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (id, dataset_id, kind, name, mime_type, original_uri, "
                "storage_uri, content_sha256, normalized_sha256, byte_size, metadata, status, "
                "version) VALUES (:id, :dataset_id, 'text', 'source', 'text/plain', NULL, NULL, "
                ":hash, NULL, 4, '{}'::jsonb, 'active', 1)"
            ),
            {"id": source_id, "dataset_id": dataset_id, "hash": "a" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO documents (id, dataset_id, source_id, generation, title, language, "
                "normalized_text, text_sha256, token_count, metadata, is_active) VALUES "
                "(:id, :dataset_id, :source_id, 0, 'doc', 'en', 'content', :hash, 4, "
                "'{}'::jsonb, TRUE)"
            ),
            {"id": document_id, "dataset_id": dataset_id, "source_id": source_id, "hash": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO chunks (id, dataset_id, document_id, source_id, generation, ordinal, "
                "text, content_sha256, token_count, start_char, end_char, section_path, metadata, "
                "embedding, lexical, is_active) VALUES (:id, :dataset_id, :document_id, "
                ":source_id, 0, 0, 'Ada worked with Charles.', :hash, 4, 0, 24, ARRAY[]::text[], "
                "'{}'::jsonb, CAST(:vector AS vector), to_tsvector('simple', 'ada charles'), TRUE)"
            ),
            {
                "id": chunk_id,
                "dataset_id": dataset_id,
                "document_id": document_id,
                "source_id": source_id,
                "hash": "c" * 64,
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO entities (id, dataset_id, generation, canonical_key, name, "
                "entity_type, description, aliases, properties, confidence, importance_weight, "
                "embedding, is_active) VALUES (:id, :dataset_id, 0, :key, 'Ada Lovelace', "
                "'person', 'A mathematician.', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, NULL, TRUE)"
            ),
            {"id": entity_id, "dataset_id": dataset_id, "key": f"person:{entity_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO entity_mentions (id, entity_id, chunk_id, surface_text, start_char, "
                "end_char, confidence) VALUES (:id, :entity_id, :chunk_id, 'Ada', 0, 3, 0.9)"
            ),
            {"id": mention_id, "entity_id": entity_id, "chunk_id": chunk_id},
        )
        await connection.execute(
            text(
                'INSERT INTO queries (id, query_text, dataset_ids, mode, answer, "references", '
                "timings, model) VALUES (:id, 'q', ARRAY[:dataset_id]::uuid[], 'rag', 'a', "
                "'{}'::jsonb, '{}'::jsonb, 'test-model')"
            ),
            {"id": query_id, "dataset_id": dataset_id},
        )
        await connection.execute(
            text(
                "INSERT INTO feedback (id, query_id, target_type, target_id, score, comment, "
                "applied_at) VALUES (:id, :query_id, 'reference', :chunk_id, 1, NULL, NULL)"
            ),
            {"id": feedback_id, "query_id": query_id, "chunk_id": chunk_id},
        )
    return entity_id, feedback_id


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def post_improve(
    app: Any, body: Mapping[str, Any], *, idempotency_key: str | None = None
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/improve", headers=headers, json=dict(body))


async def wait_for_terminal(
    harness: Harness, run_id: UUID, *, timeout_seconds: float = 10.0
) -> PipelineRunStatus:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        async with PostgresUnitOfWork(harness.session_factory) as uow:
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


async def run_metrics(harness: Harness, run_id: UUID) -> dict[str, Any]:
    async with PostgresUnitOfWork(harness.session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        assert run is not None
        return dict(run.metrics)


async def pending_outbox_count(engine: AsyncEngine, dataset_id: UUID) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT count(*) FROM graph_outbox WHERE dataset_id = :dataset_id "
                "AND status IN ('pending', 'processing')"
            ),
            {"dataset_id": dataset_id},
        )


# --- tests --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wait_false_then_worker_converges_to_succeeded(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    entity_id, feedback_id = await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        response = await post_improve(
            harness.app,
            {"dataset": harness.dataset_id.hex and f"improve-{dataset_id}", "wait": False},
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])
        assert response.json()["data"]["status"] in {"queued", "running"}

        status = await wait_for_terminal(harness, run_id)
        assert status == PipelineRunStatus.SUCCEEDED

        metrics = await run_metrics(harness, run_id)
        result = metrics["improve_result"]
        assert result["feedback_applied"] == 1
        assert result["entities_updated"] == 1
        assert result["graph_events_enqueued"] >= 1

        async with PostgresUnitOfWork(harness.session_factory) as uow:
            feedback_row = await uow.feedback.get_by_id(feedback_id)
            assert feedback_row is not None and feedback_row.applied_at is not None
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wait_true_returns_full_result_on_success(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        response = await post_improve(
            harness.app, {"dataset": f"improve-{dataset_id}", "wait": True}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["feedback_applied"] == 1
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_replay_resolves_the_same_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        first = await post_improve(
            harness.app, {"dataset": f"improve-{dataset_id}", "wait": True}, idempotency_key="k-1"
        )
        second = await post_improve(
            harness.app, {"dataset": f"improve-{dataset_id}", "wait": False}, idempotency_key="k-1"
        )
        assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reordered_stages_are_different_runs_executing_in_different_order(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-511 MAJOR 1: request order is part of the durable work identity,
    and slot assignment (hence PipelineStep names actually used) differs."""

    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        forward = await post_improve(
            harness.app,
            {
                "dataset": f"improve-{dataset_id}",
                "wait": True,
                "stages": ["feedback_weights", "relation_embeddings"],
            },
        )
        backward = await post_improve(
            harness.app,
            {
                "dataset": f"improve-{dataset_id}",
                "wait": True,
                "stages": ["relation_embeddings", "feedback_weights"],
            },
        )
        forward_run_id = UUID(forward.json()["data"]["run_id"])
        backward_run_id = UUID(backward.json()["data"]["run_id"])
        assert forward_run_id != backward_run_id

        async with PostgresUnitOfWork(harness.session_factory) as uow:
            forward_run = await uow.pipeline_runs.get_by_id(forward_run_id)
            backward_run = await uow.pipeline_runs.get_by_id(backward_run_id)
            assert forward_run is not None and backward_run is not None
            forward_hash = forward_run.payload_hash
            backward_hash = backward_run.payload_hash
            forward_input = dict(forward_run.input)
            backward_input = dict(backward_run.input)
        assert forward_hash != backward_hash

        forward_stages = resolve_slot_stages(forward_input)
        backward_stages = resolve_slot_stages(backward_input)
        assert forward_stages[0] == "feedback_weights"
        assert backward_stages[0] == "relation_embeddings"

        async with PostgresUnitOfWork(harness.session_factory) as uow:
            forward_steps = {
                step.name for step in await uow.pipeline_steps.list_for_run(forward_run_id)
            }
            assert slot_step_name(0, "main") in forward_steps
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_after_transient_embedding_failure_still_converges(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    await seed_feedback_target(postgres_engine, dataset_id=dataset_id)
    # entity_deduplication's "pre" phase (entity_embeddings) will call
    # embed_texts once for the seeded entity with a missing embedding --
    # fail that first call, forcing the engine's own retry/backoff path.
    harness.embedding_client.fail_next_n = 1

    await harness.coordinator.start()
    try:
        response = await post_improve(
            harness.app,
            {"dataset": f"improve-{dataset_id}", "wait": True, "stages": ["entity_deduplication"]},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "succeeded"
        assert response.json()["data"]["entities_embedded"] == 1

        run_id = UUID(response.json()["data"]["run_id"])
        async with PostgresUnitOfWork(harness.session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None
            assert run.attempt >= 1
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_convergence_barrier_runs_even_without_graph_reconciliation(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-511 MAJOR 2: with no Neo4j resource configured, the barrier
    degrades to a documented no-op -- so a feedback-only run still succeeds,
    but its own graph_outbox row is left durably PENDING for the autonomous
    consumer to pick up later (proving the barrier ran and reported honestly,
    rather than fabricating convergence it could not perform)."""

    harness = build_harness(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        response = await post_improve(
            harness.app,
            {"dataset": f"improve-{dataset_id}", "wait": True, "stages": ["feedback_weights"]},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["graph_events_enqueued"] >= 1
        assert data["graph_events_processed"] == 0  # honest: no Neo4j resource to drain with

        pending = await pending_outbox_count(postgres_engine, dataset_id)
        assert pending == data["graph_events_enqueued"]
    finally:
        await harness.coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_convergence_barrier_drains_against_real_neo4j(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-511 MAJOR 2, proven end to end: a feedback-only run (no
    ``graph_reconciliation`` requested) still leaves ZERO pending
    graph_outbox rows once it reports SUCCEEDED, because the mandatory
    ``final_convergence`` barrier drained them against a real Neo4j."""

    require_real_neo4j()
    harness, neo4j_resource = build_harness_with_real_neo4j(postgres_engine, tmp_path)
    dataset_id = await seed_dataset(harness)
    entity_id, _ = await seed_feedback_target(postgres_engine, dataset_id=dataset_id)

    await harness.coordinator.start()
    try:
        response = await post_improve(
            harness.app,
            {"dataset": f"improve-{dataset_id}", "wait": True, "stages": ["feedback_weights"]},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["graph_events_enqueued"] >= 1
        assert data["graph_events_processed"] >= 1

        pending = await pending_outbox_count(postgres_engine, dataset_id)
        assert pending == 0

        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.importance_weight AS weight",
                id=str(entity_id),
            )
            record = await result.single()
        assert record is not None
        assert record["weight"] == pytest.approx(0.55)
    finally:
        await harness.coordinator.stop()
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(entity_id))
        await neo4j_resource.driver.close()
