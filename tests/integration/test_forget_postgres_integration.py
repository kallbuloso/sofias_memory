from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import PipelineRun
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import chunk_delete_command
from sofias_memory.schemas.forget import ForgetRequest
from sofias_memory.services.forget import (
    ForgetService,
    forget_dataset_run_input,
    stable_payload_hash,
)

FORGET_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_FORGET_POSTGRES_TESTS"
FORGET_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_FORGET_TEST_DATABASE_URL"
FORGET_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_forget_test"


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


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = forget_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await assert_connected_to_forget_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def assert_connected_to_forget_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != FORGET_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated forget test database")


def test_forget_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        forget_test_database_url({})


def test_forget_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        forget_test_database_url({FORGET_POSTGRES_TESTS_ENV: "1"})


def test_forget_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        forget_test_database_url(
            {
                FORGET_POSTGRES_TESTS_ENV: "1",
                FORGET_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


def test_forget_postgres_tests_accept_exact_dedicated_database_name() -> None:
    database_url = "postgresql+asyncpg://user:password@localhost:5432/sofias_memory_forget_test"

    resolved_url = forget_test_database_url(
        {
            FORGET_POSTGRES_TESTS_ENV: "1",
            FORGET_POSTGRES_TEST_DATABASE_URL_ENV: database_url,
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/sofias_memory",
        }
    )

    assert resolved_url == database_url


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_for_update_serializes_forget_initial_mutation(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    first_has_lock = asyncio.Event()
    release_first = asyncio.Event()
    try:
        await insert_forget_fixture(postgres_engine, ids)

        async def first_forget_transaction() -> None:
            async with PostgresUnitOfWork(session_factory) as uow:
                source = await uow.sources.get_by_id_for_update(ids.source_id)
                assert source is not None
                chunks = await uow.chunks.list_for_source_generation(
                    source_id=ids.source_id,
                    generation=1,
                )
                source.status = SourceStatus.DELETING
                chunks[0].is_active = False
                await uow.graph_outbox.add_projection_command(
                    chunk_delete_command(chunk_id=chunks[0].id, dataset_id=ids.dataset_id)
                )
                first_has_lock.set()
                await release_first.wait()
                await uow.commit()

        async def second_forget_transaction() -> SourceStatus:
            await first_has_lock.wait()
            async with PostgresUnitOfWork(session_factory) as uow:
                source = await uow.sources.get_by_id_for_update(ids.source_id)
                assert source is not None
                observed_status = source.status
                if source.status == SourceStatus.ACTIVE:
                    chunks = await uow.chunks.list_for_source_generation(
                        source_id=ids.source_id,
                        generation=1,
                    )
                    source.status = SourceStatus.DELETING
                    chunks[0].is_active = False
                    await uow.graph_outbox.add_projection_command(
                        chunk_delete_command(chunk_id=chunks[0].id, dataset_id=ids.dataset_id)
                    )
                await uow.commit()
                return observed_status

        first_task = asyncio.create_task(first_forget_transaction())
        await first_has_lock.wait()
        second_task = asyncio.create_task(second_forget_transaction())
        await asyncio.sleep(0.1)

        assert not second_task.done()
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == 0

        release_first.set()
        observed_status = await asyncio.wait_for(second_task, timeout=5)
        await asyncio.wait_for(first_task, timeout=5)

        assert observed_status == SourceStatus.DELETING
        assert await source_status(postgres_engine, ids.source_id) == "deleting"
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == 1
    finally:
        release_first.set()
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reentrant_forget_skips_concurrent_post_commit_drain(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    class BlockingDrain:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def process_dataset(self, dataset_id: UUID) -> SimpleNamespace:
            del dataset_id
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return SimpleNamespace(processed=0)

    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    drain = BlockingDrain()
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key="sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url=str(postgres_engine.url),
        neo4j_password="test-neo4j-password",
        llm_api_key="test-llm-key",
        app_env="test",
        data_directory=tmp_path,
    )
    service = ForgetService(
        settings,
        session_factory=session_factory,
        graph_projection_drain=drain,
    )
    request = ForgetRequest(
        dataset=f"forget-{ids.dataset_id}",
        source_id=ids.source_id,
        memory_only=True,
    )
    try:
        await insert_forget_fixture(postgres_engine, ids)

        first = asyncio.create_task(service.forget_source(request))
        await asyncio.wait_for(drain.entered.wait(), timeout=5)
        outbox_after_first_commit = await graph_outbox_count(postgres_engine, ids.dataset_id)

        reentrant = await asyncio.wait_for(service.forget_source(request), timeout=5)

        assert reentrant.source_status == SourceStatus.DELETING
        assert reentrant.graph_events_enqueued == 0
        assert reentrant.graph_events_processed == 0
        assert drain.calls == 1
        assert (
            await graph_outbox_count(postgres_engine, ids.dataset_id) == outbox_after_first_commit
        )

        drain.release.set()
        owner = await asyncio.wait_for(first, timeout=5)

        assert owner.source_status == SourceStatus.PENDING
        assert await source_status(postgres_engine, ids.source_id) == "pending"
        assert (
            await graph_outbox_count(postgres_engine, ids.dataset_id) == outbox_after_first_commit
        )
    finally:
        drain.release.set()
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_orphan_detection_uses_authoritative_current_scope(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ForgetIds()
    try:
        await insert_forget_fixture(postgres_engine, ids)
        session_factory = create_session_factory(postgres_engine)

        async with PostgresUnitOfWork(session_factory) as uow:
            source = await uow.sources.get_by_id(ids.source_id)
            assert source is not None
            source.status = SourceStatus.DELETING
            documents = await uow.documents.list_for_source_generation(
                source_id=ids.source_id,
                generation=1,
            )
            chunks = await uow.chunks.list_for_source_generation(
                source_id=ids.source_id,
                generation=1,
            )
            for document in documents:
                document.is_active = False
            for chunk in chunks:
                chunk.is_active = False
            await uow.flush()

            valid_relations = (
                await uow.relation_evidence.list_relation_ids_with_authoritative_evidence(
                    dataset_id=ids.dataset_id,
                    relation_ids=[ids.shared_relation_id, ids.orphan_relation_id],
                )
            )
            valid_entities = await uow.entity_mentions.list_entity_ids_with_authoritative_mentions(
                dataset_id=ids.dataset_id,
                entity_ids=[ids.shared_entity_id, ids.orphan_entity_id],
            )

        assert valid_relations == {ids.shared_relation_id}
        assert valid_entities == {ids.shared_entity_id}
    finally:
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_authoritative_state_and_outbox_share_transaction(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    try:
        await insert_forget_fixture(postgres_engine, ids)

        with pytest.raises(RuntimeError, match="rollback forget"):
            async with PostgresUnitOfWork(session_factory) as uow:
                source = await uow.sources.get_by_id(ids.source_id)
                assert source is not None
                chunks = await uow.chunks.list_for_source_generation(
                    source_id=ids.source_id,
                    generation=1,
                )
                source.status = SourceStatus.DELETING
                chunks[0].is_active = False
                await uow.graph_outbox.add_projection_command(
                    chunk_delete_command(chunk_id=chunks[0].id, dataset_id=ids.dataset_id)
                )
                raise RuntimeError("rollback forget")

        assert await source_status(postgres_engine, ids.source_id) == "active"
        assert await chunk_is_active(postgres_engine, ids.forgotten_chunk_id) is True
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == 0

        async with PostgresUnitOfWork(session_factory) as uow:
            source = await uow.sources.get_by_id(ids.source_id)
            assert source is not None
            chunks = await uow.chunks.list_for_source_generation(
                source_id=ids.source_id,
                generation=1,
            )
            source.status = SourceStatus.DELETING
            chunks[0].is_active = False
            await uow.graph_outbox.add_projection_command(
                chunk_delete_command(chunk_id=chunks[0].id, dataset_id=ids.dataset_id)
            )
            await uow.commit()

        assert await source_status(postgres_engine, ids.source_id) == "deleting"
        assert await chunk_is_active(postgres_engine, ids.forgotten_chunk_id) is False
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == 1
    finally:
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_missing_source_returns_404_without_pipeline_run_fk_violation(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Regression: pipeline_runs.source_id has a real FK to sources.id in PostgreSQL.

    A source_id that was never persisted must be rejected before any PipelineRun
    row references it, or the FK violation surfaces as an unhandled 500 instead
    of the documented 404.
    """

    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key="sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url=str(postgres_engine.url),
        neo4j_password="test-neo4j-password",
        llm_api_key="test-llm-key",
        app_env="test",
        data_directory=tmp_path,
    )
    service = ForgetService(
        settings,
        session_factory=session_factory,
        graph_projection_drain=SimpleNamespace(process_dataset=_unreachable_drain),
    )
    missing_source_id = uuid4()
    try:
        await insert_forget_fixture(postgres_engine, ids)

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.forget_source(
                ForgetRequest(dataset=f"forget-{ids.dataset_id}", source_id=missing_source_id)
            )

        assert exc_info.value.status_code == 404
        async with postgres_engine.connect() as connection:
            run_count = await connection.scalar(
                text("SELECT count(*) FROM pipeline_runs WHERE source_id = :source_id"),
                {"source_id": missing_source_id},
            )
        assert run_count == 0
    finally:
        await cleanup_forget_fixture(postgres_engine, ids)


async def _unreachable_drain(dataset_id: UUID) -> SimpleNamespace:
    raise AssertionError(f"drain must not run for a rejected target: {dataset_id}")


def _settings_for(postgres_engine: AsyncEngine, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key="sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url=str(postgres_engine.url),
        neo4j_password="test-neo4j-password",
        llm_api_key="test-llm-key",
        app_env="test",
        data_directory=tmp_path,
    )


async def insert_pipeline_run(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: UUID | None,
    source_id: UUID | None,
    status: PipelineRunStatus,
    payload_hash: str,
) -> UUID:
    run_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.pipeline_runs.add(
            PipelineRun(
                id=run_id,
                pipeline_type=PipelineType.FORGET,
                dataset_id=dataset_id,
                source_id=source_id,
                status=status,
                idempotency_key=None,
                payload_hash=payload_hash,
                input={},
                progress=0.5,
                current_step="forget",
                attempt=1,
                worker_id=None,
                heartbeat_at=None,
                config_fingerprint="a" * 64,
                error_code=None,
                error_message=None,
                metrics={},
                started_at=None,
                finished_at=None,
            )
        )
        await uow.commit()
    return run_id


async def dataset_status(engine: AsyncEngine, dataset_id: UUID) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                text("SELECT status FROM datasets WHERE id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_reentrant_skips_concurrent_post_commit_drain(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Scenario A: two concurrent, identical dataset forgets apply the
    authoritative mutation exactly once; the second call observes the first's
    RUNNING PipelineRun and completes as a reentrant no-op instead of
    disputing drain with it."""

    class BlockingDrain:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def process_dataset(self, dataset_id: UUID) -> SimpleNamespace:
            del dataset_id
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return SimpleNamespace(processed=0)

    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    drain = BlockingDrain()
    service = ForgetService(
        _settings_for(postgres_engine, tmp_path),
        session_factory=session_factory,
        graph_projection_drain=drain,
    )
    request = ForgetRequest(dataset=f"forget-{ids.dataset_id}", memory_only=True)
    try:
        await insert_forget_fixture(postgres_engine, ids)

        first = asyncio.create_task(service.forget_dataset(request))
        await asyncio.wait_for(drain.entered.wait(), timeout=5)
        outbox_after_first_commit = await graph_outbox_count(postgres_engine, ids.dataset_id)
        assert outbox_after_first_commit > 0

        reentrant = await asyncio.wait_for(service.forget_dataset(request), timeout=5)

        assert reentrant.graph_events_enqueued == 0
        assert reentrant.graph_events_processed == 0
        assert drain.calls == 1
        assert (
            await graph_outbox_count(postgres_engine, ids.dataset_id) == outbox_after_first_commit
        )
        assert await dataset_status(postgres_engine, ids.dataset_id) == "deleting"

        drain.release.set()
        owner = await asyncio.wait_for(first, timeout=5)

        assert owner.status == "succeeded"
        assert await dataset_status(postgres_engine, ids.dataset_id) == "active"
        assert (
            await graph_outbox_count(postgres_engine, ids.dataset_id) == outbox_after_first_commit
        )
    finally:
        drain.release.set()
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_blocked_by_running_source_forget_post_commit(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Scenario B: a source-scoped forget's mutation already committed and its
    PipelineRun is still RUNNING (mid drain/storage/finalize). A dataset
    forget on the same dataset must detect this before touching anything,
    even though the dataset row itself is still `active`."""

    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    try:
        await insert_forget_fixture(postgres_engine, ids)

        # Simulate a source forget's committed mutation: the source is
        # `deleting`, its owning PipelineRun is still RUNNING, and the
        # dataset row was never touched (mirrors real source-forget behavior).
        async with PostgresUnitOfWork(session_factory) as uow:
            source = await uow.sources.get_by_id(ids.source_id)
            assert source is not None
            source.status = SourceStatus.DELETING
            await uow.commit()
        await insert_pipeline_run(
            session_factory,
            dataset_id=ids.dataset_id,
            source_id=ids.source_id,
            status=PipelineRunStatus.RUNNING,
            payload_hash="b" * 64,
        )

        outbox_before = await graph_outbox_count(postgres_engine, ids.dataset_id)
        service = ForgetService(
            _settings_for(postgres_engine, tmp_path),
            session_factory=session_factory,
            graph_projection_drain=SimpleNamespace(process_dataset=_unreachable_drain),
        )

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.forget_dataset(
                ForgetRequest(dataset=f"forget-{ids.dataset_id}", memory_only=False)
            )

        assert exc_info.value.status_code == 409
        assert await dataset_status(postgres_engine, ids.dataset_id) == "active"
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == outbox_before
    finally:
        await cleanup_forget_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_retry_after_failed_owner_preserves_intent(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Scenario C: a dataset left `deleting` by a FAILED attempt may only be
    resumed by a request with the same persisted intent; a divergent
    ``memory_only`` must be rejected instead of silently taking over."""

    ids = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    full_request = ForgetRequest(dataset=f"forget-{ids.dataset_id}", memory_only=False)
    try:
        await insert_forget_fixture(postgres_engine, ids)

        async with PostgresUnitOfWork(session_factory) as uow:
            dataset = await uow.datasets.get_by_id(ids.dataset_id)
            assert dataset is not None
            dataset.status = DatasetStatus.DELETING
            source1 = await uow.sources.get_by_id(ids.source_id)
            source2 = await uow.sources.get_by_id(ids.other_source_id)
            assert source1 is not None
            assert source2 is not None
            source1.status = SourceStatus.DELETING
            source2.status = SourceStatus.DELETING
            await uow.commit()
        await insert_pipeline_run(
            session_factory,
            dataset_id=ids.dataset_id,
            source_id=None,
            status=PipelineRunStatus.FAILED,
            payload_hash=stable_payload_hash(forget_dataset_run_input(full_request)),
        )

        service = ForgetService(
            _settings_for(postgres_engine, tmp_path),
            session_factory=session_factory,
            graph_projection_drain=SimpleNamespace(process_dataset=_unreachable_drain),
        )

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.forget_dataset(
                ForgetRequest(dataset=f"forget-{ids.dataset_id}", memory_only=True)
            )
        assert exc_info.value.status_code == 409
        assert await dataset_status(postgres_engine, ids.dataset_id) == "deleting"

        service_with_real_drain = ForgetService(
            _settings_for(postgres_engine, tmp_path),
            session_factory=session_factory,
            graph_projection_drain=SimpleNamespace(
                process_dataset=lambda dataset_id: _noop_drain_result()
            ),
        )
        result = await service_with_real_drain.forget_dataset(full_request)

        assert result.status == "succeeded"
        assert await dataset_status(postgres_engine, ids.dataset_id) == "active"
    finally:
        await cleanup_forget_fixture(postgres_engine, ids)


async def _noop_drain_result() -> SimpleNamespace:
    return SimpleNamespace(processed=0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_forget_does_not_affect_other_dataset(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Scenario D: forgetting dataset A must never mutate dataset B's rows."""

    ids_a = ForgetIds()
    ids_b = ForgetIds()
    session_factory = create_session_factory(postgres_engine)
    service = ForgetService(
        _settings_for(postgres_engine, tmp_path),
        session_factory=session_factory,
        graph_projection_drain=SimpleNamespace(
            process_dataset=lambda dataset_id: _noop_drain_result()
        ),
    )
    try:
        await insert_forget_fixture(postgres_engine, ids_a)
        await insert_forget_fixture(postgres_engine, ids_b)

        result = await service.forget_dataset(
            ForgetRequest(dataset=f"forget-{ids_a.dataset_id}", memory_only=False)
        )

        assert result.status == "succeeded"
        assert await dataset_status(postgres_engine, ids_a.dataset_id) == "active"
        assert await source_status(postgres_engine, ids_a.source_id) == "deleted"
        assert await dataset_status(postgres_engine, ids_b.dataset_id) == "active"
        assert await source_status(postgres_engine, ids_b.source_id) == "active"
        assert await chunk_is_active(postgres_engine, ids_b.forgotten_chunk_id) is True
    finally:
        await cleanup_forget_fixture(postgres_engine, ids_a)
        await cleanup_forget_fixture(postgres_engine, ids_b)


class ForgetIds:
    def __init__(self) -> None:
        self.dataset_id = uuid4()
        self.source_id = uuid4()
        self.other_source_id = uuid4()
        self.document_id = uuid4()
        self.other_document_id = uuid4()
        self.forgotten_chunk_id = uuid4()
        self.other_chunk_id = uuid4()
        self.shared_entity_id = uuid4()
        self.orphan_entity_id = uuid4()
        self.target_entity_id = uuid4()
        self.shared_relation_id = uuid4()
        self.orphan_relation_id = uuid4()


async def insert_forget_fixture(engine: AsyncEngine, ids: ForgetIds) -> None:
    vector = vector_literal(3072)
    values = _ids_dict(ids) | {"vector": vector}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO datasets (id, name, slug, description, status, active_generation)
                VALUES (:dataset_id, :name, :slug, NULL, 'active', 1)
                """
            ),
            values | {"name": f"Forget {ids.dataset_id}", "slug": f"forget-{ids.dataset_id}"},
        )
        await connection.execute(
            text(
                """
                INSERT INTO sources (
                    id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
                    content_sha256, normalized_sha256, byte_size, metadata, status, version
                )
                VALUES
                (:source_id, :dataset_id, 'text', 'forgotten source', 'text/plain', NULL, NULL,
                    :source_hash, NULL, 1, '{}'::jsonb, 'active', 1),
                (:other_source_id, :dataset_id, 'text', 'other source', 'text/plain', NULL, NULL,
                    :other_source_hash, NULL, 1, '{}'::jsonb, 'active', 1)
                """
            ),
            values | {"source_hash": "a" * 64, "other_source_hash": "b" * 64},
        )
        await connection.execute(
            text(
                """
                INSERT INTO documents (
                    id, dataset_id, source_id, generation, title, language, normalized_text,
                    text_sha256, token_count, metadata, is_active
                )
                VALUES
                (:document_id, :dataset_id, :source_id, 1, 'forgotten doc', 'en', 'forgotten',
                    :doc_hash, 1, '{}'::jsonb, TRUE),
                (:other_document_id, :dataset_id, :other_source_id, 1, 'other doc', 'en',
                    'other', :other_doc_hash, 1, '{}'::jsonb, TRUE)
                """
            ),
            values | {"doc_hash": "c" * 64, "other_doc_hash": "d" * 64},
        )
        await connection.execute(
            text(
                """
                INSERT INTO chunks (
                    id, dataset_id, document_id, source_id, generation, ordinal, text,
                    content_sha256, token_count, start_char, end_char, section_path, metadata,
                    embedding, lexical, is_active
                )
                VALUES
                (:forgotten_chunk_id, :dataset_id, :document_id, :source_id, 1, 0, 'forgotten',
                    :forgotten_chunk_hash, 1, 0, 9, ARRAY[]::text[], '{}'::jsonb,
                    CAST(:vector AS vector), to_tsvector('simple', 'forgotten'), TRUE),
                (:other_chunk_id, :dataset_id, :other_document_id, :other_source_id, 1, 0,
                    'other', :other_chunk_hash, 1, 0, 5, ARRAY[]::text[], '{}'::jsonb,
                    CAST(:vector AS vector), to_tsvector('simple', 'other'), TRUE)
                """
            ),
            values | {"forgotten_chunk_hash": "e" * 64, "other_chunk_hash": "f" * 64},
        )
        await connection.execute(
            text(
                """
                INSERT INTO entities (
                    id, dataset_id, generation, canonical_key, name, entity_type,
                    description, aliases, properties, confidence, importance_weight,
                    embedding, is_active
                )
                VALUES
                (:shared_entity_id, :dataset_id, 1, :shared_key, 'Shared', 'concept',
                    'Shared', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:orphan_entity_id, :dataset_id, 1, :orphan_key, 'Orphan', 'concept',
                    'Orphan', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:target_entity_id, :dataset_id, 1, :target_key, 'Target', 'concept',
                    'Target', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5,
                    CAST(:vector AS vector), TRUE)
                """
            ),
            values
            | {
                "shared_key": f"shared-{ids.dataset_id}",
                "orphan_key": f"orphan-{ids.dataset_id}",
                "target_key": f"target-{ids.dataset_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, chunk_id, surface_text, start_char, end_char, confidence
                )
                VALUES
                (:shared_mention_id, :shared_entity_id, :forgotten_chunk_id, 'Shared', 0, 6, 0.9),
                (:shared_other_mention_id, :shared_entity_id, :other_chunk_id, 'Shared', 0, 6, 0.9),
                (:orphan_mention_id, :orphan_entity_id, :forgotten_chunk_id, 'Orphan', 0, 6, 0.9)
                """
            ),
            values
            | {
                "shared_mention_id": uuid4(),
                "shared_other_mention_id": uuid4(),
                "orphan_mention_id": uuid4(),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO relations (
                    id, dataset_id, generation, source_entity_id, target_entity_id,
                    predicate, description, properties, confidence, importance_weight,
                    embedding, is_active
                )
                VALUES
                (:shared_relation_id, :dataset_id, 1, :shared_entity_id, :target_entity_id,
                    'shared', 'shared', '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE),
                (:orphan_relation_id, :dataset_id, 1, :orphan_entity_id, :shared_entity_id,
                    'orphan', 'orphan', '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE)
                """
            ),
            values,
        )
        await connection.execute(
            text(
                """
                INSERT INTO relation_evidence (relation_id, chunk_id, quote, confidence)
                VALUES
                (:shared_relation_id, :forgotten_chunk_id, 'forgotten shared', 0.8),
                (:shared_relation_id, :other_chunk_id, 'remaining shared', 0.9),
                (:orphan_relation_id, :forgotten_chunk_id, 'orphan', 0.8)
                """
            ),
            values,
        )


async def cleanup_forget_fixture(engine: AsyncEngine, ids: ForgetIds) -> None:
    async with engine.begin() as connection:
        # pipeline_runs.dataset_id/source_id use ON DELETE SET NULL, not
        # CASCADE: deleting the dataset first would leave any test-inserted
        # run orphaned with NULL/NULL, which then matches the
        # everything-scope wildcard in find_running_forget_for_dataset_except
        # and silently poisons every later test in the same session. Always
        # delete pipeline_runs for this fixture's identities first.
        await connection.execute(
            text(
                """
                DELETE FROM pipeline_runs
                WHERE dataset_id = :dataset_id
                   OR source_id IN (:source_id, :other_source_id)
                """
            ),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM graph_outbox WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text(
                """
                DELETE FROM relation_evidence
                WHERE relation_id IN (:shared_relation_id, :orphan_relation_id)
                """
            ),
            _ids_dict(ids),
        )
        await connection.execute(
            text(
                """
                DELETE FROM entity_mentions
                WHERE chunk_id IN (:forgotten_chunk_id, :other_chunk_id)
                """
            ),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM relations WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM entities WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM chunks WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM documents WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM sources WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )


async def source_status(engine: AsyncEngine, source_id: UUID) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                text("SELECT status FROM sources WHERE id = :source_id"),
                {"source_id": source_id},
            )
        )


async def chunk_is_active(engine: AsyncEngine, chunk_id: UUID) -> bool:
    async with engine.connect() as connection:
        return bool(
            await connection.scalar(
                text("SELECT is_active FROM chunks WHERE id = :chunk_id"),
                {"chunk_id": chunk_id},
            )
        )


async def graph_outbox_count(engine: AsyncEngine, dataset_id: UUID) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text("SELECT count(*) FROM graph_outbox WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
        )


def _ids_dict(ids: ForgetIds) -> dict[str, UUID]:
    return {
        key: value
        for key, value in ids.__dict__.items()
        if key.endswith("_id") or key == "dataset_id"
    }


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join("0.0" for _ in range(dimensions)) + "]"
