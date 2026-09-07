"""Real-PostgreSQL SM-606 hardening/compatibility proofs for Sessions.

Proves that introducing first-class durable Sessions (SM-601..SM-605) never
altered prior Sofias Memory invariants: pre-v0.3.0 legacy carriers of a
textual ``session_id`` never lazy-create or infer a first-class Session
association; a Session may span multiple Datasets without the Session ever
acquiring Dataset ownership; archive/restore correctly re-admits new
activity; and Session-only operations (create/archive/restore/SessionEntry
append/Query and PipelineRun association) never write to ``graph_outbox`` or
touch Neo4j, while genuine knowledge work in the same harness still does.

Requires migrations already applied through 0013. No new migration.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
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

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SessionStatus
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import (
    Dataset,
    Document,
    MemoryEntry,
    PipelineRun,
    Query,
    Source,
)
from sofias_memory.infrastructure.postgres.models import (
    Session as SessionModel,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.pipelines.steps.remember import (
    REMEMBER_RESOURCES_RESOURCE,
    RememberPipelineResources,
)
from sofias_memory.schemas.session_entries import SessionEntryCreateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.session_entries import SessionEntryService
from sofias_memory.services.sessions import SessionService
from tests.unit._app_factory import create_app

SESSIONS_COMPAT_TESTS_ENV = "SOFIAS_MEMORY_RUN_SESSIONS_COMPAT_POSTGRES_TESTS"
SESSIONS_COMPAT_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_SESSIONS_COMPAT_TEST_DATABASE_URL"
SESSIONS_COMPAT_TEST_DATABASE_NAME = "sofias_memory_sessions_compat_test"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
POLL_INTERVAL_MS = 20


def sessions_compat_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(SESSIONS_COMPAT_TESTS_ENV) != "1":
        pytest.skip(f"set {SESSIONS_COMPAT_TESTS_ENV}=1 to run Sessions compatibility tests")
    database_url = env.get(SESSIONS_COMPAT_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {SESSIONS_COMPAT_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("Sessions compatibility test database URL is invalid")
    if parsed_url.database != SESSIONS_COMPAT_TEST_DATABASE_NAME:
        pytest.skip(
            "Sessions compatibility tests require the exact dedicated database "
            f"{SESSIONS_COMPAT_TEST_DATABASE_NAME}"
        )
    return database_url


_TEST_TABLES = (
    "graph_outbox",
    "pipeline_steps",
    "pipeline_runs",
    "feedback",
    "queries",
    "session_entries",
    "sessions",
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
    database_url = sessions_compat_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
        if current_database != SESSIONS_COMPAT_TEST_DATABASE_NAME:
            pytest.skip(
                "connected PostgreSQL database is not the dedicated Sessions compatibility "
                "test database"
            )
        async with engine.begin() as connection:
            tables = ", ".join(f'"{table}"' for table in _TEST_TABLES)
            await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
        yield engine
    finally:
        await dispose_async_engine(engine)


def test_sessions_compat_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        sessions_compat_test_database_url({})


# --- deterministic Cognify provider doubles (mode=full knowledge generation,
# used only by the graph_outbox isolation test) -------------------------------


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
    engine: AsyncEngine, tmp_path: Path, *, worker_claims: bool = True
) -> tuple[Any, AsyncSessionFactory, PipelineRegistry]:
    """``worker_claims=False`` mirrors the established pattern
    (test_run_control_postgres_integration.py): the coordinator is genuinely
    started/enabled/running (submission gate passes) but never claims
    anything -- sufficient for every test here except the graph_outbox
    isolation test's own mode=full Remember, which needs real claiming."""

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
        enabled=True,
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


async def seed_minimal_source_and_document(
    session_factory: AsyncSessionFactory, *, dataset_id: UUID
) -> UUID:
    source_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.sources.add(
            Source(
                id=source_id,
                dataset_id=dataset_id,
                kind="text",
                name="legacy-source",
                mime_type="text/plain",
                content_sha256="a" * 64,
                byte_size=11,
                status="active",
                storage_uri=None,
                metadata_={},
                version=1,
            )
        )
        await uow.documents.add(
            Document(
                id=uuid4(),
                dataset_id=dataset_id,
                source_id=source_id,
                generation=0,
                title="legacy-doc",
                language="en",
                normalized_text="hello world",
                text_sha256="b" * 64,
                token_count=2,
                is_active=True,
                metadata_={"session_id": "legacy-session-text"},
            )
        )
        await uow.commit()
    return source_id


async def sessions_row_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(text("SELECT count(*) FROM sessions"))
        return int(result or 0)


async def graph_outbox_row_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(text("SELECT count(*) FROM graph_outbox"))
        return int(result or 0)


# ==============================================================================
# Legacy compatibility (SM-606 SS 10-15)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_memory_entry_session_id_text_does_not_create_session(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"legacy-mem-{uuid4()}")

    async with session_factory() as raw_session:
        raw_session.add(
            MemoryEntry(
                id=uuid4(),
                dataset_id=dataset_id,
                source_id=None,
                session_id="legacy-session-text",
                entry_type="note",
                content="legacy content",
                metadata_={},
            )
        )
        await raw_session.commit()

    assert await sessions_row_count(postgres_engine) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_document_metadata_session_id_does_not_create_session(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"legacy-doc-{uuid4()}")

    await seed_minimal_source_and_document(session_factory, dataset_id=dataset_id)

    assert await sessions_row_count(postgres_engine) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_pipeline_run_input_session_id_run_api_returns_null(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_slug = f"legacy-run-{uuid4()}"
    dataset_id = await seed_dataset(session_factory, slug=dataset_slug)
    run_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.pipeline_runs.add(
            PipelineRun(
                id=run_id,
                pipeline_type=PipelineType.REMEMBER,
                dataset_id=dataset_id,
                source_id=None,
                session_id=None,
                status=PipelineRunStatus.FAILED,
                idempotency_key=None,
                payload_hash="a" * 64,
                # URL kind with source_id=None: prepare_remember_retry_ingress's
                # own case 3 ("never acquired") lets retry proceed with a fresh
                # fetch, without needing a real staged ingress artifact.
                input={
                    "source_kind": "url",
                    "dataset": dataset_slug,
                    "url": "https://example.com/legacy",
                    "metadata": {},
                    "session_id": "legacy-session-text",
                    "mode": "ingest",
                    "force": False,
                },
                progress=1.0,
                current_step=None,
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="b" * 64,
                error_code="X",
                error_message="legacy failure",
                metrics={},
                started_at=None,
                finished_at=None,
            )
        )
        await uow.commit()

    async with build_client(app) as client:
        response = await client.get(
            f"/api/v1/runs/{run_id}", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )
    assert response.status_code == 200
    assert response.json()["data"]["session_uuid"] is None
    assert await sessions_row_count(postgres_engine) == 0

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        async with build_client(app) as client:
            retry_response = await client.post(
                f"/api/v1/runs/{run_id}/retry", headers={API_KEY_HEADER: EXPECTED_API_KEY}
            )
        assert retry_response.status_code == 202
        assert retry_response.json()["data"]["session_uuid"] is None
        child_id = UUID(retry_response.json()["data"]["run_id"])
        async with PostgresUnitOfWork(session_factory) as uow:
            child = await uow.pipeline_runs.get_by_id(child_id)
            assert child is not None
            assert child.session_id is None
            await uow.commit()
        assert await sessions_row_count(postgres_engine) == 0
    finally:
        await coordinator.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_query_returns_null_session_and_empty_context(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"legacy-query-{uuid4()}")
    query_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.queries.add(
            Query(
                id=query_id,
                query_text="legacy-session-text sounds like a key",
                dataset_ids=[dataset_id],
                mode="chunks",
                answer=None,
                references={"items": []},
                timings={"total": 1},
                model=None,
                session_id=None,
                session_context_entry_ids=[],
            )
        )
        await uow.commit()

    async with build_client(app) as client:
        response = await client.get(
            f"/api/v1/provenance/query/{query_id}", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["session_uuid"] is None
    assert data["session_context"] == []
    assert await sessions_row_count(postgres_engine) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_adversarial_matching_session_key_never_retroactively_associates(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-606 SS 15: creating a first-class Session whose key coincidentally
    matches legacy textual carriers, AFTER those legacy rows already exist,
    must never retroactively associate them -- association exists only via
    the persisted FK."""

    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"legacy-adversarial-{uuid4()}")

    legacy_key = "legacy-session-text"
    async with session_factory() as raw_session:
        raw_session.add(
            MemoryEntry(
                id=uuid4(),
                dataset_id=dataset_id,
                source_id=None,
                session_id=legacy_key,
                entry_type="note",
                content="legacy content",
                metadata_={},
            )
        )
        await raw_session.commit()
    await seed_minimal_source_and_document(session_factory, dataset_id=dataset_id)

    legacy_run_id = uuid4()
    legacy_query_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.pipeline_runs.add(
            PipelineRun(
                id=legacy_run_id,
                pipeline_type=PipelineType.REMEMBER,
                dataset_id=dataset_id,
                source_id=None,
                session_id=None,
                status=PipelineRunStatus.FAILED,
                idempotency_key=None,
                payload_hash="a" * 64,
                input={"session_id": legacy_key},
                progress=1.0,
                current_step=None,
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="b" * 64,
                error_code="X",
                error_message="legacy failure",
                metrics={},
                started_at=None,
                finished_at=None,
            )
        )
        await uow.queries.add(
            Query(
                id=legacy_query_id,
                query_text=legacy_key,
                dataset_ids=[dataset_id],
                mode="chunks",
                answer=None,
                references={"items": []},
                timings={"total": 1},
                model=None,
                session_id=None,
                session_context_entry_ids=[],
            )
        )
        await uow.commit()

    # Now materialize a first-class Session with the exact matching key.
    created = await SessionService(session_factory=session_factory).create_session(
        SessionCreateRequest(session_id=legacy_key)
    )

    async with build_client(app) as client:
        run_response = await client.get(
            f"/api/v1/runs/{legacy_run_id}", headers={API_KEY_HEADER: EXPECTED_API_KEY}
        )
        query_response = await client.get(
            f"/api/v1/provenance/query/{legacy_query_id}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        session_runs_response = await client.get(
            f"/api/v1/runs?session_uuid={created.session_uuid}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        session_queries_response = await client.get(
            f"/api/v1/sessions/{created.session_uuid}/queries",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert run_response.json()["data"]["session_uuid"] is None
    assert query_response.json()["data"]["session_uuid"] is None
    # The newly created Session's own Run/Query listings must stay empty --
    # the legacy rows are never swept in by textual coincidence.
    assert session_runs_response.json()["data"]["items"] == []
    assert session_queries_response.json()["data"]["items"] == []
    assert await sessions_row_count(postgres_engine) == 1


# ==============================================================================
# Multi-dataset Session (SM-606 SS 26-28)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_spans_multiple_datasets_without_dataset_ownership(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_a = await seed_dataset(session_factory, slug=f"multi-a-{uuid4()}")
    dataset_b = await seed_dataset(session_factory, slug=f"multi-b-{uuid4()}")
    dataset_c = await seed_dataset(session_factory, slug=f"multi-c-{uuid4()}")

    session_service = SessionService(session_factory=session_factory)
    created = await session_service.create_session(
        SessionCreateRequest(session_id=f"sm606-multi-{uuid4().hex}")
    )

    async def add_query(dataset_ids: list[UUID]) -> UUID:
        query_id = uuid4()
        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.queries.add(
                Query(
                    id=query_id,
                    query_text="multi-dataset probe",
                    dataset_ids=dataset_ids,
                    mode="chunks",
                    answer=None,
                    references={"items": []},
                    timings={"total": 1},
                    model=None,
                    session_id=created.session_uuid,
                    session_context_entry_ids=[],
                )
            )
            await uow.commit()
        return query_id

    query_1 = await add_query([dataset_a])
    query_2 = await add_query([dataset_a, dataset_b])
    query_3 = await add_query([dataset_c])

    async with session_factory() as raw_session:
        rows = {
            query_1: await raw_session.get(Query, query_1),
            query_2: await raw_session.get(Query, query_2),
            query_3: await raw_session.get(Query, query_3),
        }
        session_row = await raw_session.get(SessionModel, created.session_uuid)

    for query_id, row in rows.items():
        assert row is not None
        assert row.session_id == created.session_uuid, query_id

    assert list(rows[query_1].dataset_ids) == [dataset_a]
    assert set(rows[query_2].dataset_ids) == {dataset_a, dataset_b}
    assert list(rows[query_3].dataset_ids) == [dataset_c]

    # Session.updated_at is untouched by any of these multi-dataset Queries.
    assert session_row is not None
    assert session_row.updated_at == created.updated_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sessions_table_has_no_dataset_ownership_column(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'sessions' "
                "AND column_name IN ('dataset_id', 'default_dataset_id')"
            )
        )
        columns = result.fetchall()
    assert columns == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_and_run_associate_same_session_across_different_datasets(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-606 SS 28: one Session can carry a Query scoped to Dataset A+B and
    a Remember Run scoped to Dataset C with no conflict; Dataset scope stays
    at the operation level, never propagated to the Session."""

    _, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_a = await seed_dataset(session_factory, slug=f"assoc-a-{uuid4()}")
    dataset_b = await seed_dataset(session_factory, slug=f"assoc-b-{uuid4()}")
    dataset_c = await seed_dataset(session_factory, slug=f"assoc-c-{uuid4()}")

    created = await SessionService(session_factory=session_factory).create_session(
        SessionCreateRequest(session_id=f"sm606-assoc-{uuid4().hex}")
    )

    query_id = uuid4()
    run_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.queries.add(
            Query(
                id=query_id,
                query_text="scoped to A+B",
                dataset_ids=[dataset_a, dataset_b],
                mode="chunks",
                answer=None,
                references={"items": []},
                timings={"total": 1},
                model=None,
                session_id=created.session_uuid,
                session_context_entry_ids=[],
            )
        )
        await uow.pipeline_runs.add(
            PipelineRun(
                id=run_id,
                pipeline_type=PipelineType.REMEMBER,
                dataset_id=dataset_c,
                source_id=None,
                session_id=created.session_uuid,
                status=PipelineRunStatus.QUEUED,
                idempotency_key=None,
                payload_hash="c" * 64,
                input={"dataset": "assoc-c"},
                progress=0.0,
                current_step=None,
                attempt=0,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="d" * 64,
                error_code=None,
                error_message=None,
                metrics={},
                started_at=None,
                finished_at=None,
            )
        )
        await uow.commit()

    async with session_factory() as raw_session:
        query_row = await raw_session.get(Query, query_id)
        run_row = await raw_session.get(PipelineRun, run_id)

    assert query_row is not None and query_row.session_id == created.session_uuid
    assert run_row is not None and run_row.session_id == created.session_uuid
    assert run_row.dataset_id == dataset_c
    assert set(query_row.dataset_ids) == {dataset_a, dataset_b}


# ==============================================================================
# Session.updated_at invariants (SM-606 SS 41)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remember_admission_and_manual_retry_never_touch_session_updated_at(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Closes the two remaining cells of the SS 41 coverage matrix not
    already covered elsewhere: Recall (SM-604 unit test), Forget/Dataset
    Delete (this file's own preservation tests) already prove
    `Session.updated_at` invariance; this test adds Remember admission and
    manual retry."""

    from sofias_memory.services.pipeline_lifecycle import transition_run

    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_slug = f"updated-at-{uuid4()}"
    dataset_id = await seed_dataset(session_factory, slug=dataset_slug)

    session_service = SessionService(session_factory=session_factory)
    session_key = f"sm606-updated-at-{uuid4().hex}"
    created = await session_service.create_session(SessionCreateRequest(session_id=session_key))
    baseline_updated_at = created.updated_at

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        async with build_client(app) as client:
            remember_response = await client.post(
                "/api/v1/remember",
                headers={API_KEY_HEADER: EXPECTED_API_KEY},
                json={
                    "dataset": dataset_slug,
                    "content": "updated_at probe",
                    "mode": "ingest",
                    "wait": False,
                    "session_id": session_key,
                },
            )
        assert remember_response.status_code == 202

        async with session_factory() as raw_session:
            after_remember = await raw_session.get(SessionModel, created.session_uuid)
        assert after_remember is not None
        assert after_remember.updated_at == baseline_updated_at

        # Manual retry: fail a plain (no-Session) run, then retry it, and
        # confirm the UNRELATED Session created above is still untouched.
        run_id = uuid4()
        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.pipeline_runs.add(
                PipelineRun(
                    id=run_id,
                    pipeline_type=PipelineType.COGNIFY,
                    dataset_id=dataset_id,
                    source_id=None,
                    session_id=None,
                    status=PipelineRunStatus.QUEUED,
                    idempotency_key=None,
                    payload_hash="a" * 64,
                    input={"dataset": dataset_slug, "source_ids": None, "rebuild": False},
                    progress=0.0,
                    current_step=None,
                    attempt=0,
                    worker_id=None,
                    heartbeat_at=None,
                    config_fingerprint="b" * 64,
                    error_code=None,
                    error_message=None,
                    metrics={},
                    started_at=None,
                    finished_at=None,
                )
            )
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            now = await uow.pipeline_runs.get_database_now()
            transition_run(run, PipelineRunStatus.RUNNING, now=now, worker_id="w1")
            transition_run(
                run, PipelineRunStatus.FAILED, now=now, error_code="X", error_message="e"
            )
            await uow.commit()

        async with build_client(app) as client:
            retry_response = await client.post(
                f"/api/v1/runs/{run_id}/retry", headers={API_KEY_HEADER: EXPECTED_API_KEY}
            )
        assert retry_response.status_code == 202

        async with session_factory() as raw_session:
            after_retry = await raw_session.get(SessionModel, created.session_uuid)
        assert after_retry is not None
        assert after_retry.updated_at == baseline_updated_at
    finally:
        await coordinator.stop()


# ==============================================================================
# Restore regression (SM-606 SS 25)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_re_admits_session_entry_append(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    _, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    session_service = SessionService(session_factory=session_factory)
    created = await session_service.create_session(
        SessionCreateRequest(session_id=f"sm606-restore-entry-{uuid4().hex}")
    )
    await session_service.archive_session(created.session_uuid)

    entry_service = SessionEntryService(session_factory=session_factory)
    with pytest.raises(SofiasMemoryError) as exc_info:
        await entry_service.append_entry(
            created.session_uuid,
            SessionEntryCreateRequest(role="user", content="should be rejected"),
        )
    assert exc_info.value.code.value == "SESSION_ARCHIVED"

    await session_service.restore_session(created.session_uuid)

    restored_entry = await entry_service.append_entry(
        created.session_uuid,
        SessionEntryCreateRequest(role="user", content="admitted after restore"),
    )
    assert restored_entry.content == "admitted after restore"

    async with session_factory() as raw_session:
        session_row = await raw_session.get(SessionModel, created.session_uuid)
    assert session_row is not None
    assert session_row.status == SessionStatus.ACTIVE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_re_admits_remember_submission(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"restore-remember-{uuid4()}")

    session_service = SessionService(session_factory=session_factory)
    session_key = f"sm606-restore-remember-{uuid4().hex}"
    created = await session_service.create_session(SessionCreateRequest(session_id=session_key))
    await session_service.archive_session(created.session_uuid)

    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        assert dataset is not None
        dataset_slug_value = dataset.slug

    coordinator = app.state.pipeline_worker
    await coordinator.start()
    try:
        async with build_client(app) as client:
            response = await client.post(
                "/api/v1/remember",
                headers={API_KEY_HEADER: EXPECTED_API_KEY},
                json={
                    "dataset": dataset_slug_value,
                    "content": "should be rejected",
                    "mode": "ingest",
                    "wait": False,
                    "session_id": session_key,
                },
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SESSION_ARCHIVED"

        await session_service.restore_session(created.session_uuid)

        async with build_client(app) as client:
            admitted_response = await client.post(
                "/api/v1/remember",
                headers={API_KEY_HEADER: EXPECTED_API_KEY},
                json={
                    "dataset": dataset_slug_value,
                    "content": "admitted after restore",
                    "mode": "ingest",
                    "wait": False,
                    "session_id": session_key,
                },
            )
        assert admitted_response.status_code == 202
        assert admitted_response.json()["data"]["session_uuid"] == str(created.session_uuid)
    finally:
        await coordinator.stop()


# ==============================================================================
# Neo4j / graph_outbox isolation (SM-606 SS 29-31)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_only_operations_write_zero_graph_outbox_events(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    """SM-606 SS 29/30: Session lifecycle, SessionEntry append, and Query/
    PipelineRun<->Session association write zero graph_outbox rows -- proven
    against the SAME harness that (in the sibling test below) demonstrably
    DOES write graph_outbox rows for genuine knowledge work, so this is not
    a vacuous "nothing ever happens here" harness."""

    app, session_factory, _ = build_harness(postgres_engine, tmp_path, worker_claims=False)
    dataset_id = await seed_dataset(session_factory, slug=f"outbox-session-only-{uuid4()}")

    assert await graph_outbox_row_count(postgres_engine) == 0

    session_service = SessionService(session_factory=session_factory)
    created = await session_service.create_session(
        SessionCreateRequest(session_id=f"sm606-outbox-{uuid4().hex}")
    )
    await SessionEntryService(session_factory=session_factory).append_entry(
        created.session_uuid,
        SessionEntryCreateRequest(role="user", content="no projection expected"),
    )
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.queries.add(
            Query(
                id=uuid4(),
                query_text="no projection expected",
                dataset_ids=[dataset_id],
                mode="chunks",
                answer=None,
                references={"items": []},
                timings={"total": 1},
                model=None,
                session_id=created.session_uuid,
                session_context_entry_ids=[],
            )
        )
        await uow.pipeline_runs.add(
            PipelineRun(
                id=uuid4(),
                pipeline_type=PipelineType.REMEMBER,
                dataset_id=dataset_id,
                source_id=None,
                session_id=created.session_uuid,
                status=PipelineRunStatus.QUEUED,
                idempotency_key=None,
                payload_hash="e" * 64,
                input={},
                progress=0.0,
                current_step=None,
                attempt=0,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="f" * 64,
                error_code=None,
                error_message=None,
                metrics={},
                started_at=None,
                finished_at=None,
            )
        )
        await uow.commit()
    await session_service.archive_session(created.session_uuid)
    await session_service.restore_session(created.session_uuid)

    assert await graph_outbox_row_count(postgres_engine) == 0

    # Sanity: a real worker (same engine/database, a fresh harness with an
    # actually-claiming registry) genuinely produces graph_outbox rows for
    # real knowledge work (mode=full) -- so the zero above is not a vacuous
    # result of a harness that never writes graph_outbox under any
    # circumstance.
    knowledge_app, knowledge_session_factory, _ = build_harness(
        postgres_engine, tmp_path, worker_claims=True
    )
    knowledge_dataset_id = await seed_dataset(
        knowledge_session_factory, slug=f"outbox-knowledge-{uuid4()}"
    )
    async with PostgresUnitOfWork(knowledge_session_factory) as uow:
        knowledge_dataset = await uow.datasets.get_by_id(knowledge_dataset_id)
        assert knowledge_dataset is not None
        knowledge_dataset_slug = knowledge_dataset.slug

    coordinator = knowledge_app.state.pipeline_worker
    await coordinator.start()
    try:
        async with build_client(knowledge_app) as client:
            response = await client.post(
                "/api/v1/remember",
                headers={API_KEY_HEADER: EXPECTED_API_KEY},
                json={
                    "dataset": knowledge_dataset_slug,
                    "content": "PostgreSQL supports Sofias Memory knowledge. " * 5,
                    "mode": "full",
                    "wait": True,
                },
            )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "succeeded"
        assert await graph_outbox_row_count(postgres_engine) > 0
    finally:
        await coordinator.stop()
