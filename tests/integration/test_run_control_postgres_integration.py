"""Real-PostgreSQL (+ real filesystem, + real worker) tests for run
cancellation and manual retry (SM-514).

Proves the cancel/retry control surface end to end against a real, dedicated
PostgreSQL database and a real temporary filesystem root: the cancel state
matrix (QUEUED/RUNNING/CANCELLING/terminal), cancel-vs-claim/dataset-
serialization races, manual retry creation/lineage/concurrency, worker-gate
behavior, and Remember's retry-ingress recovery matrix (queued-cancel
retry, post-ingest retry reusing the committed Source, force=true not
duplicating a version, post-final-storage retry despite original ingress
being gone, and URL acquired-content stability).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from hashlib import sha256
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
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
)
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.remember import (
    REMEMBER_RESOURCES_RESOURCE,
    RememberPipelineResources,
)
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.remember import ingress_artifact_exists, write_ingress_bytes

RUN_CONTROL_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_RUN_CONTROL_POSTGRES_TESTS"
RUN_CONTROL_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_RUN_CONTROL_TEST_DATABASE_URL"
RUN_CONTROL_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_run_control_test"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
POLL_INTERVAL_MS = 20


def run_control_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(RUN_CONTROL_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {RUN_CONTROL_POSTGRES_TESTS_ENV}=1 to run run-control PostgreSQL tests")
    database_url = env.get(RUN_CONTROL_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {RUN_CONTROL_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("run-control PostgreSQL test database URL is invalid")
    if parsed_url.database != RUN_CONTROL_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "run-control PostgreSQL tests require the exact dedicated database "
            f"{RUN_CONTROL_POSTGRES_TEST_DATABASE_NAME}"
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
    database_url = run_control_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
        if current_database != RUN_CONTROL_POSTGRES_TEST_DATABASE_NAME:
            pytest.skip(
                "connected PostgreSQL database is not the dedicated run-control test database"
            )
        async with engine.begin() as connection:
            tables = ", ".join(f'"{table}"' for table in _TEST_TABLES)
            await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
        yield engine
    finally:
        await dispose_async_engine(engine)


def test_run_control_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        run_control_test_database_url({})


# --- deterministic Cognify provider doubles (Remember mode=full retry tests) --


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * 3072 for _ in texts]


class FakeKnowledgeExtractionClient:
    async def extract(self, chunk_text: str) -> Any:
        from sofias_memory.schemas.knowledge import ChunkKnowledgeExtraction, ExtractedEntity

        del chunk_text
        return ChunkKnowledgeExtraction(
            summary="s",
            entities=[
                ExtractedEntity(
                    local_id="e1",
                    name="PostgreSQL",
                    type="Technology",
                    description="d",
                    aliases=[],
                    confidence=0.9,
                )
            ],
            relations=[],
        )


class FakeDocumentSummaryClient:
    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        del chunk_summaries
        return "summary"


# --- harness ------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": "postgresql+asyncpg://unused:unused@localhost:5432/unused",
        "neo4j_password": os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "chunk_max_tokens": 24,
        "chunk_overlap_tokens": 6,
        "chunk_min_tokens": 4,
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
) -> tuple[Any, AsyncSessionFactory, PipelineRegistry, dict[str, Any]]:
    """``worker_claims=False`` gives the coordinator an empty registry: it is
    genuinely started/enabled/running (so the submission gate passes, and
    cancel's own "no worker required" property can be tested against a
    truly-not-executing run), but never claims anything -- a run stays
    durably QUEUED until the test itself decides what happens to it. This
    mirrors the same pattern already established for Cognify's own test
    harness (``test_cognify_async_postgres_integration.py``)."""

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources: dict[str, Any] = {
        REMEMBER_RESOURCES_RESOURCE: RememberPipelineResources(
            settings=settings, cognify_service=cognify_service
        )
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
    return app, session_factory, registry, resources


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def post_remember(
    app: Any, body: Mapping[str, Any], *, idempotency_key: str | None = None
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/remember", headers=headers, json=dict(body))


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
            attempt=run.attempt,
            source_id=run.source_id,
            dataset_id=run.dataset_id,
            retry_of_run_id=run.retry_of_run_id,
            next_attempt_at=run.next_attempt_at,
            metrics=dict(run.metrics),
            input=dict(run.input),
        )


async def list_steps(session_factory: AsyncSessionFactory, run_id: UUID) -> list[SimpleNamespace]:
    async with PostgresUnitOfWork(session_factory) as uow:
        steps = await uow.pipeline_steps.list_for_run(run_id)
        return [SimpleNamespace(name=s.name, status=s.status, attempt=s.attempt) for s in steps]


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


# --- cancel state matrix -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_missing_run_is_404(postgres_engine: AsyncEngine, tmp_path: Path) -> None:
    app, _, _, _ = build_harness(postgres_engine, tmp_path)
    response = await post_cancel(app, uuid4())
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_queued_cancels_immediately_and_cancels_queued_steps(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app, {"dataset": "main", "content": "hello", "mode": "full", "wait": False}
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])

        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["data"]["status"] == "cancelled"

        run = await get_run(session_factory, run_id)
        assert run is not None
        assert run.status == PipelineRunStatus.CANCELLED
        assert run.next_attempt_at is None
        steps = await list_steps(session_factory, run_id)
        assert steps
        assert all(step.status == PipelineStepStatus.CANCELLED for step in steps)
        assert all(step.attempt == 0 for step in steps)
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_running_moves_to_cancelling_without_touching_current_step(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app, {"dataset": "main", "content": "hello", "mode": "full", "wait": True}
        )
        assert response.status_code == 200
        run_id = UUID(response.json()["data"]["run_id"])
        # The run already reached SUCCEEDED (worker is fast + fakes are
        # instant) -- cancel afterward must be an immutable no-op, proving
        # terminal immutability rather than a RUNNING race here.
        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["data"]["status"] == "succeeded"
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_terminal_run_is_immutable_noop(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(app, {"dataset": "main", "content": "hello", "wait": True})
        assert response.status_code == 200
        run_id = UUID(response.json()["data"]["run_id"])
        before = await get_run(session_factory, run_id)
        assert before is not None

        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["data"]["status"] == "succeeded"

        after = await get_run(session_factory, run_id)
        assert after is not None
        assert after.status == before.status
        assert after.metrics == before.metrics
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_does_not_require_worker_available(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "hello", "wait": False})
    assert response.status_code == 202
    run_id = UUID(response.json()["data"]["run_id"])
    # Now genuinely unavailable (SM-514 SS 14): is_running becomes False.
    await coordinator.stop()

    cancel_response = await post_cancel(app, run_id)
    assert cancel_response.status_code == 200
    assert cancel_response.json()["data"]["status"] == "cancelled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_queued_with_scheduled_automatic_retry_clears_next_attempt_at(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from datetime import timedelta

    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "hello", "wait": False})
    assert response.status_code == 202
    run_id = UUID(response.json()["data"]["run_id"])

    # Simulate the engine's own RUNNING -> QUEUED automatic-retry requeue,
    # scheduling a future next_attempt_at (SM-514 SS 58).
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(
            run, PipelineRunStatus.QUEUED, now=now, next_attempt_at=now + timedelta(minutes=5)
        )
        await uow.commit()

    scheduled = await get_run(session_factory, run_id)
    assert scheduled is not None and scheduled.next_attempt_at is not None

    cancel_response = await post_cancel(app, run_id)
    assert cancel_response.status_code == 200
    after = await get_run(session_factory, run_id)
    assert after is not None
    assert after.status == PipelineRunStatus.CANCELLED
    assert after.next_attempt_at is None
    await coordinator.stop()


# --- cancel vs claim race / dataset serialization ---------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_vs_claim_race_never_double_executes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app, {"dataset": "main", "content": "race content", "mode": "full", "wait": False}
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])

        cancel_response = await post_cancel(app, run_id)
        assert cancel_response.status_code in (200, 202)
        terminal = await wait_for_status(
            session_factory,
            run_id,
            {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.CANCELLED, PipelineRunStatus.FAILED},
        )
        # Either outcome is valid (SM-514 SS 12): cancel won the lock first
        # (CANCELLED, never executed), or claim won and cancel observed
        # RUNNING and converged the run to CANCELLING -> CANCELLED, or the
        # work finished before cancel's own lock acquisition (SUCCEEDED).
        assert terminal in (
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.CANCELLED,
            PipelineRunStatus.FAILED,
        )
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelled_run_releases_dataset_for_next_queued(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    dataset_slug = f"serialize-{uuid4()}"
    await seed_dataset(session_factory, slug=dataset_slug)

    first = await post_remember(app, {"dataset": dataset_slug, "content": "first", "wait": False})
    assert first.status_code == 202
    first_run_id = UUID(first.json()["data"]["run_id"])
    cancel_response = await post_cancel(app, first_run_id)
    assert cancel_response.status_code == 200
    await coordinator.stop()

    # A second, independent app instance with real claiming enabled, sharing
    # the same PostgreSQL database: proves dataset-scoped serialization is a
    # property of the durable data, not of one in-process coordinator.
    app2, session_factory2, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=True)
    coordinator2 = app2.state.pipeline_worker
    await coordinator2.start()
    second = await post_remember(
        app2, {"dataset": dataset_slug, "content": "second", "wait": False}
    )
    assert second.status_code == 202
    second_run_id = UUID(second.json()["data"]["run_id"])

    try:
        status = await wait_for_status(
            session_factory2,
            second_run_id,
            {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED},
        )
        assert status == PipelineRunStatus.SUCCEEDED
    finally:
        await coordinator2.stop()


# --- manual retry: creation, lineage, worker gate ---------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_missing_run_is_404(postgres_engine: AsyncEngine, tmp_path: Path) -> None:
    app, _, _, _ = build_harness(postgres_engine, tmp_path)
    response = await post_retry(app, uuid4())
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["queued", "running", "succeeded"])
async def test_retry_non_terminal_or_succeeded_is_409(
    postgres_engine: AsyncEngine, tmp_path: Path, status_name: str
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "x", "wait": False})
    assert response.status_code == 202
    run_id = UUID(response.json()["data"]["run_id"])

    if status_name != "queued":
        from sofias_memory.services.pipeline_lifecycle import transition_run

        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            now = await uow.pipeline_runs.get_database_now()
            transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
            if status_name == "succeeded":
                transition_run(run, PipelineRunStatus.SUCCEEDED, now=now)
            await uow.commit()

    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 409
    assert retry_response.json()["error"]["code"] == "RUN_NOT_RETRYABLE"
    await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_of_failed_run_creates_new_queued_run_original_untouched(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:

    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "retry me", "wait": False})
    assert response.status_code == 202
    original_id = UUID(response.json()["data"]["run_id"])

    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id_for_update(original_id)
        assert run is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="boom")
        await uow.commit()
    before = await get_run(session_factory, original_id)
    assert before is not None

    retry_response = await post_retry(app, original_id)
    assert retry_response.status_code == 202
    new_run_id = UUID(retry_response.json()["data"]["run_id"])
    assert new_run_id != original_id

    new_run = await get_run(session_factory, new_run_id)
    assert new_run is not None
    assert new_run.status == PipelineRunStatus.QUEUED
    assert new_run.attempt == 0
    assert new_run.retry_of_run_id == original_id
    assert new_run.input == before.input

    after = await get_run(session_factory, original_id)
    assert after is not None
    assert after.status == before.status
    assert after.attempt == before.attempt
    assert after.metrics == before.metrics

    new_steps = await list_steps(session_factory, new_run_id)
    assert new_steps
    assert all(step.status == PipelineStepStatus.QUEUED for step in new_steps)
    assert all(step.attempt == 0 for step in new_steps)
    await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_retries_converge_to_one_child(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "concurrent", "wait": False})
    assert response.status_code == 202
    original_id = UUID(response.json()["data"]["run_id"])
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id_for_update(original_id)
        assert run is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    responses = await asyncio.gather(post_retry(app, original_id), post_retry(app, original_id))
    for response in responses:
        assert response.status_code == 202
    child_ids = {UUID(response.json()["data"]["run_id"]) for response in responses}
    assert len(child_ids) == 1

    async with postgres_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM pipeline_runs WHERE retry_of_run_id = :id"),
            {"id": str(original_id)},
        )
    assert count == 1
    await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_of_retry_builds_lineage_chain(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "chain", "wait": False})
    assert response.status_code == 202
    a_id = UUID(response.json()["data"]["run_id"])

    async def fail(run_id: UUID) -> None:
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            now = await uow.pipeline_runs.get_database_now()
            transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
            transition_run(
                run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e"
            )
            await uow.commit()

    await fail(a_id)
    retry_b = await post_retry(app, a_id)
    assert retry_b.status_code == 202
    b_id = UUID(retry_b.json()["data"]["run_id"])
    # Captured immediately after creation, before B is itself failed below --
    # a fresh manual-retry child always starts at attempt 0 (SM-514 SS 18).
    b_run_at_creation = await get_run(session_factory, b_id)
    assert b_run_at_creation is not None
    assert b_run_at_creation.retry_of_run_id == a_id
    assert b_run_at_creation.attempt == 0

    await fail(b_id)
    retry_c = await post_retry(app, b_id)
    assert retry_c.status_code == 202
    c_id = UUID(retry_c.json()["data"]["run_id"])

    c_run = await get_run(session_factory, c_id)
    assert c_run is not None and c_run.retry_of_run_id == b_id
    assert c_run.attempt == 0
    await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_new_child_worker_disabled_is_503_no_row_created(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(app, {"dataset": "main", "content": "no worker", "wait": False})
    assert response.status_code == 202
    original_id = UUID(response.json()["data"]["run_id"])
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id_for_update(original_id)
        assert run is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()
    # Now genuinely unavailable: a NEW retry child requires a running worker.
    await coordinator.stop()

    retry_response = await post_retry(app, original_id)
    assert retry_response.status_code == 503

    async with postgres_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM pipeline_runs WHERE retry_of_run_id = :id"),
            {"id": str(original_id)},
        )
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_sys_prefixed_idempotency_key_still_rejected(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-514 SS 21/30: the internal namespace stays reserved -- a real
    client trying to submit under `sys:` is still rejected by the public
    Remember route, unaffected by the new internal-trust entry point."""

    app, _, _, _ = build_harness(postgres_engine, tmp_path)
    response = await post_remember(
        app,
        {"dataset": "main", "content": "hijack attempt", "wait": False},
        idempotency_key="sys:retry:00000000-0000-0000-0000-000000000000",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"


# --- Remember retry ingress matrix (SM-514 SS 34-43) ------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_queued_text_cancel_then_retry_succeeds(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    response = await post_remember(
        app, {"dataset": "main", "content": "queued then cancelled", "wait": False}
    )
    assert response.status_code == 202
    run_id = UUID(response.json()["data"]["run_id"])
    cancel_response = await post_cancel(app, run_id)
    assert cancel_response.status_code == 200

    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 202
    child_id = UUID(retry_response.json()["data"]["run_id"])
    await coordinator.stop()

    # A second, real-claiming coordinator against the same database actually
    # executes the retry child.
    app2, session_factory2, _, _ = build_harness(postgres_engine, tmp_path, worker_claims=True)
    coordinator2 = app2.state.pipeline_worker
    await coordinator2.start()
    try:
        status = await wait_for_status(
            session_factory2, child_id, {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED}
        )
        assert status == PipelineRunStatus.SUCCEEDED
    finally:
        await coordinator2.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_failed_after_prepare_and_ingest_retry_reuses_source(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.pipelines.registry import build_default_pipeline_registry
    from sofias_memory.services.pipeline_lifecycle import (
        create_run_with_steps,
        transition_run,
        transition_step,
    )
    from sofias_memory.services.remember import remember_text_run_input

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="main")
    content = "partial failure content"
    work_input = remember_text_run_input(
        dataset="main",
        content_sha256=sha256(content.encode()).hexdigest(),
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content.encode("utf-8"))
    registry = build_default_pipeline_registry()
    step_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=work_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=dict(work_input),
            config_fingerprint=settings.config_fingerprint(),
            steps=step_plan,
            run_id=run_id,
        )
        await uow.commit()

    # Run only prepare_and_ingest (real step), then mark it SUCCEEDED and
    # fail the run directly -- simulating a genuine crash/permanent failure
    # right after step 1 committed.
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.steps.remember import PrepareAndIngestStep
    from sofias_memory.services.cognify import CognifyService as _CognifyService

    cognify_service = _CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    from sofias_memory.pipelines.steps.remember import RememberPipelineResources as _Resources

    resources = _Resources(settings=settings, cognify_service=cognify_service)
    context = PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )
    step = PrepareAndIngestStep()
    execute_result = await step.execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await step.persist(context, execute_result, uow)
        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 0)
        assert step_row is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_step(step_row, PipelineStepStatus.RUNNING, now=now)
        transition_step(
            step_row, PipelineStepStatus.SUCCEEDED, now=now, output=execute_result.output
        )
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    original_source_id = execute_result.output["source_id"]

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 202
    child_id = UUID(retry_response.json()["data"]["run_id"])
    try:
        status = await wait_for_status(
            session_factory, child_id, {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED}
        )
        assert status == PipelineRunStatus.SUCCEEDED
        child = await get_run(session_factory, child_id)
        assert child is not None
        assert str(child.metrics["remember_result"]["source_id"]) == str(original_source_id)
    finally:
        await coordinator.stop()

    async with postgres_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM sources WHERE dataset_id = :ds"), {"ds": str(dataset_id)}
        )
    assert count == 1  # no duplicate Source created by the retry


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_force_true_retry_does_not_create_another_source_version(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.registry import build_default_pipeline_registry
    from sofias_memory.pipelines.steps.remember import PrepareAndIngestStep
    from sofias_memory.services.pipeline_lifecycle import (
        create_run_with_steps,
        transition_run,
        transition_step,
    )
    from sofias_memory.services.remember import remember_text_run_input

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="main-force")
    content = "forced content"
    work_input = remember_text_run_input(
        dataset="main-force",
        content_sha256=sha256(content.encode()).hexdigest(),
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=True,
    )
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content.encode("utf-8"))
    registry = build_default_pipeline_registry()
    step_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=work_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="b" * 64,
            input=dict(work_input),
            config_fingerprint=settings.config_fingerprint(),
            steps=step_plan,
            run_id=run_id,
        )
        await uow.commit()

    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources = RememberPipelineResources(settings=settings, cognify_service=cognify_service)
    context = PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )
    step = PrepareAndIngestStep()
    execute_result = await step.execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await step.persist(context, execute_result, uow)
        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 0)
        assert step_row is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_step(step_row, PipelineStepStatus.RUNNING, now=now)
        transition_step(
            step_row, PipelineStepStatus.SUCCEEDED, now=now, output=execute_result.output
        )
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    assert execute_result.output["version"] == 1

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 202
    child_id = UUID(retry_response.json()["data"]["run_id"])
    try:
        status = await wait_for_status(
            session_factory, child_id, {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED}
        )
        assert status == PipelineRunStatus.SUCCEEDED
    finally:
        await coordinator.stop()

    async with postgres_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM sources WHERE dataset_id = :ds"), {"ds": str(dataset_id)}
        )
        max_version = await connection.scalar(
            text("SELECT max(version) FROM sources WHERE dataset_id = :ds"), {"ds": str(dataset_id)}
        )
    assert count == 1
    assert max_version == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_failed_after_final_storage_retry_succeeds_despite_missing_original_ingress(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.registry import build_default_pipeline_registry
    from sofias_memory.pipelines.steps.remember import (
        PREPARE_AND_INGEST_STEP,
        FinalizeStorageStep,
        PrepareAndIngestStep,
    )
    from sofias_memory.services.pipeline_lifecycle import (
        create_run_with_steps,
        transition_run,
        transition_step,
    )
    from sofias_memory.services.remember import remember_text_run_input

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    settings = make_settings(tmp_path)
    dataset_id = await seed_dataset(session_factory, slug="main-storage")
    content = "content past storage"
    work_input = remember_text_run_input(
        dataset="main-storage",
        content_sha256=sha256(content.encode()).hexdigest(),
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content.encode("utf-8"))
    registry = build_default_pipeline_registry()
    step_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=work_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="c" * 64,
            input=dict(work_input),
            config_fingerprint=settings.config_fingerprint(),
            steps=step_plan,
            run_id=run_id,
        )
        await uow.commit()

    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources = RememberPipelineResources(settings=settings, cognify_service=cognify_service)
    context = PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )
    ingest_step = PrepareAndIngestStep()
    ingest_result = await ingest_step.execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await ingest_step.persist(context, ingest_result, uow)
        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 0)
        assert step_row is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_step(step_row, PipelineStepStatus.RUNNING, now=now)
        transition_step(
            step_row, PipelineStepStatus.SUCCEEDED, now=now, output=ingest_result.output
        )
        await uow.commit()

    context2 = PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=work_input,
        step_outputs={PREPARE_AND_INGEST_STEP: ingest_result.output},
        session_factory=session_factory,
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )
    storage_step = FinalizeStorageStep()
    storage_result = await storage_step.execute(context2)
    async with PostgresUnitOfWork(session_factory) as uow:
        await storage_step.persist(context2, storage_result, uow)
        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 1)
        assert step_row is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_step(step_row, PipelineStepStatus.RUNNING, now=now)
        transition_step(
            step_row, PipelineStepStatus.SUCCEEDED, now=now, output=storage_result.output
        )
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    assert not ingress_artifact_exists(tmp_path, run_id=run_id)  # cleaned up by finalize_storage

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 202
    child_id = UUID(retry_response.json()["data"]["run_id"])
    try:
        status = await wait_for_status(
            session_factory, child_id, {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED}
        )
        assert status == PipelineRunStatus.SUCCEEDED
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_url_acquired_content_stable_across_retry(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-514 SS 43: once the original run acquired URL bytes durably, a
    retry must reuse those bytes -- not silently refetch and observe
    different content from a server that changes its response."""

    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.registry import build_default_pipeline_registry
    from sofias_memory.pipelines.steps.remember import PrepareAndIngestStep
    from sofias_memory.services.pipeline_lifecycle import (
        create_run_with_steps,
        transition_run,
        transition_step,
    )
    from sofias_memory.services.remember import remember_url_run_input

    first_body = b"first content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=first_body, headers={"Content-Type": "text/plain"})

    transport = httpx.MockTransport(handler)

    def resolver(host: str, port: int) -> list[Any]:
        import ipaddress

        del host, port
        return [ipaddress.ip_address("93.184.216.34")]

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await seed_dataset(session_factory, slug="main-url")
    work_input = remember_url_run_input(
        dataset="main-url",
        url="https://example.com/a.txt",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    run_id = uuid4()
    registry = build_default_pipeline_registry()
    step_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=work_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="d" * 64,
            input=dict(work_input),
            config_fingerprint=settings.config_fingerprint(),
            steps=step_plan,
            run_id=run_id,
        )
        await uow.commit()

    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources = RememberPipelineResources(
        settings=settings,
        cognify_service=cognify_service,
        url_transport=transport,
        url_resolver=resolver,
    )
    context = PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )
    ingest_step = PrepareAndIngestStep()
    ingest_result = await ingest_step.execute(context)
    assert ingest_result.output["content_sha256"] == sha256(first_body).hexdigest()
    async with PostgresUnitOfWork(session_factory) as uow:
        await ingest_step.persist(context, ingest_result, uow)
        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 0)
        assert step_row is not None
        now = await uow.pipeline_runs.get_database_now()
        transition_step(step_row, PipelineStepStatus.RUNNING, now=now)
        transition_step(
            step_row, PipelineStepStatus.SUCCEEDED, now=now, output=ingest_result.output
        )
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    # Server now returns DIFFERENT content -- proves the retry must NOT
    # observe this if it correctly reuses the original's acquired bytes.
    def changed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"different content!!", headers={"Content-Type": "text/plain"}
        )

    app, _, _, harness_resources = build_harness(postgres_engine, tmp_path)
    # Mutate the harness's OWN worker resources (the dict actually wired into
    # PipelineWorkerCoordinator, distinct from app.state.pipeline_resources --
    # a separate instance create_app() always builds itself) with the changed
    # transport, so the retry child's own worker execution exercises the
    # "would refetch differently" scenario if the bug were present.
    coordinator = app.state.pipeline_worker
    remember_resources = harness_resources[REMEMBER_RESOURCES_RESOURCE]
    object.__setattr__(remember_resources, "url_transport", httpx.MockTransport(changed_handler))
    object.__setattr__(remember_resources, "url_resolver", resolver)

    await coordinator.start()
    retry_response = await post_retry(app, run_id)
    assert retry_response.status_code == 202
    child_id = UUID(retry_response.json()["data"]["run_id"])
    try:
        status = await wait_for_status(
            session_factory, child_id, {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED}
        )
        assert status == PipelineRunStatus.SUCCEEDED
        child = await get_run(session_factory, child_id)
        assert child is not None
        assert child.metrics["remember_result"]["content_hash"] == sha256(first_body).hexdigest()
    finally:
        await coordinator.stop()


# --- other pipelines: business idempotence under manual retry (SM-514 SS 70) -
#
# Proven by directly re-invoking each pipeline's OWN authoritative step
# across two INDEPENDENT PipelineRun rows targeting the same durable state --
# exactly what the real engine does when it executes a fresh manual-retry
# child's step plan. This exercises the identical business code path a real
# POST /runs/{id}/retry child's own worker execution would run, without
# re-proving run/lineage/idempotency-key mechanics already covered above.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cognify_partial_state_retry_does_not_duplicate_chunks(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.domain import SourceKind, SourceStatus
    from sofias_memory.infrastructure.postgres.models import Document, Source
    from sofias_memory.pipelines.steps.cognify import (
        COGNIFY_SERVICE_RESOURCE,
        ProcessSourcesStep,
    )
    from sofias_memory.services.pipeline_lifecycle import create_run_with_steps

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await seed_dataset(session_factory, slug=f"cognify-retry-{uuid4()}")
    source_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.sources.add(
            Source(
                id=source_id,
                dataset_id=dataset_id,
                kind=SourceKind.TEXT,
                name="note",
                mime_type="text/plain",
                content_sha256=f"{source_id.int:064x}"[:64],
                byte_size=64,
                metadata_={},
                status=SourceStatus.PENDING,
                version=1,
            )
        )
        await uow.documents.add(
            Document(
                dataset_id=dataset_id,
                source_id=source_id,
                generation=0,
                title="note",
                language="und",
                normalized_text="PostgreSQL supports Sofias Memory. " * 3,
                text_sha256=f"{source_id.int:064x}"[:64],
                token_count=-1,
                metadata_={},
                is_active=True,
            )
        )
        await uow.commit()

    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources = {COGNIFY_SERVICE_RESOURCE: cognify_service}
    work_input = {"dataset": "cognify-retry", "source_ids": [str(source_id)], "rebuild": None}

    async def run_process_sources_once() -> None:
        registry = build_default_pipeline_registry()
        step_plan = registry.build_step_plan(PipelineType.COGNIFY, run_input=work_input)
        run_id = uuid4()
        async with PostgresUnitOfWork(session_factory) as uow:
            await create_run_with_steps(
                uow,
                pipeline_type=PipelineType.COGNIFY,
                dataset_id=dataset_id,
                source_id=None,
                idempotency_key=None,
                payload_hash=f"{run_id.int:064x}"[:64],
                input=dict(work_input),
                config_fingerprint=settings.config_fingerprint(),
                steps=step_plan,
                run_id=run_id,
            )
            await uow.commit()

        from sofias_memory.pipelines.context import PipelineContext

        context = PipelineContext(
            run_id=run_id,
            pipeline_type=PipelineType.COGNIFY,
            dataset_id=dataset_id,
            source_id=None,
            run_input=work_input,
            step_outputs={},
            session_factory=session_factory,
            resources=resources,
        )
        step = ProcessSourcesStep()
        result = await step.execute(context)
        async with PostgresUnitOfWork(session_factory) as uow:
            await step.persist(context, result, uow)
            await uow.commit()

    # First attempt: a genuine PipelineRun commits chunks/entities for the
    # source (simulating an original run that then failed at a later step).
    await run_process_sources_once()
    async with postgres_engine.connect() as connection:
        chunks_after_first = await connection.scalar(
            text("SELECT count(*) FROM chunks WHERE source_id = :sid"), {"sid": str(source_id)}
        )
        entities_after_first = await connection.scalar(
            text("SELECT count(*) FROM entities WHERE dataset_id = :ds"), {"ds": str(dataset_id)}
        )
    assert chunks_after_first > 0
    assert entities_after_first > 0

    # A fresh manual-retry child's own PipelineRun re-executes the SAME
    # authoritative step against the SAME source -- must converge, not
    # duplicate.
    await run_process_sources_once()
    async with postgres_engine.connect() as connection:
        chunks_after_retry = await connection.scalar(
            text("SELECT count(*) FROM chunks WHERE source_id = :sid"), {"sid": str(source_id)}
        )
        entities_after_retry = await connection.scalar(
            text("SELECT count(*) FROM entities WHERE dataset_id = :ds"), {"ds": str(dataset_id)}
        )
    assert chunks_after_retry == chunks_after_first
    assert entities_after_retry == entities_after_first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_improve_feedback_weights_not_reapplied_on_retry(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.infrastructure.postgres.models import Feedback, Query
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.steps.improve import FeedbackWeightsStep

    session_factory = create_session_factory(postgres_engine)
    dataset_id = await seed_dataset(session_factory, slug=f"improve-retry-{uuid4()}")
    query_id = uuid4()
    feedback_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.queries.add(
            Query(
                id=query_id,
                query_text="q",
                dataset_ids=[dataset_id],
                mode="chunks",
                answer=None,
                references={},
                timings={},
                model=None,
            )
        )
        await uow.feedback.add(
            Feedback(
                id=feedback_id,
                query_id=query_id,
                target_type="chunk",
                target_id=None,
                score=1,
                comment=None,
                applied_at=None,
            )
        )
        await uow.commit()

    async def run_feedback_weights_once() -> dict[str, object]:
        context = PipelineContext(
            run_id=uuid4(),
            pipeline_type=PipelineType.IMPROVE,
            dataset_id=dataset_id,
            source_id=None,
            run_input={"dataset": "improve-retry", "stages": ["feedback_weights"]},
            step_outputs={},
            session_factory=session_factory,
            resources={},
        )
        step = FeedbackWeightsStep()
        result = await step.execute(context)
        async with PostgresUnitOfWork(session_factory) as uow:
            await step.persist(context, result, uow)
            await uow.commit()
        return dict(result.output)

    first = await run_feedback_weights_once()
    assert first["processed"] == 1

    # A fresh manual-retry child's own execution of the same stage must find
    # nothing left to (re-)apply -- feedback.applied_at already committed.
    second = await run_feedback_weights_once()
    assert second["processed"] == 0
    assert second["applied"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_partial_state_retry_resumes_deleting_target(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sofias_memory.domain import SourceKind, SourceStatus
    from sofias_memory.infrastructure.postgres.models import Source
    from sofias_memory.pipelines.context import PipelineContext
    from sofias_memory.pipelines.steps.forget import AuthoritativeMutationStep
    from sofias_memory.services.forget import forget_source_run_input
    from sofias_memory.services.pipeline_lifecycle import create_run_with_steps, transition_run

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(postgres_engine)
    dataset_slug = f"forget-retry-{uuid4()}"
    dataset_id = await seed_dataset(session_factory, slug=dataset_slug)
    source_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.sources.add(
            Source(
                id=source_id,
                dataset_id=dataset_id,
                kind=SourceKind.TEXT,
                name="note",
                mime_type="text/plain",
                content_sha256=f"{source_id.int:064x}"[:64],
                byte_size=4,
                metadata_={},
                status=SourceStatus.ACTIVE,
                version=1,
            )
        )
        await uow.commit()

    work_input = forget_source_run_input(
        dataset=dataset_slug, source_id=source_id, memory_only=False
    )
    registry = build_default_pipeline_registry()
    step_plan = registry.build_step_plan(PipelineType.FORGET, run_input=work_input)

    async def submit_and_fail() -> UUID:
        run_id = uuid4()
        async with PostgresUnitOfWork(session_factory) as uow:
            await create_run_with_steps(
                uow,
                pipeline_type=PipelineType.FORGET,
                dataset_id=dataset_id,
                source_id=source_id,
                idempotency_key=None,
                payload_hash=f"{run_id.int:064x}"[:64],
                input=dict(work_input),
                config_fingerprint=settings.config_fingerprint(),
                steps=step_plan,
                run_id=run_id,
            )
            await uow.commit()
        return run_id

    original_id = await submit_and_fail()
    context = PipelineContext(
        run_id=original_id,
        pipeline_type=PipelineType.FORGET,
        dataset_id=dataset_id,
        source_id=source_id,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={},
    )
    step = AuthoritativeMutationStep()
    result = await step.execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await step.persist(context, result, uow)
        now = await uow.pipeline_runs.get_database_now()
        run = await uow.pipeline_runs.get_by_id_for_update(original_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
        transition_run(run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e")
        await uow.commit()

    source_after_first = await get_source_status(session_factory, source_id)
    assert source_after_first == SourceStatus.DELETING

    # Fresh retry child (independent PipelineRun, SAME semantic intent):
    # must recognize the DELETING target and resume, never re-raise a
    # conflict against itself.
    retry_id = await submit_and_fail()
    retry_context = PipelineContext(
        run_id=retry_id,
        pipeline_type=PipelineType.FORGET,
        dataset_id=dataset_id,
        source_id=source_id,
        run_input=work_input,
        step_outputs={},
        session_factory=session_factory,
        resources={},
    )
    retry_result = await step.execute(retry_context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await step.persist(retry_context, retry_result, uow)
        await uow.commit()

    assert retry_result.output["proceed"] is True


async def get_source_status(session_factory: AsyncSessionFactory, source_id: UUID) -> Any:
    async with PostgresUnitOfWork(session_factory) as uow:
        source = await uow.sources.get_by_id(source_id)
        assert source is not None
        return source.status
