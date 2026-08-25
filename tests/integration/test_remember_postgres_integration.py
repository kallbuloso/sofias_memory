"""Real-PostgreSQL (+ real filesystem, + optional real Neo4j) tests for
asynchronous Remember (SM-513).

Proves the B5 async lifecycle end to end against a real, dedicated
PostgreSQL database and a real temporary filesystem root: durable
submission, wait=false/wait=true, durable ingress staging/cleanup, a real
worker executing the four registered Remember steps to ``succeeded`` for
TEXT/FILE/URL, mode=ingest vs mode=full (zero nested COGNIFY run), dedup/
force/version semantics, concurrent first-``main``-dataset creation,
B4-legacy Idempotency-Key compatibility, the new partial unique operational-
run constraint, and crash/resume convergence. Real Neo4j coverage (full-mode
projection convergence) is opt-in via its own gate.
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
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    PipelineRunStatus,
    PipelineType,
    SourceStatus,
)
from sofias_memory.infrastructure.neo4j import Neo4jProjection, create_neo4j_resource_from_settings
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.registry import (
    PipelineRegistry,
    build_default_pipeline_registry,
)
from sofias_memory.pipelines.steps.remember import (
    REMEMBER_RESOURCES_RESOURCE,
    PrepareAndIngestStep,
    RememberPipelineResources,
)
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.remember import (
    REMEMBER_RESULT_METRIC_KEY,
    remember_text_run_input,
    write_ingress_bytes,
)

REMEMBER_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_REMEMBER_POSTGRES_TESTS"
REMEMBER_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_REMEMBER_TEST_DATABASE_URL"
REMEMBER_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_remember_test"
REMEMBER_NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_REMEMBER_NEO4J_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
POLL_INTERVAL_MS = 20


def remember_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(REMEMBER_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {REMEMBER_POSTGRES_TESTS_ENV}=1 to run remember PostgreSQL tests")
    database_url = env.get(REMEMBER_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {REMEMBER_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    _validate_remember_test_database_url(database_url)
    return database_url


def _validate_remember_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("remember PostgreSQL test database URL is invalid")
    if parsed_url.database != REMEMBER_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "remember PostgreSQL tests require the exact dedicated database "
            f"{REMEMBER_POSTGRES_TEST_DATABASE_NAME}"
        )


def require_real_neo4j() -> None:
    if os.environ.get(REMEMBER_NEO4J_TESTS_ENV) != "1":
        pytest.skip(f"set {REMEMBER_NEO4J_TESTS_ENV}=1 to run real-Neo4j remember tests")


_REMEMBER_TEST_TABLES = (
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


async def _truncate_remember_test_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table}"' for table in _REMEMBER_TEST_TABLES)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = remember_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected(engine)
        await _truncate_remember_test_tables(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != REMEMBER_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated remember test database")


def test_remember_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        remember_test_database_url({})


def test_remember_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        remember_test_database_url(
            {
                REMEMBER_POSTGRES_TESTS_ENV: "1",
                REMEMBER_POSTGRES_TEST_DATABASE_URL_ENV: "postgresql+asyncpg://u:p@localhost:5432/wrong",
            }
        )


# --- deterministic provider doubles -----------------------------------------


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * 3072 for _ in texts]


class FakeKnowledgeExtractionClient:
    async def extract(self, chunk_text: str) -> Any:
        from sofias_memory.schemas.knowledge import (
            ChunkKnowledgeExtraction,
            ExtractedEntity,
            ExtractedRelation,
        )

        del chunk_text
        return ChunkKnowledgeExtraction(
            summary="A retrieval summary.",
            entities=[
                ExtractedEntity(
                    local_id="e1",
                    name="PostgreSQL",
                    type="Technology",
                    description="A database technology.",
                    aliases=[],
                    confidence=0.9,
                ),
                ExtractedEntity(
                    local_id="e2",
                    name="Sofias Memory",
                    type="System",
                    description="A persistent memory system.",
                    aliases=[],
                    confidence=0.9,
                ),
            ],
            relations=[
                ExtractedRelation(
                    source_local_id="e1",
                    target_local_id="e2",
                    predicate="supports",
                    description="PostgreSQL supports Sofias Memory.",
                    confidence=0.8,
                    evidence="PostgreSQL supports Sofias Memory.",
                )
            ],
        )


class FakeDocumentSummaryClient:
    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        del chunk_summaries
        return "A retrieval-ready document summary."


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
    with_neo4j: bool = False,
    worker_enabled: bool = True,
    url_transport: httpx.AsyncBaseTransport | None = None,
    url_resolver: Any = None,
) -> tuple[Any, AsyncSessionFactory, PipelineRegistry, object | None]:
    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    neo4j_resource = None
    graph_outbox_processor: GraphOutboxProcessor | None = None
    if with_neo4j:
        neo4j_resource = create_neo4j_resource_from_settings(settings)
        projection = Neo4jProjection(neo4j_resource)
        graph_outbox_processor = GraphOutboxProcessor(
            session_factory=session_factory, projection=projection
        )
    cognify_service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=FakeEmbeddingClient(),
        knowledge_extraction_client=FakeKnowledgeExtractionClient(),
        document_summary_client=FakeDocumentSummaryClient(),
    )
    resources: dict[str, Any] = {
        REMEMBER_RESOURCES_RESOURCE: RememberPipelineResources(
            settings=settings,
            cognify_service=cognify_service,
            url_transport=url_transport,
            url_resolver=url_resolver,
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
        graph_outbox_processor=graph_outbox_processor,
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


async def post_remember(
    app: Any,
    path: str,
    body: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post(path, headers=headers, json=dict(body))


async def post_remember_file(
    app: Any,
    *,
    filename: str,
    content: bytes,
    content_type: str,
    form: Mapping[str, Any],
    idempotency_key: str | None = None,
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post(
            "/api/v1/remember/file",
            headers=headers,
            data=dict(form),
            files={"file": (filename, content, content_type)},
        )


async def get_dataset_by_slug(
    session_factory: AsyncSessionFactory, slug: str
) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_slug(slug)
        if dataset is None:
            return None
        return SimpleNamespace(id=dataset.id, status=dataset.status)


async def get_source(
    session_factory: AsyncSessionFactory, source_id: UUID
) -> SimpleNamespace | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        source = await uow.sources.get_by_id(source_id)
        if source is None:
            return None
        return SimpleNamespace(
            id=source.id,
            status=source.status,
            storage_uri=source.storage_uri,
            version=source.version,
            content_sha256=source.content_sha256,
        )


async def count_pipeline_runs(
    session_factory: AsyncSessionFactory, *, pipeline_type: PipelineType
) -> int:
    async with PostgresUnitOfWork(session_factory) as uow:
        from sqlalchemy import func, select

        cast_uow = uow
        statement = (
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.pipeline_type == pipeline_type)
        )
        result = await cast_uow._session.scalar(statement)  # type: ignore[attr-defined]
        return int(result or 0)


async def wait_for_terminal(
    session_factory: AsyncSessionFactory, run_id: UUID, *, timeout_seconds: float = 10.0
) -> PipelineRunStatus:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            status = run.status if run is not None else None
        if status in (
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.FAILED,
            PipelineRunStatus.CANCELLED,
        ):
            return status
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"run {run_id} did not reach a terminal state in time")
        await asyncio.sleep(0.02)


# --- TEXT ---------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_ingest_wait_false_then_worker_completes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app, "/api/v1/remember", {"dataset": "main", "content": "hello world", "wait": False}
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["data"]["run_id"])
        status = await wait_for_terminal(session_factory, run_id)
        assert status == PipelineRunStatus.SUCCEEDED

        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None
            persisted = run.metrics[REMEMBER_RESULT_METRIC_KEY]
        source = await get_source(session_factory, UUID(str(persisted["source_id"])))
        assert source is not None
        assert source.status == SourceStatus.PENDING
        assert source.storage_uri is not None
        assert source.storage_uri.startswith("file://")
        assert persisted["chunks"] == 0
        assert persisted["deduplicated"] is False
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_full_mode_cognifies_and_creates_no_nested_cognify_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "hello world " * 20, "mode": "full", "wait": True},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "succeeded"
        assert data["chunks"] > 0
        assert data["entities"] > 0

        cognify_run_count = await count_pipeline_runs(
            session_factory, pipeline_type=PipelineType.COGNIFY
        )
        assert cognify_run_count == 0
        remember_run_count = await count_pipeline_runs(
            session_factory, pipeline_type=PipelineType.REMEMBER
        )
        assert remember_run_count == 1
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_dedup_reuses_source_and_full_mode_can_still_cognify_pending_dup(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        first = await post_remember(
            app, "/api/v1/remember", {"dataset": "main", "content": "same content", "wait": True}
        )
        assert first.status_code == 200
        first_source_id = first.json()["data"]["source_id"]

        second = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "same content", "mode": "full", "wait": True},
        )
        assert second.status_code == 200
        second_data = second.json()["data"]
        assert second_data["deduplicated"] is True
        assert second_data["source_id"] == first_source_id
        assert second_data["chunks"] > 0  # Case A: PENDING dup can still be cognified
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_force_true_creates_new_version(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        first = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "versioned content", "wait": True},
        )
        assert first.status_code == 200
        second = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "versioned content", "force": True, "wait": True},
        )
        assert second.status_code == 200
        assert second.json()["data"]["deduplicated"] is False
        assert second.json()["data"]["source_id"] != first.json()["data"]["source_id"]

        second_source = await get_source(session_factory, UUID(second.json()["data"]["source_id"]))
        assert second_source is not None
        assert second_source.version == 2
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_remember_concurrent_creates_only_one_main_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        assert await get_dataset_by_slug(session_factory, "main") is None
        responses = await asyncio.gather(
            post_remember(
                app, "/api/v1/remember", {"dataset": "main", "content": "race A", "wait": True}
            ),
            post_remember(
                app, "/api/v1/remember", {"dataset": "main", "content": "race B", "wait": True}
            ),
        )
        for response in responses:
            assert response.status_code == 200

        async with postgres_engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM datasets WHERE slug = 'main'")
            )
        assert count == 1
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_replay_wait_true_then_false_resolves_same_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        first = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "idempotent content", "wait": True},
            idempotency_key="rk1",
        )
        second = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "idempotent content", "wait": False},
            idempotency_key="rk1",
        )
        assert first.status_code == 200
        assert second.status_code in (200, 202)
        assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_b4_text_idempotency_key_resolves_same_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-513 SS 6: a historical B4 TEXT run (no `source_kind` key, no
    `wait` key) must still resolve as the same work under the new B5
    submission, via semantic-intent compatibility rather than raw
    payload_hash equality."""

    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    dataset_id = uuid4()
    legacy_run_id = uuid4()
    content_sha256 = sha256(b"legacy content").hexdigest()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name="main",
                slug="main",
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        legacy_input = {
            "dataset": "main",
            "content_sha256": content_sha256,
            "name": None,
            "metadata": {},
            "session_id": None,
            "mode": "ingest",
            "force": False,
        }
        await uow.pipeline_runs.add(
            PipelineRun(
                id=legacy_run_id,
                pipeline_type=PipelineType.REMEMBER,
                dataset_id=dataset_id,
                source_id=None,
                status=PipelineRunStatus.SUCCEEDED,
                idempotency_key="legacy-text-key",
                payload_hash="a" * 64,
                input=legacy_input,
                progress=1.0,
                current_step=None,
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="b" * 64,
                error_code=None,
                error_message=None,
                metrics={
                    REMEMBER_RESULT_METRIC_KEY: {
                        "dataset_id": str(dataset_id),
                        "source_id": None,
                        "document_id": None,
                        "content_hash": content_sha256,
                        "chunks": 0,
                        "entities": 0,
                        "relations": 0,
                        "deduplicated": False,
                    }
                },
                started_at=None,
                finished_at=None,
            )
        )
        await uow.commit()

    response = await post_remember(
        app,
        "/api/v1/remember",
        {"dataset": "main", "content": "legacy content", "wait": True},
        idempotency_key="legacy-text-key",
    )
    assert response.status_code == 200
    assert response.json()["data"]["run_id"] == str(legacy_run_id)


# --- FILE -----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_ingest_txt_preserves_original_bytes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        content = b"plain text file content"
        response = await post_remember_file(
            app,
            filename="note.txt",
            content=content,
            content_type="text/plain",
            form={"dataset": "main", "wait": "true"},
        )
        assert response.status_code == 200
        source_id = UUID(response.json()["data"]["source_id"])
        source = await get_source(session_factory, source_id)
        assert source is not None
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        parsed = urlparse(source.storage_uri)
        stored_path = Path(url2pathname(parsed.path))
        assert stored_path.read_bytes() == content
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_unsupported_extension_never_creates_a_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path)
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember_file(
            app,
            filename="evil.exe",
            content=b"MZ",
            content_type="application/octet-stream",
            form={"dataset": "main", "wait": "false"},
        )
        assert response.status_code == 400
        run_count = await count_pipeline_runs(session_factory, pipeline_type=PipelineType.REMEMBER)
        assert run_count == 0
    finally:
        await coordinator.stop()


# --- URL (local MockTransport, no real network) -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_url_ingest_fetches_only_in_worker_and_stores_bytes(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    import ipaddress

    body = b"remote text content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "text/plain"})

    transport = httpx.MockTransport(handler)

    def resolver(host: str, port: int) -> list[Any]:
        del host, port
        return [ipaddress.ip_address("93.184.216.34")]

    app, session_factory, _, _ = build_harness(
        postgres_engine, tmp_path, url_transport=transport, url_resolver=resolver
    )
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app,
            "/api/v1/remember/url",
            {"dataset": "main", "url": "https://example.com/doc.txt", "wait": True},
        )
        assert response.status_code == 200
        source_id = UUID(response.json()["data"]["source_id"])
        source = await get_source(session_factory, source_id)
        assert source is not None
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        parsed = urlparse(source.storage_uri)
        stored_path = Path(url2pathname(parsed.path))
        assert stored_path.read_bytes() == body
    finally:
        await coordinator.stop()


# --- crash / resume ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_crash_after_ingest_persist_before_storage_resumes_and_converges(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _, _ = build_harness(postgres_engine, tmp_path, worker_enabled=True)
    coordinator = app.state.pipeline_worker

    settings = make_settings(tmp_path)
    dataset_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name="main",
                slug="main",
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        await uow.commit()

    from sofias_memory.pipelines.registry import build_default_pipeline_registry
    from sofias_memory.services.pipeline_lifecycle import create_run_with_steps

    registry = build_default_pipeline_registry()
    work_input = remember_text_run_input(
        dataset="main",
        content_sha256=sha256(b"resume me").hexdigest(),
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"resume me")
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

    # "Crash" here: run ingest persist directly, bypassing the worker/engine,
    # simulating a process death exactly after this step's own commit.
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
    execute_result = await ingest_step.execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await ingest_step.persist(context, execute_result, uow)
        # Mirror the engine's own commit boundary (ADR-0009 SS O): the
        # step's authoritative mutation and its own PipelineStep ->
        # succeeded transition land in exactly one commit -- so a real
        # crash-then-resume never finds this step QUEUED again after its
        # persist() has actually run.
        from datetime import UTC, datetime

        from sofias_memory.domain import PipelineStepStatus
        from sofias_memory.services.pipeline_lifecycle import transition_step

        step_row = await uow.pipeline_steps.get_by_run_and_ordinal(run_id, 0)
        assert step_row is not None
        transition_step(step_row, PipelineStepStatus.RUNNING, now=datetime.now(UTC))
        transition_step(
            step_row,
            PipelineStepStatus.SUCCEEDED,
            now=datetime.now(UTC),
            output=execute_result.output,
        )
        await uow.commit()

    from sofias_memory.services.remember import ingress_artifact_exists

    assert ingress_artifact_exists(tmp_path, run_id=run_id)  # not yet finalized/deleted

    # Now start the real worker to resume this same run from finalize_storage.
    await coordinator.start()
    try:
        status = await wait_for_terminal(session_factory, run_id)
        assert status == PipelineRunStatus.SUCCEEDED
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None
            persisted = run.metrics[REMEMBER_RESULT_METRIC_KEY]
        source = await get_source(session_factory, UUID(str(persisted["source_id"])))
        assert source is not None
        assert source.storage_uri is not None
        assert not ingress_artifact_exists(tmp_path, run_id=run_id)
    finally:
        await coordinator.stop()


# --- partial unique operational-run constraint (SM-513 SS 42/43) -----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_unique_constraint_blocks_second_running_for_same_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    async with PostgresUnitOfWork(create_session_factory(postgres_engine)) as uow:
        dataset_id = uuid4()
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name="d1",
                slug="d1",
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        await uow.pipeline_runs.add(_bare_run(dataset_id, PipelineRunStatus.RUNNING))
        await uow.commit()

        with pytest.raises(IntegrityError):
            await uow.pipeline_runs.add(_bare_run(dataset_id, PipelineRunStatus.RUNNING))
            await uow.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_unique_constraint_allows_multiple_queued(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    async with PostgresUnitOfWork(create_session_factory(postgres_engine)) as uow:
        dataset_id = uuid4()
        await uow.datasets.add(
            Dataset(
                id=dataset_id,
                name="d2",
                slug="d2",
                status=DatasetStatus.ACTIVE,
                active_generation=0,
            )
        )
        await uow.pipeline_runs.add(_bare_run(dataset_id, PipelineRunStatus.QUEUED))
        await uow.pipeline_runs.add(_bare_run(dataset_id, PipelineRunStatus.QUEUED))
        await uow.commit()  # must not raise


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_unique_constraint_allows_different_datasets_running(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    async with PostgresUnitOfWork(create_session_factory(postgres_engine)) as uow:
        dataset_a, dataset_b = uuid4(), uuid4()
        await uow.datasets.add(
            Dataset(
                id=dataset_a, name="da", slug="da", status=DatasetStatus.ACTIVE, active_generation=0
            )
        )
        await uow.datasets.add(
            Dataset(
                id=dataset_b, name="db", slug="db", status=DatasetStatus.ACTIVE, active_generation=0
            )
        )
        await uow.pipeline_runs.add(_bare_run(dataset_a, PipelineRunStatus.RUNNING))
        await uow.pipeline_runs.add(_bare_run(dataset_b, PipelineRunStatus.RUNNING))
        await uow.commit()  # must not raise


def _bare_run(dataset_id: UUID | None, status: PipelineRunStatus) -> PipelineRun:
    now = None
    return PipelineRun(
        id=uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        status=status,
        idempotency_key=None,
        payload_hash="d" * 64,
        input={},
        progress=0.0,
        current_step=None,
        attempt=1 if status != PipelineRunStatus.QUEUED else 0,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint="e" * 64,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=now,
        finished_at=None,
    )


# --- real Neo4j full-mode projection convergence ---------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_mode_projects_to_real_neo4j_with_no_nested_cognify_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    require_real_neo4j()
    app, session_factory, _, neo4j_resource = build_harness(
        postgres_engine, tmp_path, with_neo4j=True
    )
    assert neo4j_resource is not None
    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        response = await post_remember(
            app,
            "/api/v1/remember",
            {"dataset": "main", "content": "graph content " * 20, "mode": "full", "wait": True},
        )
        assert response.status_code == 200
        dataset_id = UUID(response.json()["data"]["dataset_id"])

        deadline = asyncio.get_event_loop().time() + 10.0
        node_count = 0
        while asyncio.get_event_loop().time() < deadline:
            async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
                result = await session.run(
                    "MATCH (n) WHERE n.dataset_id = $ds RETURN count(n) AS c", ds=str(dataset_id)
                )
                record = await result.single()
                node_count = record["c"] if record is not None else 0
            if node_count > 0:
                break
            await asyncio.sleep(0.2)
        assert node_count > 0

        cognify_run_count = await count_pipeline_runs(
            session_factory, pipeline_type=PipelineType.COGNIFY
        )
        assert cognify_run_count == 0
    finally:
        await coordinator.stop()
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run(
                "MATCH (n) WHERE n.dataset_id = $ds DETACH DELETE n", ds=str(dataset_id)
            )
        await neo4j_resource.driver.close()
