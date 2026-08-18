"""Opt-in PostgreSQL + Neo4j integration tests for SM-424.

Mirrors the safety pattern already established by
``tests/integration/test_forget_postgres_integration.py``: a dedicated,
name-checked PostgreSQL database, and Neo4j scoped exclusively by test-owned
UUIDs (never a global wipe). PostgreSQL is always authoritative; Neo4j is
consulted only through the real read-only ``Neo4jGraphRead`` traversal.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    Neo4jResource,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.neo4j.graph_read import Neo4jGraphRead
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.ports import entity_upsert_command, relation_upsert_command
from sofias_memory.services.graph_read import GraphReadService
from sofias_memory.services.provenance import ProvenanceService

GRAPH_PROVENANCE_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_PROVENANCE_TESTS"
GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_GRAPH_PROVENANCE_TEST_DATABASE_URL"
GRAPH_PROVENANCE_TEST_DATABASE_NAME = "sofias_memory_graph_provenance_test"
FORBIDDEN_DATABASE_NAMES = frozenset({"cognee_db", "sofias_memory_forget_test"})

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def graph_provenance_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(GRAPH_PROVENANCE_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_PROVENANCE_TESTS_ENV}=1 to run graph/provenance integration tests")

    database_url = env.get(GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_graph_provenance_test_database_url(database_url)
    return database_url


def _validate_graph_provenance_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("graph/provenance test database URL is invalid")

    if parsed_url.database in FORBIDDEN_DATABASE_NAMES:
        pytest.skip(
            "graph/provenance integration tests must never target "
            f"{sorted(FORBIDDEN_DATABASE_NAMES)}"
        )
    if parsed_url.database != GRAPH_PROVENANCE_TEST_DATABASE_NAME:
        pytest.skip(
            "graph/provenance integration tests require the exact dedicated database "
            f"{GRAPH_PROVENANCE_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = graph_provenance_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_dedicated_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_dedicated_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database in FORBIDDEN_DATABASE_NAMES:
        pytest.skip("refusing to run against a protected, non-dedicated PostgreSQL database")
    if current_database != GRAPH_PROVENANCE_TEST_DATABASE_NAME:
        pytest.skip("connected PostgreSQL database is not the dedicated graph/provenance database")


@pytest_asyncio.fixture()
async def neo4j_resource() -> AsyncIterator[Neo4jResource]:
    if os.environ.get(GRAPH_PROVENANCE_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_PROVENANCE_TESTS_ENV}=1 to run graph/provenance integration tests")

    resource = create_neo4j_resource_from_settings(load_settings())
    try:
        yield resource
    finally:
        await resource.close()


def test_graph_provenance_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_provenance_test_database_url({})


def test_graph_provenance_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_provenance_test_database_url({GRAPH_PROVENANCE_TESTS_ENV: "1"})


def test_graph_provenance_tests_reject_cognee_db() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_provenance_test_database_url(
            {
                GRAPH_PROVENANCE_TESTS_ENV: "1",
                GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/cognee_db"
                ),
            }
        )


def test_graph_provenance_tests_reject_forget_test_database() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_provenance_test_database_url(
            {
                GRAPH_PROVENANCE_TESTS_ENV: "1",
                GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory_forget_test"
                ),
            }
        )


def test_graph_provenance_tests_accept_exact_dedicated_database_name() -> None:
    database_url = (
        "postgresql+asyncpg://user:password@localhost:5432/sofias_memory_graph_provenance_test"
    )

    resolved_url = graph_provenance_test_database_url(
        {
            GRAPH_PROVENANCE_TESTS_ENV: "1",
            GRAPH_PROVENANCE_TEST_DATABASE_URL_ENV: database_url,
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/cognee_db",
        }
    )

    assert resolved_url == database_url


# --------------------------------------------------------------------------
# Fixture identities and seeding
# --------------------------------------------------------------------------


class GraphProvenanceIds:
    """Fresh, exclusive UUIDs for one test run. Never reused across tests."""

    def __init__(self) -> None:
        self.dataset_a_id = uuid4()
        self.dataset_b_id = uuid4()
        self.source_id = uuid4()
        self.document_id = uuid4()
        self.chunk1_id = uuid4()
        self.chunk2_id = uuid4()
        self.entity_a1_id = uuid4()
        self.entity_a2_id = uuid4()
        self.entity_a3_id = uuid4()
        self.relation_a1_a2_id = uuid4()
        self.relation_a2_a3_id = uuid4()
        self.mention_a1_id = uuid4()
        self.mention_a2_id = uuid4()
        self.mention_a3_id = uuid4()
        self.query_id = uuid4()
        self.source_b_id = uuid4()
        self.document_b_id = uuid4()
        self.chunk_b_id = uuid4()
        self.entity_b1_id = uuid4()


def vector_literal(dimensions: int = 3072) -> str:
    return "[" + ",".join("0.0" for _ in range(dimensions)) + "]"


def _hash(seed: str) -> str:
    return (seed * 64)[:64]


async def insert_postgres_fixture(
    engine: AsyncEngine, ids: GraphProvenanceIds, *, storage_uri: str
) -> None:
    vector = vector_literal()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO datasets (id, name, slug, description, status, active_generation)
                VALUES
                (:dataset_a_id, :name_a, :slug_a, NULL, 'active', 1),
                (:dataset_b_id, :name_b, :slug_b, NULL, 'active', 1)
                """
            ),
            {
                "dataset_a_id": ids.dataset_a_id,
                "name_a": f"Graph provenance A {ids.dataset_a_id}",
                "slug_a": f"graph-provenance-a-{ids.dataset_a_id}",
                "dataset_b_id": ids.dataset_b_id,
                "name_b": f"Graph provenance B {ids.dataset_b_id}",
                "slug_b": f"graph-provenance-b-{ids.dataset_b_id}",
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
                (:source_id, :dataset_a_id, 'text', 'source A', 'text/plain', NULL,
                    :storage_uri, :source_hash, NULL, 42, '{}'::jsonb, 'active', 1),
                (:source_b_id, :dataset_b_id, 'text', 'source B', 'text/plain', NULL, NULL,
                    :source_b_hash, NULL, 7, '{}'::jsonb, 'active', 1)
                """
            ),
            {
                "source_id": ids.source_id,
                "dataset_a_id": ids.dataset_a_id,
                "storage_uri": storage_uri,
                "source_hash": _hash("a"),
                "source_b_id": ids.source_b_id,
                "dataset_b_id": ids.dataset_b_id,
                "source_b_hash": _hash("b"),
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
                (:document_id, :dataset_a_id, :source_id, 1, 'Document A', 'en', 'content a',
                    :doc_hash, 10, '{}'::jsonb, TRUE),
                (:document_b_id, :dataset_b_id, :source_b_id, 1, 'Document B', 'en', 'content b',
                    :doc_b_hash, 5, '{}'::jsonb, TRUE)
                """
            ),
            {
                "document_id": ids.document_id,
                "dataset_a_id": ids.dataset_a_id,
                "source_id": ids.source_id,
                "doc_hash": _hash("c"),
                "document_b_id": ids.document_b_id,
                "dataset_b_id": ids.dataset_b_id,
                "source_b_id": ids.source_b_id,
                "doc_b_hash": _hash("d"),
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
                (:chunk1_id, :dataset_a_id, :document_id, :source_id, 1, 0,
                    'Ada Lovelace worked with Charles Babbage.', :chunk1_hash, 8, 0, 42,
                    ARRAY[]::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'ada lovelace charles babbage'), TRUE),
                (:chunk2_id, :dataset_a_id, :document_id, :source_id, 1, 1,
                    'Charles Babbage designed the Analytical Engine.', :chunk2_hash, 8, 43, 91,
                    ARRAY[]::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'charles babbage analytical engine'), TRUE),
                (:chunk_b_id, :dataset_b_id, :document_b_id, :source_b_id, 1, 0,
                    'Isolated dataset B chunk.', :chunk_b_hash, 4, 0, 25, ARRAY[]::text[],
                    '{}'::jsonb, CAST(:vector AS vector), to_tsvector('simple', 'isolated'), TRUE)
                """
            ),
            {
                "chunk1_id": ids.chunk1_id,
                "dataset_a_id": ids.dataset_a_id,
                "document_id": ids.document_id,
                "source_id": ids.source_id,
                "chunk1_hash": _hash("e"),
                "chunk2_id": ids.chunk2_id,
                "chunk2_hash": _hash("f"),
                "chunk_b_id": ids.chunk_b_id,
                "dataset_b_id": ids.dataset_b_id,
                "document_b_id": ids.document_b_id,
                "source_b_id": ids.source_b_id,
                "chunk_b_hash": _hash("1"),
                "vector": vector,
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
                (:a1, :dataset_a_id, 1, :a1_key, 'Ada Lovelace', 'person', 'Mathematician',
                    ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE),
                (:a2, :dataset_a_id, 1, :a2_key, 'Charles Babbage', 'person', 'Inventor',
                    ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE),
                (:a3, :dataset_a_id, 1, :a3_key, 'Analytical Engine', 'concept', 'Machine',
                    ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE),
                (:b1, :dataset_b_id, 1, :b1_key, 'Other Entity', 'concept', 'Unrelated',
                    ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE)
                """
            ),
            {
                "a1": ids.entity_a1_id,
                "a2": ids.entity_a2_id,
                "a3": ids.entity_a3_id,
                "b1": ids.entity_b1_id,
                "dataset_a_id": ids.dataset_a_id,
                "dataset_b_id": ids.dataset_b_id,
                "a1_key": f"a1-{ids.entity_a1_id}",
                "a2_key": f"a2-{ids.entity_a2_id}",
                "a3_key": f"a3-{ids.entity_a3_id}",
                "b1_key": f"b1-{ids.entity_b1_id}",
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, chunk_id, surface_text, start_char, end_char, confidence
                )
                VALUES
                (:m1, :a1, :chunk1_id, 'Ada Lovelace', 0, 12, 0.9),
                (:m2, :a2, :chunk1_id, 'Charles Babbage', 18, 33, 0.9),
                (:m3, :a3, :chunk2_id, 'Analytical Engine', 30, 47, 0.9)
                """
            ),
            {
                "m1": ids.mention_a1_id,
                "m2": ids.mention_a2_id,
                "m3": ids.mention_a3_id,
                "a1": ids.entity_a1_id,
                "a2": ids.entity_a2_id,
                "a3": ids.entity_a3_id,
                "chunk1_id": ids.chunk1_id,
                "chunk2_id": ids.chunk2_id,
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
                (:r12, :dataset_a_id, 1, :a1, :a2, 'worked_with', 'Worked together',
                    '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE),
                (:r23, :dataset_a_id, 1, :a2, :a3, 'designed', 'Designed the machine',
                    '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE)
                """
            ),
            {
                "r12": ids.relation_a1_a2_id,
                "r23": ids.relation_a2_a3_id,
                "dataset_a_id": ids.dataset_a_id,
                "a1": ids.entity_a1_id,
                "a2": ids.entity_a2_id,
                "a3": ids.entity_a3_id,
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO relation_evidence (relation_id, chunk_id, quote, confidence)
                VALUES
                (:r12, :chunk1_id, 'Ada Lovelace worked with Charles Babbage.', 0.8),
                (:r23, :chunk2_id, 'Charles Babbage designed the Analytical Engine.', 0.85)
                """
            ),
            {
                "r12": ids.relation_a1_a2_id,
                "r23": ids.relation_a2_a3_id,
                "chunk1_id": ids.chunk1_id,
                "chunk2_id": ids.chunk2_id,
            },
        )
        references_payload = json.dumps(
            {
                "items": [
                    {
                        "source_id": str(ids.source_id),
                        "document_id": str(ids.document_id),
                        "chunk_id": str(ids.chunk1_id),
                        "chunk_ordinal": 0,
                        "score": 0.9,
                    }
                ]
            }
        )
        await connection.execute(
            text(
                """
                INSERT INTO queries (
                    id, query_text, dataset_ids, mode, answer, "references", timings, model
                )
                VALUES
                (:query_id, 'Who worked with Charles Babbage?', ARRAY[:dataset_a_id]::uuid[],
                    'rag', 'Ada Lovelace.', CAST(:references AS jsonb), '{}'::jsonb, 'test-model')
                """
            ),
            {
                "query_id": ids.query_id,
                "dataset_a_id": ids.dataset_a_id,
                "references": references_payload,
            },
        )


async def cleanup_postgres_fixture(engine: AsyncEngine, ids: GraphProvenanceIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM queries WHERE id = :query_id"), {"query_id": ids.query_id}
        )
        await connection.execute(
            text("DELETE FROM relation_evidence WHERE relation_id IN (:r12, :r23)"),
            {"r12": ids.relation_a1_a2_id, "r23": ids.relation_a2_a3_id},
        )
        await connection.execute(
            text("DELETE FROM entity_mentions WHERE chunk_id IN (:chunk1_id, :chunk2_id)"),
            {"chunk1_id": ids.chunk1_id, "chunk2_id": ids.chunk2_id},
        )
        for dataset_id in (ids.dataset_a_id, ids.dataset_b_id):
            await connection.execute(
                text("DELETE FROM relations WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
            await connection.execute(
                text("DELETE FROM entities WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
            await connection.execute(
                text("DELETE FROM chunks WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
            await connection.execute(
                text("DELETE FROM documents WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
            await connection.execute(
                text("DELETE FROM sources WHERE dataset_id = :dataset_id"),
                {"dataset_id": dataset_id},
            )
            await connection.execute(
                text("DELETE FROM datasets WHERE id = :dataset_id"), {"dataset_id": dataset_id}
            )


async def project_neo4j_fixture(resource: Neo4jResource, ids: GraphProvenanceIds) -> None:
    projection = Neo4jProjection(resource)
    for entity_id, dataset_id, name, entity_type in (
        (ids.entity_a1_id, ids.dataset_a_id, "Ada Lovelace", "person"),
        (ids.entity_a2_id, ids.dataset_a_id, "Charles Babbage", "person"),
        (ids.entity_a3_id, ids.dataset_a_id, "Analytical Engine", "concept"),
        (ids.entity_b1_id, ids.dataset_b_id, "Other Entity", "concept"),
    ):
        await projection.apply(
            entity_upsert_command(
                entity_id=entity_id,
                dataset_id=dataset_id,
                name=name,
                entity_type=entity_type,
                description="seeded",
                importance_weight=0.5,
                generation=1,
            )
        )
    for relation_id, source_entity_id, target_entity_id, predicate in (
        (ids.relation_a1_a2_id, ids.entity_a1_id, ids.entity_a2_id, "worked_with"),
        (ids.relation_a2_a3_id, ids.entity_a2_id, ids.entity_a3_id, "designed"),
    ):
        await projection.apply(
            relation_upsert_command(
                relation_id=relation_id,
                dataset_id=ids.dataset_a_id,
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
                predicate=predicate,
                description="seeded",
                confidence=0.8,
                importance_weight=0.5,
                generation=1,
            )
        )


async def cleanup_neo4j_fixture(resource: Neo4jResource, ids: GraphProvenanceIds) -> None:
    entity_ids = [ids.entity_a1_id, ids.entity_a2_id, ids.entity_a3_id, ids.entity_b1_id]
    for entity_id in entity_ids:
        await resource.driver.execute_query(
            "MATCH (n:Entity {id: $id}) DETACH DELETE n",
            {"id": str(entity_id)},
            database_=resource.database,
        )


async def neo4j_entity_count_by_id(resource: Neo4jResource, entity_id: UUID) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n:Entity {id: $id}) RETURN count(n) AS count",
        {"id": str(entity_id)},
        database_=resource.database,
    )
    records = getattr(result, "records", ())
    return int(records[0]["count"]) if records else 0


def settings_for(engine: AsyncEngine, tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": str(engine.url),
        "neo4j_password": "test-neo4j-password",
        "llm_api_key": "test-llm-key",
        "app_env": "test",
        "data_directory": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def graph_service(engine: AsyncEngine, neo4j: Neo4jResource, tmp_path: Path) -> GraphReadService:
    return GraphReadService(
        settings_for(engine, tmp_path),
        graph_client=Neo4jGraphRead(neo4j),
        session_factory=create_session_factory(engine),
    )


def provenance_service(engine: AsyncEngine, tmp_path: Path) -> ProvenanceService:
    return ProvenanceService(
        settings_for(engine, tmp_path),
        session_factory=create_session_factory(engine),
    )


# --------------------------------------------------------------------------
# Scenario A/G — subgraph real + isolation
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subgraph_real_discovers_hydrates_and_isolates_datasets(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("Ada Lovelace worked with Charles Babbage.", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        await project_neo4j_fixture(neo4j_resource, ids)
        service = graph_service(postgres_engine, neo4j_resource, tmp_path)

        dataset_a_slug = f"graph-provenance-a-{ids.dataset_a_id}"
        result = await service.subgraph(
            dataset_slug=dataset_a_slug, entity_id=ids.entity_a1_id, depth=2
        )

        entity_ids = {entity.entity_id for entity in result.entities}
        relation_ids = {relation.relation_id for relation in result.relations}
        assert entity_ids == {ids.entity_a1_id, ids.entity_a2_id, ids.entity_a3_id}
        assert relation_ids == {ids.relation_a1_a2_id, ids.relation_a2_a3_id}
        assert ids.entity_b1_id not in entity_ids

        repeat = await service.subgraph(
            dataset_slug=dataset_a_slug, entity_id=ids.entity_a1_id, depth=2
        )
        assert [e.entity_id for e in repeat.entities] == [e.entity_id for e in result.entities]
        assert [r.relation_id for r in repeat.relations] == [
            r.relation_id for r in result.relations
        ]

        dataset_b_slug = f"graph-provenance-b-{ids.dataset_b_id}"
        schema_a = await service.schema(dataset_slug=dataset_a_slug)
        schema_b = await service.schema(dataset_slug=dataset_b_slug)
        assert schema_a.entity_types == ["concept", "person"]
        assert schema_a.relation_predicates == ["designed", "worked_with"]
        assert schema_b.entity_types == ["concept"]
        assert schema_b.relation_predicates == []
    finally:
        await cleanup_neo4j_fixture(neo4j_resource, ids)
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario B — path real
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_real_finds_shortest_path_and_respects_max_depth(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("content", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        await project_neo4j_fixture(neo4j_resource, ids)
        service = graph_service(postgres_engine, neo4j_resource, tmp_path)
        dataset_a_slug = f"graph-provenance-a-{ids.dataset_a_id}"

        found = await service.path(
            dataset_slug=dataset_a_slug,
            from_entity_id=ids.entity_a1_id,
            to_entity_id=ids.entity_a3_id,
            max_depth=4,
        )
        assert found.found is True
        assert [e.entity_id for e in found.entities] == [
            ids.entity_a1_id,
            ids.entity_a2_id,
            ids.entity_a3_id,
        ]
        assert [r.relation_id for r in found.relations] == [
            ids.relation_a1_a2_id,
            ids.relation_a2_a3_id,
        ]

        too_short = await service.path(
            dataset_slug=dataset_a_slug,
            from_entity_id=ids.entity_a1_id,
            to_entity_id=ids.entity_a3_id,
            max_depth=1,
        )
        assert too_short.found is False
        assert too_short.entities == []
        assert too_short.relations == []
    finally:
        await cleanup_neo4j_fixture(neo4j_resource, ids)
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario C — stale projection never resurrected
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_projection_is_never_resurrected(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("content", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        await project_neo4j_fixture(neo4j_resource, ids)

        # Neo4j still has A2 and both relations projected, but PostgreSQL now
        # marks A2 inactive: authoritative state must win.
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE entities SET is_active = FALSE WHERE id = :entity_id"),
                {"entity_id": ids.entity_a2_id},
            )

        service = graph_service(postgres_engine, neo4j_resource, tmp_path)
        dataset_a_slug = f"graph-provenance-a-{ids.dataset_a_id}"

        subgraph = await service.subgraph(
            dataset_slug=dataset_a_slug, entity_id=ids.entity_a1_id, depth=2
        )
        entity_ids = {entity.entity_id for entity in subgraph.entities}
        assert ids.entity_a2_id not in entity_ids
        relation_ids = {relation.relation_id for relation in subgraph.relations}
        assert ids.relation_a1_a2_id not in relation_ids
        assert ids.relation_a2_a3_id not in relation_ids

        path = await service.path(
            dataset_slug=dataset_a_slug,
            from_entity_id=ids.entity_a1_id,
            to_entity_id=ids.entity_a3_id,
            max_depth=4,
        )
        assert path.found is False
        assert path.entities == []
        assert path.relations == []

        # A2 is still physically projected in Neo4j: proves this was
        # authoritative filtering, not accidental Neo4j deletion.
        assert await neo4j_entity_count_by_id(neo4j_resource, ids.entity_a2_id) == 1
    finally:
        await cleanup_neo4j_fixture(neo4j_resource, ids)
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario D — provenance/relation real, then hidden after deactivation
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_relation_real_then_hidden_after_deactivation(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("content", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        service = provenance_service(postgres_engine, tmp_path)

        result = await service.relation(ids.relation_a1_a2_id)
        assert result.source_entity_id == ids.entity_a1_id
        assert result.target_entity_id == ids.entity_a2_id
        assert len(result.evidence) == 1
        evidence = result.evidence[0]
        assert evidence.source_id == ids.source_id
        assert evidence.document_id == ids.document_id
        assert evidence.chunk_id == ids.chunk1_id
        assert evidence.chunk_ordinal == 0
        assert "Ada Lovelace" in evidence.quote
        assert evidence.start_char == 0
        assert evidence.end_char == 42

        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE chunks SET is_active = FALSE WHERE id = :chunk_id"),
                {"chunk_id": ids.chunk1_id},
            )

        # Evidence chunk is now stale; the relation itself is still
        # authoritative but no evidence should survive for that chunk.
        after = await service.relation(ids.relation_a1_a2_id)
        assert all(item.chunk_id != ids.chunk1_id for item in after.evidence)
    finally:
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario E — provenance/source real, no storage leak
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_source_real_lineage_without_storage_leak(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    internal_dir = tmp_path / "internal-secret-directory"
    internal_dir.mkdir()
    storage_path = internal_dir / "content.txt"
    storage_path.write_text("Ada Lovelace worked with Charles Babbage.", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        service = provenance_service(postgres_engine, tmp_path)

        result = await service.source(ids.source_id)

        assert result.source_id == ids.source_id
        assert result.dataset_id == ids.dataset_a_id
        assert result.storage_available is True
        assert {d.document_id for d in result.documents} == {ids.document_id}
        assert {c.chunk_id for c in result.chunks} == {ids.chunk1_id, ids.chunk2_id}
        assert {e.entity_id for e in result.entities} == {
            ids.entity_a1_id,
            ids.entity_a2_id,
            ids.entity_a3_id,
        }
        assert {r.relation_id for r in result.relations} == {
            ids.relation_a1_a2_id,
            ids.relation_a2_a3_id,
        }

        dumped = json.dumps(result.model_dump(mode="json"))
        assert "internal-secret-directory" not in dumped
        assert str(storage_path) not in dumped
        assert "file://" not in dumped
        assert "file:" not in dumped
    finally:
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario F — provenance/query real, then forgotten
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_query_real_then_hidden_after_forgetting(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("content", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        service = provenance_service(postgres_engine, tmp_path)

        before = await service.query(ids.query_id)
        assert before.query_id == ids.query_id
        assert len(before.references) == 1
        reference = before.references[0]
        assert reference.available is True
        assert reference.quote is not None
        assert "Ada Lovelace" in reference.quote

        # Equivalent to a Forget: the chunk stops being authoritative.
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE chunks SET is_active = FALSE WHERE id = :chunk_id"),
                {"chunk_id": ids.chunk1_id},
            )

        after = await service.query(ids.query_id)
        assert after.query_id == ids.query_id
        assert len(after.references) == 1
        forgotten_reference = after.references[0]
        assert forgotten_reference.available is False
        assert forgotten_reference.quote is None

        dumped = json.dumps(after.model_dump(mode="json"))
        assert "Ada Lovelace worked with Charles Babbage" not in dumped
    finally:
        await cleanup_postgres_fixture(postgres_engine, ids)


# --------------------------------------------------------------------------
# Scenario G — dataset isolation across every endpoint
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dataset_isolation_across_graph_and_provenance_endpoints(
    postgres_engine: AsyncEngine, neo4j_resource: Neo4jResource, tmp_path: Path
) -> None:
    ids = GraphProvenanceIds()
    storage_path = tmp_path / "source.txt"
    storage_path.write_text("content", encoding="utf-8")
    try:
        await insert_postgres_fixture(postgres_engine, ids, storage_uri=storage_path.as_uri())
        await project_neo4j_fixture(neo4j_resource, ids)
        graph_read = graph_service(postgres_engine, neo4j_resource, tmp_path)
        provenance = provenance_service(postgres_engine, tmp_path)
        dataset_b_slug = f"graph-provenance-b-{ids.dataset_b_id}"

        schema_b = await graph_read.schema(dataset_slug=dataset_b_slug)
        assert "person" not in schema_b.entity_types

        subgraph_b = await graph_read.subgraph(
            dataset_slug=dataset_b_slug, entity_id=ids.entity_b1_id, depth=2
        )
        entity_ids_b = {entity.entity_id for entity in subgraph_b.entities}
        assert entity_ids_b == {ids.entity_b1_id}
        assert ids.entity_a1_id not in entity_ids_b

        with pytest.raises(SofiasMemoryError):
            # A1 belongs to dataset A: must not be reachable from dataset B.
            await graph_read.subgraph(
                dataset_slug=dataset_b_slug, entity_id=ids.entity_a1_id, depth=2
            )

        source_b = await provenance.source(ids.source_b_id)
        assert source_b.dataset_id == ids.dataset_b_id
        assert ids.entity_a1_id not in {entity.entity_id for entity in source_b.entities}
    finally:
        await cleanup_neo4j_fixture(neo4j_resource, ids)
        await cleanup_postgres_fixture(postgres_engine, ids)
