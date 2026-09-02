"""Real-PostgreSQL tests for asynchronous Cognify (SM-510).

Proves the whole B5 path end to end against a real, dedicated PostgreSQL
database: durable submission from the HTTP route, ``wait=false``/``wait=true``
sharing one run and one engine, ``Idempotency-Key`` replay, the worker gate,
a real worker executing the two registered Cognify steps to ``succeeded``, a
``CognifyResult`` rebuilt purely from persisted state, source selection, and
``rebuild=true`` producing an invisible generation ``N+1`` that only becomes
visible when the activation step commits.

LLM, embedding and document-summary providers are deterministic in-process
fakes and the graph projection is an in-memory recorder -- no external
service is contacted. Requires migrations already applied through 0009.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
from sofias_memory.domain import (
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, Document, Source
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.engine import PipelineEngine
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.cognify import (
    ACTIVATE_GENERATION_STEP,
    COGNIFY_SERVICE_RESOURCE,
    PROCESS_SOURCES_STEP,
)
from sofias_memory.ports import ProjectionCommand
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    ExtractedEntity,
    ExtractedRelation,
)
from sofias_memory.services.cognify import COGNIFY_RESULT_METRIC_KEY, CognifyService
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.pipeline_queue_claimer import PipelineRunClaimer
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from tests.unit._app_factory import create_app

COGNIFY_ASYNC_TESTS_ENV = "SOFIAS_MEMORY_RUN_COGNIFY_ASYNC_POSTGRES_TESTS"
COGNIFY_ASYNC_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_COGNIFY_ASYNC_TEST_DATABASE_URL"
COGNIFY_ASYNC_TEST_DATABASE_NAME = "sofias_memory_cognify_async_test"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

EMBEDDING_DIMENSIONS = 3072
POLL_INTERVAL_MS = 20

SOURCE_TEXT = "PostgreSQL supports Sofias Memory and keeps provenance for every answer. " * 4
SECOND_SOURCE_TEXT = "Neo4j is a reconstructible projection of PostgreSQL knowledge. " * 4


# --- opt-in gate --------------------------------------------------------------


def cognify_async_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(COGNIFY_ASYNC_TESTS_ENV) != "1":
        pytest.skip(f"set {COGNIFY_ASYNC_TESTS_ENV}=1 to run async Cognify PostgreSQL tests")

    database_url = env.get(COGNIFY_ASYNC_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {COGNIFY_ASYNC_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_database_url(database_url)
    return database_url


def _validate_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("async Cognify PostgreSQL test database URL is invalid")

    if parsed_url.database != COGNIFY_ASYNC_TEST_DATABASE_NAME:
        pytest.skip(
            "async Cognify PostgreSQL tests require the exact dedicated database "
            f"{COGNIFY_ASYNC_TEST_DATABASE_NAME}"
        )


def test_cognify_async_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        cognify_async_test_database_url({})


def test_cognify_async_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        cognify_async_test_database_url({COGNIFY_ASYNC_TESTS_ENV: "1"})


def test_cognify_async_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        cognify_async_test_database_url(
            {
                COGNIFY_ASYNC_TESTS_ENV: "1",
                COGNIFY_ASYNC_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fixtures -----------------------------------------------------------------


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = cognify_async_test_database_url(os.environ)
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
    if current_database != COGNIFY_ASYNC_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated async Cognify test database"
        )


async def truncate_everything(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE pipeline_steps, pipeline_runs, graph_outbox, relation_evidence, "
                "relations, entity_mentions, entities, summaries, chunks, documents, "
                "sources, datasets RESTART IDENTITY CASCADE"
            )
        )


# --- deterministic provider doubles -------------------------------------------


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


class FakeKnowledgeExtractionClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction:
        self.calls.append(chunk_text)
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
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        self.calls.append(list(chunk_summaries))
        return "A retrieval-ready document summary."


class RecordingProjection:
    """In-memory stand-in for ``Neo4jProjection`` -- the outbox contract is
    what matters here, not the Cypher behind it (that is SM-506/B3 territory)."""

    def __init__(self) -> None:
        self.applied: list[ProjectionCommand] = []

    async def apply(self, command: ProjectionCommand) -> None:
        self.applied.append(command)


# --- harness ------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory",
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "chunk_max_tokens": 24,
        "chunk_overlap_tokens": 6,
        "chunk_min_tokens": 4,
        "request_wait_timeout_seconds": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@dataclass
class Harness:
    settings: Settings
    session_factory: AsyncSessionFactory
    registry: PipelineRegistry
    resources: dict[str, Any]
    projection: RecordingProjection
    embedding_client: FakeEmbeddingClient
    knowledge_client: FakeKnowledgeExtractionClient
    summary_client: FakeDocumentSummaryClient
    coordinator: PipelineWorkerCoordinator
    worker_registry: PipelineRegistry
    outbox_processor: GraphOutboxProcessor
    app: Any
    dataset_id: UUID = field(default_factory=uuid4)


def build_harness(
    engine: AsyncEngine,
    tmp_path: Path,
    *,
    worker_enabled: bool = True,
    worker_claims: bool = True,
) -> Harness:
    """``worker_claims=False`` gives the coordinator an empty registry: it is
    genuinely started, enabled and ready (so the submission gate passes), but
    never claims anything -- which is exactly how a run can be observed sitting
    durably ``QUEUED`` without racing an executor."""

    settings = make_settings(tmp_path)
    session_factory = create_session_factory(engine)
    projection = RecordingProjection()
    embedding_client = FakeEmbeddingClient()
    knowledge_client = FakeKnowledgeExtractionClient()
    summary_client = FakeDocumentSummaryClient()
    outbox_processor = GraphOutboxProcessor(
        session_factory=session_factory,
        projection=projection,
    )
    service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=embedding_client,
        knowledge_extraction_client=knowledge_client,
        document_summary_client=summary_client,
    )
    resources: dict[str, Any] = {COGNIFY_SERVICE_RESOURCE: service}
    registry = build_default_pipeline_registry()
    worker_registry = registry if worker_claims else PipelineRegistry([])
    # SM-510 boundary fix: Cognify no longer drains the outbox itself (that
    # would be a Neo4j call from inside a pipeline step phase that may not
    # make one), so the worker's own autonomous consumer is the projection
    # path -- exactly as in production.
    coordinator = PipelineWorkerCoordinator(
        session_factory,
        worker_registry,
        enabled=worker_enabled,
        poll_interval_ms=POLL_INTERVAL_MS,
        stale_after_seconds=settings.worker_stale_after_seconds,
        max_concurrent_datasets=settings.worker_max_concurrent_datasets,
        graph_outbox_processor=outbox_processor,
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
        resources=resources,
        projection=projection,
        embedding_client=embedding_client,
        knowledge_client=knowledge_client,
        summary_client=summary_client,
        coordinator=coordinator,
        worker_registry=worker_registry,
        outbox_processor=outbox_processor,
        app=app,
    )


async def seed_dataset(harness: Harness) -> UUID:
    async with PostgresUnitOfWork(harness.session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=harness.dataset_id,
                name=f"cognify-{harness.dataset_id}",
                slug=f"cognify-{harness.dataset_id}",
                active_generation=0,
            )
        )
        await uow.commit()
    return harness.dataset_id


async def seed_source(
    harness: Harness,
    *,
    text_content: str = SOURCE_TEXT,
    status: SourceStatus = SourceStatus.PENDING,
    generation: int = 0,
) -> UUID:
    source_id = uuid4()
    async with PostgresUnitOfWork(harness.session_factory) as uow:
        await uow.sources.add(
            Source(
                id=source_id,
                dataset_id=harness.dataset_id,
                kind=SourceKind.TEXT,
                name=f"note-{source_id}",
                mime_type="text/plain",
                original_uri=None,
                storage_uri=None,
                content_sha256=f"{source_id.int:064x}"[:64],
                normalized_sha256=f"{source_id.int:064x}"[:64],
                byte_size=len(text_content.encode("utf-8")),
                metadata_={},
                status=status,
                version=1,
            )
        )
        await uow.documents.add(
            Document(
                id=uuid4(),
                dataset_id=harness.dataset_id,
                source_id=source_id,
                generation=generation,
                title="note",
                language="und",
                normalized_text=text_content,
                text_sha256=f"{source_id.int:064x}"[:64],
                token_count=-1,
                metadata_={},
                is_active=True,
            )
        )
        await uow.commit()
    return source_id


def client_for(harness: Harness) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app),
        base_url="http://testserver",
    )


async def post_cognify(
    harness: Harness,
    body: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with client_for(harness) as client:
        return await client.post("/api/v1/cognify", headers=headers, json=dict(body))


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def run_row(engine: AsyncEngine, run_id: UUID) -> Mapping[str, Any]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT status, dataset_id, metrics, error_code, progress "
                "FROM pipeline_runs WHERE id = :id"
            ),
            {"id": run_id},
        )
        row = result.mappings().one()
        return dict(row)


async def step_rows(engine: AsyncEngine, run_id: UUID) -> list[Mapping[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT name, ordinal, status, input_hash, output FROM pipeline_steps "
                "WHERE run_id = :id ORDER BY ordinal"
            ),
            {"id": run_id},
        )
        return [dict(row) for row in result.mappings()]


async def active_generation(engine: AsyncEngine, dataset_id: UUID) -> int:
    return int(
        await scalar(
            engine,
            "SELECT active_generation FROM datasets WHERE id = :id",
            id=dataset_id,
        )
    )


async def chunk_ids_at(engine: AsyncEngine, dataset_id: UUID, generation: int) -> set[UUID]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT id FROM chunks WHERE dataset_id = :dataset_id AND generation = :generation"
            ),
            {"dataset_id": dataset_id, "generation": generation},
        )
        return {row[0] for row in result}


async def source_ids_with_chunks_at(
    engine: AsyncEngine, dataset_id: UUID, generation: int
) -> set[UUID]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT DISTINCT source_id FROM chunks "
                "WHERE dataset_id = :dataset_id AND generation = :generation"
            ),
            {"dataset_id": dataset_id, "generation": generation},
        )
        return {row[0] for row in result}


async def with_worker(harness: Harness, action: Callable[[], Any]) -> Any:
    """Run ``action`` with a freshly started worker.

    A stopped ``PipelineWorkerCoordinator`` is terminal by design (a real
    process boot owns exactly one ``worker_id``), so each call builds a new
    coordinator -- the same thing a restart does -- and rebinds it on the app
    so the submission readiness gate observes the live one.
    """

    coordinator = PipelineWorkerCoordinator(
        harness.session_factory,
        harness.worker_registry,
        enabled=True,
        poll_interval_ms=POLL_INTERVAL_MS,
        stale_after_seconds=harness.settings.worker_stale_after_seconds,
        max_concurrent_datasets=harness.settings.worker_max_concurrent_datasets,
        graph_outbox_processor=harness.outbox_processor,
        resources=harness.resources,
    )
    harness.coordinator = coordinator
    harness.app.state.pipeline_worker = coordinator
    await coordinator.start()
    try:
        return await action()
    finally:
        await coordinator.stop()


async def submit_only(harness: Harness, body: Mapping[str, Any], **kwargs: Any) -> httpx.Response:
    """Submit against a ready-but-idle worker, so the run stays durably queued."""

    return await with_worker(harness, lambda: post_cognify(harness, body, **kwargs))


# --- durable submission -------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_false_accepts_a_durable_queued_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    await seed_dataset(harness)
    await seed_source(harness)

    # The worker is ready but claims nothing: the run must be durably QUEUED
    # purely because submission committed it, never because anything ran.
    response = await submit_only(
        harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": False}
    )

    assert response.status_code == 202
    run_id = UUID(response.json()["data"]["run_id"])
    assert response.json()["data"]["status"] == "queued"
    row = await run_row(postgres_engine, run_id)
    assert row["status"] == PipelineRunStatus.QUEUED
    assert row["dataset_id"] == harness.dataset_id


@pytest.mark.asyncio
async def test_both_steps_are_materialized_at_submission_time(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    await seed_dataset(harness)
    await seed_source(harness)

    response = await submit_only(
        harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": False}
    )

    steps = await step_rows(postgres_engine, UUID(response.json()["data"]["run_id"]))
    assert [(step["name"], step["ordinal"]) for step in steps] == [
        (PROCESS_SOURCES_STEP, 0),
        (ACTIVATE_GENERATION_STEP, 1),
    ]
    assert all(step["status"] == PipelineStepStatus.QUEUED for step in steps)
    assert steps[0]["input_hash"] is not None
    assert steps[1]["input_hash"] is None


@pytest.mark.asyncio
async def test_worker_unavailable_creates_no_durable_state(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path, worker_enabled=False)
    await seed_dataset(harness)
    await seed_source(harness)

    response = await post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.WORKER_DISABLED
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_runs") == 0
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_steps") == 0


# --- end-to-end execution ------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_executes_cognify_end_to_end_for_wait_true(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    source_id = await seed_source(harness)

    response = await with_worker(
        harness,
        lambda: post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True}),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["generation"] == 0
    assert data["sources_processed"] == 1
    assert data["chunks"] > 0
    assert data["entities"] == 2
    assert data["relations"] == 1

    run_id = UUID(data["run_id"])
    row = await run_row(postgres_engine, run_id)
    assert row["status"] == PipelineRunStatus.SUCCEEDED
    assert float(row["progress"]) == 1.0
    steps = await step_rows(postgres_engine, run_id)
    assert all(step["status"] == PipelineStepStatus.SUCCEEDED for step in steps)
    assert (
        await scalar(postgres_engine, "SELECT status FROM sources WHERE id = :id", id=source_id)
        == SourceStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_result_is_rebuilt_from_persisted_run_metrics_only(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)

    response = await with_worker(
        harness,
        lambda: post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True}),
    )
    data = response.json()["data"]
    run_id = UUID(data["run_id"])

    persisted = (await run_row(postgres_engine, run_id))["metrics"][COGNIFY_RESULT_METRIC_KEY]
    assert persisted == {
        "dataset_id": str(harness.dataset_id),
        "generation": data["generation"],
        "sources_processed": data["sources_processed"],
        "chunks": data["chunks"],
        "entities": data["entities"],
        "relations": data["relations"],
    }

    # A brand-new process (fresh app, fresh submission service) replaying the
    # same Idempotency-Key rebuilds the identical result from PostgreSQL alone.
    replay_harness = build_harness(postgres_engine, tmp_path)
    replay_harness.dataset_id = harness.dataset_id
    await with_worker(
        replay_harness,
        lambda: post_cognify(
            replay_harness,
            {"dataset": f"cognify-{harness.dataset_id}", "wait": True},
            idempotency_key="replay-key",
        ),
    )
    second = await with_worker(
        replay_harness,
        lambda: post_cognify(
            replay_harness,
            {"dataset": f"cognify-{harness.dataset_id}", "wait": True},
            idempotency_key="replay-key",
        ),
    )
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_no_persisted_state_leaks_document_content(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)

    response = await with_worker(
        harness,
        lambda: post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True}),
    )
    run_id = UUID(response.json()["data"]["run_id"])

    row = await run_row(postgres_engine, run_id)
    steps = await step_rows(postgres_engine, run_id)
    encoded = json.dumps(
        {
            "metrics": row["metrics"],
            "error_code": row["error_code"],
            "steps": [step["output"] for step in steps],
        }
    )
    assert "PostgreSQL supports" not in encoded
    assert "retrieval summary" not in encoded
    assert "0.125" not in encoded


@pytest.mark.asyncio
async def test_graph_outbox_converges_through_the_projection_consumer(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Projection is now *eventually* consistent with the run, not synchronous.

    Cognify no longer drains the outbox inside the pipeline step (SM-510
    boundary fix): emitting those rows is an authoritative mutation that only
    exists once ``persist`` commits, and ``persist`` may never call Neo4j. So
    the run can reach ``succeeded`` with rows still pending, and SM-506's
    autonomous consumer converges them shortly afterwards -- which is what
    this asserts, with the worker still running.
    """

    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)

    async def cognify_then_wait_for_convergence() -> int:
        await post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True})
        return await wait_for_outbox_convergence(postgres_engine)

    pending = await with_worker(harness, cognify_then_wait_for_convergence)

    assert pending == 0
    assert harness.projection.applied


async def wait_for_outbox_convergence(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 10.0,
) -> int:
    """Poll until the autonomous consumer has drained every pending row."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    pending = -1
    while asyncio.get_running_loop().time() < deadline:
        pending = int(
            await scalar(engine, "SELECT count(*) FROM graph_outbox WHERE status <> 'done'")
        )
        if pending == 0:
            return 0
        await asyncio.sleep(POLL_INTERVAL_MS / 1000.0)
    return pending


# --- idempotency ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_idempotency_key_resolves_to_one_run_for_wait_false(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    await seed_dataset(harness)
    await seed_source(harness)
    body = {"dataset": f"cognify-{harness.dataset_id}", "wait": False}

    first = await submit_only(harness, body, idempotency_key="key-a")
    second = await submit_only(harness, body, idempotency_key="key-a")

    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_runs") == 1
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_steps") == 2


@pytest.mark.asyncio
async def test_wait_true_and_wait_false_share_the_same_run_under_one_key(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)
    slug = f"cognify-{harness.dataset_id}"

    async def submit_both() -> tuple[httpx.Response, httpx.Response]:
        waiting = await post_cognify(
            harness, {"dataset": slug, "wait": True}, idempotency_key="key-b"
        )
        not_waiting = await post_cognify(
            harness, {"dataset": slug, "wait": False}, idempotency_key="key-b"
        )
        return waiting, not_waiting

    waiting, not_waiting = await with_worker(harness, submit_both)

    assert waiting.json()["data"]["run_id"] == not_waiting.json()["data"]["run_id"]
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_runs") == 1


# --- source selection ----------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_source_ids_are_respected(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    selected = await seed_source(harness)
    ignored = await seed_source(harness, text_content=SECOND_SOURCE_TEXT)

    response = await with_worker(
        harness,
        lambda: post_cognify(
            harness,
            {
                "dataset": f"cognify-{harness.dataset_id}",
                "source_ids": [str(selected)],
                "wait": True,
            },
        ),
    )

    assert response.json()["data"]["sources_processed"] == 1
    assert await source_ids_with_chunks_at(postgres_engine, harness.dataset_id, 0) == {selected}
    assert (
        await scalar(postgres_engine, "SELECT status FROM sources WHERE id = :id", id=ignored)
        == SourceStatus.PENDING
    )


@pytest.mark.asyncio
async def test_without_source_ids_only_pending_sources_are_processed(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    pending = await seed_source(harness)
    failed = await seed_source(harness, text_content=SECOND_SOURCE_TEXT, status=SourceStatus.FAILED)

    response = await with_worker(
        harness,
        lambda: post_cognify(harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True}),
    )

    assert response.json()["data"]["sources_processed"] == 1
    assert await source_ids_with_chunks_at(postgres_engine, harness.dataset_id, 0) == {pending}
    assert (
        await scalar(postgres_engine, "SELECT status FROM sources WHERE id = :id", id=failed)
        == SourceStatus.FAILED
    )


@pytest.mark.asyncio
async def test_rebuild_with_source_ids_is_rejected_before_any_run_exists(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    source_id = await seed_source(harness)

    response = await post_cognify(
        harness,
        {
            "dataset": f"cognify-{harness.dataset_id}",
            "rebuild": True,
            "source_ids": [str(source_id)],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_runs") == 0


# --- rebuild / generation ------------------------------------------------------


async def cognify_once(harness: Harness, **body: Any) -> httpx.Response:
    return await with_worker(
        harness,
        lambda: post_cognify(
            harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": True, **body}
        ),
    )


@pytest.mark.asyncio
async def test_rebuild_creates_generation_one_for_the_whole_dataset(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    pending = await seed_source(harness)
    second = await seed_source(harness, text_content=SECOND_SOURCE_TEXT)
    await cognify_once(harness)
    assert await active_generation(postgres_engine, harness.dataset_id) == 0

    response = await cognify_once(harness, rebuild=True)

    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["generation"] == 1
    assert data["sources_processed"] == 2
    assert await active_generation(postgres_engine, harness.dataset_id) == 1
    # Every source is reprocessed into the new generation, not only pending ones.
    assert await source_ids_with_chunks_at(postgres_engine, harness.dataset_id, 1) == {
        pending,
        second,
    }
    # The previous generation's rows are still there, just no longer current.
    assert await chunk_ids_at(postgres_engine, harness.dataset_id, 0)


@pytest.mark.asyncio
async def test_rebuild_carries_active_entities_into_the_new_generation(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)
    await cognify_once(harness)

    await cognify_once(harness, rebuild=True)

    stale_entities = await scalar(
        postgres_engine,
        "SELECT count(*) FROM entities WHERE dataset_id = :id AND is_active AND generation <> 1",
        id=harness.dataset_id,
    )
    assert stale_entities == 0
    current_relations = await scalar(
        postgres_engine,
        "SELECT count(*) FROM relations WHERE dataset_id = :id AND is_active AND generation = 1",
        id=harness.dataset_id,
    )
    assert current_relations >= 1


@pytest.mark.asyncio
async def test_generation_stays_current_until_activation_commits(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Interrupting a rebuild right after ``process_sources`` succeeded must
    leave the dataset on generation ``N``: the half-built ``N+1`` exists in
    PostgreSQL but is invisible to every ``active_generation`` reader."""

    setup = build_harness(postgres_engine, tmp_path)
    await seed_dataset(setup)
    await seed_source(setup)
    await cognify_once(setup)

    # A second, deliberately idle worker accepts the rebuild without executing
    # it, so this test -- not a background poll loop -- drives the engine.
    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    harness.dataset_id = setup.dataset_id
    response = await submit_only(
        harness,
        {"dataset": f"cognify-{harness.dataset_id}", "rebuild": True, "wait": False},
    )
    run_id = UUID(response.json()["data"]["run_id"])

    paused, claimed = await execute_until_step_boundary(harness, stop_before_step=2)
    assert paused.paused_for_shutdown is True

    steps = await step_rows(postgres_engine, run_id)
    assert steps[0]["status"] == PipelineStepStatus.SUCCEEDED
    assert steps[1]["status"] == PipelineStepStatus.QUEUED
    # Generation 1 artifacts already exist...
    assert await chunk_ids_at(postgres_engine, harness.dataset_id, 1)
    # ...but nothing reading `active_generation` can see them yet.
    assert await active_generation(postgres_engine, harness.dataset_id) == 0
    assert (await run_row(postgres_engine, run_id))["status"] == PipelineRunStatus.RUNNING

    generation_one_before_resume = await chunk_ids_at(postgres_engine, harness.dataset_id, 1)

    # Resuming the interrupted run activates the new generation and never
    # duplicates the artifacts the first attempt already committed.
    resumed, _ = await execute_until_step_boundary(harness, stop_before_step=None, claimed=claimed)
    assert resumed.status == PipelineRunStatus.SUCCEEDED
    assert await active_generation(postgres_engine, harness.dataset_id) == 1
    assert (
        await chunk_ids_at(postgres_engine, harness.dataset_id, 1) == generation_one_before_resume
    )
    assert (await run_row(postgres_engine, run_id))["metrics"][COGNIFY_RESULT_METRIC_KEY][
        "generation"
    ] == 1


@pytest.mark.asyncio
async def test_failure_before_activation_leaves_the_generation_untouched(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    setup = build_harness(postgres_engine, tmp_path)
    await seed_dataset(setup)
    await seed_source(setup)
    await cognify_once(setup)

    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    harness.dataset_id = setup.dataset_id
    response = await submit_only(
        harness,
        {"dataset": f"cognify-{harness.dataset_id}", "rebuild": True, "wait": False},
    )
    run_id = UUID(response.json()["data"]["run_id"])

    # A provider failure inside `process_sources` fails the run before the
    # activation step ever starts.
    async def always_failing(texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("embedding provider is down")

    harness.embedding_client.embed_texts = always_failing  # type: ignore[method-assign]
    await execute_until_step_boundary(harness, stop_before_step=None)

    assert await active_generation(postgres_engine, harness.dataset_id) == 0
    row = await run_row(postgres_engine, run_id)
    assert row["status"] in (PipelineRunStatus.QUEUED, PipelineRunStatus.FAILED)
    steps = await step_rows(postgres_engine, run_id)
    assert steps[1]["status"] == PipelineStepStatus.QUEUED
    assert "provider is down" not in json.dumps(row["error_code"])


async def execute_until_step_boundary(
    harness: Harness,
    *,
    stop_before_step: int | None,
    claimed: Any = None,
) -> tuple[Any, Any]:
    """Claim and execute one attempt directly, optionally pausing before the
    Nth step's checkpoint -- the same cooperative pause the worker performs at
    shutdown, which is exactly how a real interruption lands between steps.

    Passing a previous call's ``claimed`` token resumes that same still-owned
    attempt (the run is left ``RUNNING`` by a shutdown pause, so it is not
    re-claimable until it goes stale -- resuming under the original fencing
    token is what the owning process itself would do).
    """

    if claimed is None:
        claimer = PipelineRunClaimer(harness.session_factory)
        claimed = await claimer.try_claim_one(worker_id=f"test-worker-{uuid4()}")
        assert claimed is not None
    engine = PipelineEngine(harness.session_factory, harness.registry, resources=harness.resources)
    checkpoints = 0

    def stop_requested() -> bool:
        nonlocal checkpoints
        checkpoints += 1
        return stop_before_step is not None and checkpoints >= stop_before_step

    return await engine.execute(claimed, stop_requested=stop_requested), claimed


# --- execute()/persist() crash consistency (SM-510 boundary fix) --------------


async def authoritative_row_counts(engine: AsyncEngine, dataset_id: UUID) -> dict[str, int]:
    """Every authoritative artifact a cognify would produce for a dataset."""

    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in ("chunks", "entities", "relations", "summaries", "graph_outbox"):
            counts[table] = int(
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE dataset_id = :id"),
                    {"id": dataset_id},
                )
            )
        counts["entity_mentions"] = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id "
                    "WHERE c.dataset_id = :id"
                ),
                {"id": dataset_id},
            )
        )
        counts["relation_evidence"] = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM relation_evidence e JOIN chunks c ON c.id = e.chunk_id "
                    "WHERE c.dataset_id = :id"
                ),
                {"id": dataset_id},
            )
        )
    return counts


@pytest.mark.asyncio
async def test_no_authoritative_state_is_committed_between_execute_and_persist(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The SM-510 boundary bug, proven fixed against real PostgreSQL.

    A fault is injected at exactly the boundary this story is about: the
    ``process_sources`` step's ``execute`` phase completes in full -- every
    chunk computed, every embedding fetched, every entity/relation resolved --
    and then its ``persist`` phase raises before the engine's transaction can
    commit. That is the precise window in which the buggy implementation had
    *already* committed chunks, entities, relations, summaries and
    ``graph_outbox`` rows from inside ``execute``, while its ``PipelineStep``
    row was still ``running``.

    The assertion is absolute: PostgreSQL must show **zero** authoritative
    Cognify rows for the target dataset, the step must not be ``succeeded``,
    and the source must not have been silently mutated either.
    """

    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    await seed_dataset(harness)
    source_id = await seed_source(harness)
    second_source_id = await seed_source(harness, text_content=SECOND_SOURCE_TEXT)

    response = await submit_only(
        harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": False}
    )
    run_id = UUID(response.json()["data"]["run_id"])

    step = harness.registry.get(PipelineType.COGNIFY).steps[0].step
    executed: list[str] = []
    real_execute = step.execute
    real_persist = step.persist

    async def execute_then_fail_in_persist(context: Any) -> Any:
        result = await real_execute(context)
        executed.append("execute")
        return result

    async def failing_persist(context: Any, result: Any, uow: Any) -> None:
        # Fail as late as legitimately possible: the batch is staged, the
        # engine's transaction is open, and everything the step wants to write
        # is about to be written -- then the process "dies".
        executed.append("persist")
        raise RuntimeError("interrupted between execute and commit")

    step.execute = execute_then_fail_in_persist  # type: ignore[method-assign]
    step.persist = failing_persist  # type: ignore[method-assign]
    try:
        await execute_until_step_boundary(harness, stop_before_step=None)
    finally:
        step.execute = real_execute  # type: ignore[method-assign]
        step.persist = real_persist  # type: ignore[method-assign]

    # The external phase really did run to completion -- this is not a test
    # that passes because nothing happened.
    assert executed == ["execute", "persist"]
    assert harness.embedding_client.calls
    assert harness.knowledge_client.calls
    assert harness.summary_client.calls

    # ...and yet PostgreSQL holds nothing at all.
    assert await authoritative_row_counts(postgres_engine, harness.dataset_id) == {
        "chunks": 0,
        "entities": 0,
        "relations": 0,
        "summaries": 0,
        "graph_outbox": 0,
        "entity_mentions": 0,
        "relation_evidence": 0,
    }
    steps = await step_rows(postgres_engine, run_id)
    assert steps[0]["status"] != PipelineStepStatus.SUCCEEDED
    assert steps[1]["status"] == PipelineStepStatus.QUEUED
    assert await active_generation(postgres_engine, harness.dataset_id) == 0
    for seeded in (source_id, second_source_id):
        assert (
            await scalar(postgres_engine, "SELECT status FROM sources WHERE id = :id", id=seeded)
            == SourceStatus.PENDING
        )

    # A clean, uninjected run over the same dataset then commits the whole
    # batch at once -- nothing the aborted attempt did has to be undone first.
    retry = build_harness(postgres_engine, tmp_path)
    retry.dataset_id = harness.dataset_id
    retried = await cognify_once(retry)
    assert retried.json()["data"]["status"] == "succeeded"
    committed = await authoritative_row_counts(postgres_engine, harness.dataset_id)
    assert committed["chunks"] > 0
    assert committed["entities"] > 0
    assert committed["relations"] > 0
    assert committed["summaries"] == 2
    assert committed["graph_outbox"] > 0
    assert (
        await scalar(postgres_engine, "SELECT status FROM sources WHERE id = :id", id=source_id)
        == SourceStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_a_mid_batch_execute_failure_commits_nothing_for_earlier_sources(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The same guarantee, with the fault inside ``execute`` itself.

    The first source is fully computed; the second source's provider call then
    fails. Under the old per-source incremental commits the first source's
    chunks/entities/summary were already durable at that point. Now the whole
    batch is discarded together.
    """

    harness = build_harness(postgres_engine, tmp_path, worker_claims=False)
    await seed_dataset(harness)
    await seed_source(harness)
    await seed_source(harness, text_content=SECOND_SOURCE_TEXT)

    response = await submit_only(
        harness, {"dataset": f"cognify-{harness.dataset_id}", "wait": False}
    )
    run_id = UUID(response.json()["data"]["run_id"])

    summaries_seen = 0
    real_summarize = harness.summary_client.summarize

    async def fail_on_the_second_source(chunk_summaries: Sequence[str]) -> str:
        nonlocal summaries_seen
        summaries_seen += 1
        if summaries_seen > 1:
            raise RuntimeError("summary provider is down")
        return await real_summarize(chunk_summaries)

    harness.summary_client.summarize = fail_on_the_second_source  # type: ignore[method-assign]
    await execute_until_step_boundary(harness, stop_before_step=None)

    assert summaries_seen == 2
    assert await authoritative_row_counts(postgres_engine, harness.dataset_id) == {
        "chunks": 0,
        "entities": 0,
        "relations": 0,
        "summaries": 0,
        "graph_outbox": 0,
        "entity_mentions": 0,
        "relation_evidence": 0,
    }
    steps = await step_rows(postgres_engine, run_id)
    assert steps[0]["status"] != PipelineStepStatus.SUCCEEDED
    assert (
        await scalar(postgres_engine, "SELECT count(*) FROM sources WHERE status <> 'pending'") == 0
    )


@pytest.mark.asyncio
async def test_repeated_rebuild_does_not_duplicate_active_artifacts(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)
    await cognify_once(harness)
    await cognify_once(harness, rebuild=True)
    generation_one = await chunk_ids_at(postgres_engine, harness.dataset_id, 1)
    entities_before = await scalar(
        postgres_engine,
        "SELECT count(*) FROM entities WHERE dataset_id = :id",
        id=harness.dataset_id,
    )

    # Re-running the exact same rebuild request now targets generation 2, but
    # every source is already complete at generation 1 and nothing about
    # generation 1 may be duplicated or mutated.
    await cognify_once(harness, rebuild=True)

    assert await chunk_ids_at(postgres_engine, harness.dataset_id, 1) == generation_one
    assert (
        await scalar(
            postgres_engine,
            "SELECT count(*) FROM entities WHERE dataset_id = :id",
            id=harness.dataset_id,
        )
        == entities_before
    )


@pytest.mark.asyncio
async def test_concurrent_wait_true_requests_share_one_run(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = build_harness(postgres_engine, tmp_path)
    await seed_dataset(harness)
    await seed_source(harness)
    slug = f"cognify-{harness.dataset_id}"

    async def submit_concurrently() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                post_cognify(harness, {"dataset": slug, "wait": True}, idempotency_key="key-c"),
                post_cognify(harness, {"dataset": slug, "wait": True}, idempotency_key="key-c"),
            )
        )

    responses = await with_worker(harness, submit_concurrently)

    run_ids = {response.json()["data"]["run_id"] for response in responses}
    assert len(run_ids) == 1
    assert await scalar(postgres_engine, "SELECT count(*) FROM pipeline_runs") == 1
