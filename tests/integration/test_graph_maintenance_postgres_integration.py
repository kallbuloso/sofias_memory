from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.infrastructure.postgres import (
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RelationEvidenceRepository,
)
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import relation_delete_command
from sofias_memory.services.graph_maintenance_service import GraphMaintenanceService

GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_MAINTENANCE_POSTGRES_TESTS"
GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_URL_ENV = (
    "SOFIAS_MEMORY_GRAPH_MAINTENANCE_TEST_DATABASE_URL"
)
GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_graph_maintenance_test"


def graph_maintenance_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV}=1 to run graph maintenance "
            "PostgreSQL tests"
        )

    database_url = env.get(GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated "
            "discardable PostgreSQL database"
        )

    _validate_graph_maintenance_test_database_url(database_url)
    return database_url


def _validate_graph_maintenance_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("graph maintenance PostgreSQL test database URL is invalid")

    if parsed_url.database != GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "graph maintenance PostgreSQL tests require the exact dedicated database "
            f"{GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = graph_maintenance_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await assert_connected_to_graph_maintenance_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def assert_connected_to_graph_maintenance_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated graph maintenance test database"
        )


def test_graph_maintenance_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_maintenance_test_database_url({})


def test_graph_maintenance_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_maintenance_test_database_url({GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV: "1"})


def test_graph_maintenance_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_maintenance_test_database_url(
            {
                GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV: "1",
                GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


def test_graph_maintenance_postgres_tests_accept_exact_dedicated_database_name() -> None:
    database_url = (
        "postgresql+asyncpg://user:password@localhost:5432/sofias_memory_graph_maintenance_test"
    )

    resolved_url = graph_maintenance_test_database_url(
        {
            GRAPH_MAINTENANCE_POSTGRES_TESTS_ENV: "1",
            GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_URL_ENV: database_url,
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/sofias_memory",
        }
    )

    assert resolved_url == database_url


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authoritative_evidence_query_uses_active_current_dataset_scope(
    postgres_engine: AsyncEngine,
) -> None:
    ids = MaintenanceIds()
    try:
        await insert_authoritative_evidence_fixture(postgres_engine, ids)

        async with create_session_factory(postgres_engine)() as session:
            relation_ids = [
                ids.valid_relation_id,
                ids.inactive_chunk_relation_id,
                ids.stale_chunk_relation_id,
                ids.inactive_document_relation_id,
                ids.inactive_source_relation_id,
                ids.other_dataset_relation_id,
            ]
            result = await RelationEvidenceRepository(
                session
            ).list_relation_ids_with_authoritative_evidence(
                dataset_id=ids.dataset_id,
                relation_ids=relation_ids,
            )

        assert result == {ids.valid_relation_id}
    finally:
        await cleanup_postgres(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_maintenance_postgres_and_outbox_share_transaction(
    postgres_engine: AsyncEngine,
) -> None:
    ids = MaintenanceIds()
    session_factory = create_session_factory(postgres_engine)
    try:
        await insert_transaction_fixture(postgres_engine, ids)

        with pytest.raises(RuntimeError, match="rollback graph maintenance"):
            async with PostgresUnitOfWork(session_factory) as uow:
                relations = await uow.relations.list_active_current_for_dataset(
                    dataset_id=ids.dataset_id
                )
                relation = next(item for item in relations if item.id == ids.valid_relation_id)
                relation.is_active = False
                relation.importance_weight = 0.1
                await uow.graph_outbox.add_projection_command(
                    relation_delete_command(
                        relation_id=relation.id,
                        dataset_id=ids.dataset_id,
                        source_entity_id=relation.source_entity_id,
                        target_entity_id=relation.target_entity_id,
                    )
                )
                raise RuntimeError("rollback graph maintenance")

        assert await relation_is_active(postgres_engine, ids.valid_relation_id) is True
        assert await relation_importance(postgres_engine, ids.valid_relation_id) == 0.5
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == 0

        service = GraphMaintenanceService(session_factory=session_factory)
        result = await service.maintain_dataset(ids.dataset_id, generation=1)

        assert result.relations_deactivated == 1
        assert result.graph_events_enqueued > 0
        assert await relation_is_active(postgres_engine, ids.valid_relation_id) is False
        assert await graph_outbox_count(postgres_engine, ids.dataset_id) == (
            result.graph_events_enqueued
        )
    finally:
        await cleanup_postgres(postgres_engine, ids)


class MaintenanceIds:
    def __init__(self) -> None:
        self.dataset_id = uuid4()
        self.other_dataset_id = uuid4()
        self.source_id = uuid4()
        self.inactive_source_id = uuid4()
        self.other_source_id = uuid4()
        self.document_id = uuid4()
        self.inactive_document_id = uuid4()
        self.stale_document_id = uuid4()
        self.inactive_source_document_id = uuid4()
        self.other_document_id = uuid4()
        self.valid_chunk_id = uuid4()
        self.inactive_chunk_id = uuid4()
        self.stale_chunk_id = uuid4()
        self.inactive_document_chunk_id = uuid4()
        self.inactive_source_chunk_id = uuid4()
        self.other_chunk_id = uuid4()
        self.entity_a_id = uuid4()
        self.entity_b_id = uuid4()
        self.other_entity_a_id = uuid4()
        self.other_entity_b_id = uuid4()
        self.valid_relation_id = uuid4()
        self.inactive_chunk_relation_id = uuid4()
        self.stale_chunk_relation_id = uuid4()
        self.inactive_document_relation_id = uuid4()
        self.inactive_source_relation_id = uuid4()
        self.other_dataset_relation_id = uuid4()


async def insert_authoritative_evidence_fixture(engine: AsyncEngine, ids: MaintenanceIds) -> None:
    await insert_base_fixture(engine, ids)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO relation_evidence (relation_id, chunk_id, quote, confidence)
                VALUES
                (:valid_relation_id, :inactive_chunk_id, 'inactive chunk does not count', 0.7),
                (:valid_relation_id, :valid_chunk_id, 'valid evidence counts', 0.9),
                (:inactive_chunk_relation_id, :inactive_chunk_id, 'inactive chunk', 0.8),
                (:stale_chunk_relation_id, :stale_chunk_id, 'stale chunk', 0.8),
                (:inactive_document_relation_id, :inactive_document_chunk_id, 'inactive doc', 0.8),
                (:inactive_source_relation_id, :inactive_source_chunk_id, 'inactive source', 0.8),
                (:other_dataset_relation_id, :other_chunk_id, 'other dataset', 0.8)
                """
            ),
            _ids_dict(ids),
        )


async def insert_transaction_fixture(engine: AsyncEngine, ids: MaintenanceIds) -> None:
    await insert_base_fixture(engine, ids)


async def insert_base_fixture(engine: AsyncEngine, ids: MaintenanceIds) -> None:
    vector = vector_literal(3072)
    values = _ids_dict(ids) | {"vector": vector}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO datasets (id, name, slug, description, status, active_generation)
                VALUES
                (:dataset_id, :name, :slug, NULL, 'active', 1),
                (:other_dataset_id, :other_name, :other_slug, NULL, 'active', 1)
                """
            ),
            values
            | {
                "name": f"Graph maintenance {ids.dataset_id}",
                "slug": f"graph-maintenance-{ids.dataset_id}",
                "other_name": f"Graph maintenance other {ids.other_dataset_id}",
                "other_slug": f"graph-maintenance-other-{ids.other_dataset_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO sources (
                    id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
                    content_sha256, normalized_sha256, byte_size, metadata, status, version
                )
                VALUES
                (:source_id, :dataset_id, 'text', 'active source', 'text/plain', NULL, NULL,
                    :source_hash, NULL, 1, '{}'::jsonb, 'active', 1),
                (:inactive_source_id, :dataset_id, 'text', 'inactive source', 'text/plain',
                    NULL, NULL, :inactive_source_hash, NULL, 1, '{}'::jsonb, 'pending', 1),
                (:other_source_id, :other_dataset_id, 'text', 'other source', 'text/plain',
                    NULL, NULL, :other_source_hash, NULL, 1, '{}'::jsonb, 'active', 1)
                """
            ),
            values
            | {
                "source_hash": "a" * 64,
                "inactive_source_hash": "b" * 64,
                "other_source_hash": "c" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO documents (
                    id, dataset_id, source_id, generation, title, language, normalized_text,
                    text_sha256, token_count, metadata, is_active
                )
                VALUES
                (:document_id, :dataset_id, :source_id, 1, 'active doc', 'en', 'active',
                    :doc_hash, 1, '{}'::jsonb, TRUE),
                (:inactive_document_id, :dataset_id, :source_id, 1, 'inactive doc', 'en',
                    'inactive', :inactive_doc_hash, 1, '{}'::jsonb, FALSE),
                (:stale_document_id, :dataset_id, :source_id, 0, 'stale doc', 'en', 'stale',
                    :stale_doc_hash, 1, '{}'::jsonb, TRUE),
                (:inactive_source_document_id, :dataset_id, :inactive_source_id, 1,
                    'inactive source doc', 'en', 'inactive source', :inactive_source_doc_hash,
                    1, '{}'::jsonb, TRUE),
                (:other_document_id, :other_dataset_id, :other_source_id, 1, 'other doc',
                    'en', 'other', :other_doc_hash, 1, '{}'::jsonb, TRUE)
                """
            ),
            values
            | {
                "doc_hash": "d" * 64,
                "inactive_doc_hash": "e" * 64,
                "stale_doc_hash": "f" * 64,
                "inactive_source_doc_hash": "1" * 64,
                "other_doc_hash": "2" * 64,
            },
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
                (:valid_chunk_id, :dataset_id, :document_id,  :source_id, 1, 0, 'valid',
                    :valid_chunk_hash, 1, 0, 5, ARRAY[]::text[], '{}'::jsonb,
                    CAST(:vector AS vector), to_tsvector('simple', 'valid'), TRUE),
                (:inactive_chunk_id, :dataset_id, :document_id, :source_id, 1, 1, 'inactive',
                    :inactive_chunk_hash, 1, 0, 8, ARRAY[]::text[], '{}'::jsonb,
                    CAST(:vector AS vector), to_tsvector('simple', 'inactive'), FALSE),
                (:stale_chunk_id, :dataset_id, :stale_document_id, :source_id, 0, 0, 'stale',
                    :stale_chunk_hash, 1, 0, 5, ARRAY[]::text[], '{}'::jsonb,
                    CAST(:vector AS vector), to_tsvector('simple', 'stale'), TRUE),
                (:inactive_document_chunk_id, :dataset_id, :inactive_document_id, :source_id,
                    1, 0, 'inactive doc', :inactive_document_chunk_hash, 1, 0, 12,
                    ARRAY[]::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'inactive doc'), TRUE),
                (:inactive_source_chunk_id, :dataset_id, :inactive_source_document_id,
                    :inactive_source_id, 1, 0, 'inactive source', :inactive_source_chunk_hash,
                    1, 0, 15, ARRAY[]::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'inactive source'), TRUE),
                (:other_chunk_id, :other_dataset_id, :other_document_id, :other_source_id,
                    1, 0, 'other', :other_chunk_hash, 1, 0, 5, ARRAY[]::text[],
                    '{}'::jsonb, CAST(:vector AS vector), to_tsvector('simple', 'other'), TRUE)
                """
            ),
            values
            | {
                "valid_chunk_hash": "3" * 64,
                "inactive_chunk_hash": "4" * 64,
                "stale_chunk_hash": "5" * 64,
                "inactive_document_chunk_hash": "6" * 64,
                "inactive_source_chunk_hash": "7" * 64,
                "other_chunk_hash": "8" * 64,
            },
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
                (:entity_a_id, :dataset_id, 1, :entity_a_key, 'Entity A', 'concept',
                    'A', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE),
                (:entity_b_id, :dataset_id, 1, :entity_b_key, 'Entity B', 'concept',
                    'B', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE),
                (:other_entity_a_id, :other_dataset_id, 1, :other_entity_a_key,
                    'Other A', 'concept', 'A', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:other_entity_b_id, :other_dataset_id, 1, :other_entity_b_key,
                    'Other B', 'concept', 'B', ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5,
                    CAST(:vector AS vector), TRUE)
                """
            ),
            values
            | {
                "entity_a_key": f"entity-a-{ids.dataset_id}",
                "entity_b_key": f"entity-b-{ids.dataset_id}",
                "other_entity_a_key": f"other-a-{ids.other_dataset_id}",
                "other_entity_b_key": f"other-b-{ids.other_dataset_id}",
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
                (:valid_relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id,
                    'relates_to', 'valid', '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE),
                (:inactive_chunk_relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id,
                    'inactive_chunk', 'inactive chunk', '{}'::jsonb, 0.8, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:stale_chunk_relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id,
                    'stale_chunk', 'stale chunk', '{}'::jsonb, 0.8, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:inactive_document_relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id,
                    'inactive_document', 'inactive document', '{}'::jsonb, 0.8, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:inactive_source_relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id,
                    'inactive_source', 'inactive source', '{}'::jsonb, 0.8, 0.5,
                    CAST(:vector AS vector), TRUE),
                (:other_dataset_relation_id, :other_dataset_id, 1, :other_entity_a_id,
                    :other_entity_b_id, 'other', 'other dataset', '{}'::jsonb, 0.8, 0.5,
                    CAST(:vector AS vector), TRUE)
                """
            ),
            values,
        )


async def cleanup_postgres(engine: AsyncEngine, ids: MaintenanceIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM graph_outbox WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text(
                """
                DELETE FROM relation_evidence
                WHERE relation_id IN (
                    :valid_relation_id, :inactive_chunk_relation_id,
                    :stale_chunk_relation_id, :inactive_document_relation_id,
                    :inactive_source_relation_id, :other_dataset_relation_id
                )
                """
            ),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM relations WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM entities WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM chunks WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM documents WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM sources WHERE dataset_id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id IN (:dataset_id, :other_dataset_id)"),
            _ids_dict(ids),
        )


async def relation_is_active(engine: AsyncEngine, relation_id: UUID) -> bool:
    async with engine.connect() as connection:
        return bool(
            await connection.scalar(
                text("SELECT is_active FROM relations WHERE id = :relation_id"),
                {"relation_id": relation_id},
            )
        )


async def relation_importance(engine: AsyncEngine, relation_id: UUID) -> float:
    async with engine.connect() as connection:
        return float(
            await connection.scalar(
                text("SELECT importance_weight FROM relations WHERE id = :relation_id"),
                {"relation_id": relation_id},
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


def _ids_dict(ids: MaintenanceIds) -> dict[str, UUID]:
    return {
        key: value
        for key, value in ids.__dict__.items()
        if key.endswith("_id") or key == "dataset_id"
    }


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join("0.0" for _ in range(dimensions)) + "]"
