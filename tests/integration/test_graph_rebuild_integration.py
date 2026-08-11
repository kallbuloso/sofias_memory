from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    Neo4jResource,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.services.graph_rebuild_service import GraphRebuildService

GRAPH_REBUILD_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_REBUILD_TESTS"


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    if os.environ.get(GRAPH_REBUILD_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_REBUILD_TESTS_ENV}=1 to run graph rebuild tests")

    engine = create_async_engine_from_settings(load_settings())
    try:
        yield engine
    finally:
        await dispose_async_engine(engine)


@pytest_asyncio.fixture()
async def neo4j_resource() -> AsyncIterator[Neo4jResource]:
    if os.environ.get(GRAPH_REBUILD_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_REBUILD_TESTS_ENV}=1 to run graph rebuild tests")

    resource = create_neo4j_resource_from_settings(load_settings())
    try:
        yield resource
    finally:
        await resource.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_rebuild_dataset_projects_authoritative_state_only(
    postgres_engine: AsyncEngine,
    neo4j_resource: Neo4jResource,
) -> None:
    ids = RebuildIds()
    service = GraphRebuildService(
        session_factory=create_session_factory(postgres_engine),
        neo4j_resource=neo4j_resource,
        projection=Neo4jProjection(neo4j_resource),
    )
    try:
        await cleanup_neo4j(neo4j_resource, ids)
        await insert_postgres_fixture(postgres_engine, ids)
        await create_sentinel_node(neo4j_resource, ids)

        first = await service.rebuild_dataset(ids.dataset_id)

        assert first.datasets == 1
        assert first.entities == 2
        assert first.chunks == 2
        assert first.entity_mentions == 1
        assert first.relations == 1
        assert first.next_relationships == 1
        assert await entity_count(neo4j_resource, ids.dataset_id) == 2
        assert await chunk_count(neo4j_resource, ids.dataset_id) == 2
        assert await node_count_by_id(neo4j_resource, ids.inactive_entity_id) == 0
        assert await mentioned_in_count(neo4j_resource, ids) == 1
        assert await relates_to_count(neo4j_resource, ids) == 1
        assert await next_count(neo4j_resource, ids) == 1
        assert await node_count_by_id(neo4j_resource, ids.sentinel_entity_id) == 1

        second = await service.rebuild_dataset(ids.dataset_id)

        assert second == first
        assert await entity_count(neo4j_resource, ids.dataset_id) == 2
        assert await chunk_count(neo4j_resource, ids.dataset_id) == 2
        assert await mentioned_in_count(neo4j_resource, ids) == 1
        assert await relates_to_count(neo4j_resource, ids) == 1
        assert await next_count(neo4j_resource, ids) == 1
        assert await node_count_by_id(neo4j_resource, ids.sentinel_entity_id) == 1
    finally:
        await cleanup_neo4j(neo4j_resource, ids)
        await cleanup_postgres(postgres_engine, ids)


class RebuildIds:
    def __init__(self) -> None:
        self.dataset_id = str(uuid4())
        self.other_dataset_id = str(uuid4())
        self.source_id = str(uuid4())
        self.document_id = str(uuid4())
        self.chunk_a_id = str(uuid4())
        self.chunk_b_id = str(uuid4())
        self.entity_a_id = str(uuid4())
        self.entity_b_id = str(uuid4())
        self.inactive_entity_id = str(uuid4())
        self.relation_id = str(uuid4())
        self.mention_id = str(uuid4())
        self.sentinel_entity_id = str(uuid4())


async def insert_postgres_fixture(engine: AsyncEngine, ids: RebuildIds) -> None:
    vector = vector_literal(3072)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO datasets (id, name, slug, description, status, active_generation)
                VALUES (:dataset_id, :name, :slug, NULL, 'active', 1)
                """
            ),
            {
                "dataset_id": ids.dataset_id,
                "name": f"Graph rebuild {ids.dataset_id}",
                "slug": f"graph-rebuild-{ids.dataset_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO sources (
                    id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
                    content_sha256, normalized_sha256, byte_size, metadata, status, version
                )
                VALUES (
                    :source_id, :dataset_id, 'text', 'Graph rebuild source', 'text/plain',
                    NULL, NULL, :content_sha256, NULL, 42, '{}'::jsonb, 'active', 1
                )
                """
            ),
            {
                "source_id": ids.source_id,
                "dataset_id": ids.dataset_id,
                "content_sha256": "a" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO documents (
                    id, dataset_id, source_id, generation, title, language, normalized_text,
                    text_sha256, token_count, metadata, is_active
                )
                VALUES (
                    :document_id, :dataset_id, :source_id, 1, 'Graph rebuild document',
                    'en', 'graph rebuild integration text', :text_sha256, 5, '{}'::jsonb, TRUE
                )
                """
            ),
            {
                "document_id": ids.document_id,
                "dataset_id": ids.dataset_id,
                "source_id": ids.source_id,
                "text_sha256": "b" * 64,
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
                (
                    :chunk_a_id, :dataset_id, :document_id, :source_id, 1, 0,
                    'graph rebuild chunk one', :chunk_a_hash, 4, 0, 23,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'graph rebuild chunk one'), TRUE
                ),
                (
                    :chunk_b_id, :dataset_id, :document_id, :source_id, 1, 1,
                    'graph rebuild chunk two', :chunk_b_hash, 4, 24, 47,
                    ARRAY['root']::text[], '{}'::jsonb, CAST(:vector AS vector),
                    to_tsvector('simple', 'graph rebuild chunk two'), TRUE
                )
                """
            ),
            {
                "chunk_a_id": ids.chunk_a_id,
                "chunk_b_id": ids.chunk_b_id,
                "dataset_id": ids.dataset_id,
                "document_id": ids.document_id,
                "source_id": ids.source_id,
                "chunk_a_hash": "c" * 64,
                "chunk_b_hash": "d" * 64,
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
                (
                    :entity_a_id, :dataset_id, 1, :entity_a_key, 'Sofia', 'person',
                    'Projected entity A', ARRAY['Sofia']::text[], '{}'::jsonb,
                    0.9, 0.8, CAST(:vector AS vector), TRUE
                ),
                (
                    :entity_b_id, :dataset_id, 1, :entity_b_key, 'Ada', 'person',
                    'Projected entity B', ARRAY['Ada']::text[], '{}'::jsonb,
                    0.9, 0.8, CAST(:vector AS vector), TRUE
                ),
                (
                    :inactive_entity_id, :dataset_id, 1, :inactive_key, 'Inactive', 'person',
                    'Inactive entity', ARRAY['Inactive']::text[], '{}'::jsonb,
                    0.9, 0.8, CAST(:vector AS vector), FALSE
                )
                """
            ),
            {
                "entity_a_id": ids.entity_a_id,
                "entity_b_id": ids.entity_b_id,
                "inactive_entity_id": ids.inactive_entity_id,
                "dataset_id": ids.dataset_id,
                "entity_a_key": f"sofia-{ids.dataset_id}",
                "entity_b_key": f"ada-{ids.dataset_id}",
                "inactive_key": f"inactive-{ids.dataset_id}",
                "vector": vector,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, chunk_id, surface_text, start_char, end_char, confidence
                )
                VALUES (:mention_id, :entity_a_id, :chunk_a_id, 'Sofia', 0, 5, 0.95)
                """
            ),
            {
                "mention_id": ids.mention_id,
                "entity_a_id": ids.entity_a_id,
                "chunk_a_id": ids.chunk_a_id,
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
                VALUES (
                    :relation_id, :dataset_id, 1, :entity_a_id, :entity_b_id, 'knows',
                    'Sofia knows Ada', '{}'::jsonb, 0.8, 0.7, CAST(:vector AS vector), TRUE
                )
                """
            ),
            {
                "relation_id": ids.relation_id,
                "dataset_id": ids.dataset_id,
                "entity_a_id": ids.entity_a_id,
                "entity_b_id": ids.entity_b_id,
                "vector": vector,
            },
        )


async def cleanup_postgres(engine: AsyncEngine, ids: RebuildIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM relations WHERE id = :relation_id"),
            {"relation_id": ids.relation_id},
        )
        await connection.execute(
            text("DELETE FROM entity_mentions WHERE id = :mention_id"),
            {"mention_id": ids.mention_id},
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


async def create_sentinel_node(resource: Neo4jResource, ids: RebuildIds) -> None:
    await resource.driver.execute_query(
        "MERGE (n:Entity {id: $id}) SET n.dataset_id = $dataset_id, n.name = 'Sentinel'",
        {"id": ids.sentinel_entity_id, "dataset_id": ids.other_dataset_id},
        database_=resource.database,
    )


async def cleanup_neo4j(resource: Neo4jResource, ids: RebuildIds) -> None:
    await resource.driver.execute_query(
        "MATCH (n:Entity {dataset_id: $dataset_id}) DETACH DELETE n",
        {"dataset_id": ids.dataset_id},
        database_=resource.database,
    )
    await resource.driver.execute_query(
        "MATCH (n:Chunk {dataset_id: $dataset_id}) DETACH DELETE n",
        {"dataset_id": ids.dataset_id},
        database_=resource.database,
    )
    await resource.driver.execute_query(
        "MATCH (n:Entity {id: $id}) DETACH DELETE n",
        {"id": ids.sentinel_entity_id},
        database_=resource.database,
    )


async def entity_count(resource: Neo4jResource, dataset_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n:Entity {dataset_id: $dataset_id}) RETURN count(n) AS count",
        {"dataset_id": dataset_id},
    )


async def chunk_count(resource: Neo4jResource, dataset_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n:Chunk {dataset_id: $dataset_id}) RETURN count(n) AS count",
        {"dataset_id": dataset_id},
    )


async def node_count_by_id(resource: Neo4jResource, node_id: str) -> int:
    return await count_query(
        resource,
        "MATCH (n {id: $id}) RETURN count(n) AS count",
        {"id": node_id},
    )


async def mentioned_in_count(resource: Neo4jResource, ids: RebuildIds) -> int:
    return await count_query(
        resource,
        " ".join(
            (
                "MATCH (:Entity {id: $entity_id})",
                "-[r:MENTIONED_IN {mention_id: $mention_id}]->",
                "(:Chunk {id: $chunk_id})",
                "RETURN count(r) AS count",
            )
        ),
        {
            "entity_id": ids.entity_a_id,
            "chunk_id": ids.chunk_a_id,
            "mention_id": ids.mention_id,
        },
    )


async def relates_to_count(resource: Neo4jResource, ids: RebuildIds) -> int:
    return await count_query(
        resource,
        " ".join(
            (
                "MATCH (:Entity {id: $source_entity_id})",
                "-[r:RELATES_TO {relation_id: $relation_id}]->",
                "(:Entity {id: $target_entity_id})",
                "RETURN count(r) AS count",
            )
        ),
        {
            "source_entity_id": ids.entity_a_id,
            "target_entity_id": ids.entity_b_id,
            "relation_id": ids.relation_id,
        },
    )


async def next_count(resource: Neo4jResource, ids: RebuildIds) -> int:
    return await count_query(
        resource,
        " ".join(
            (
                "MATCH (:Chunk {id: $from_chunk_id})",
                "-[r:NEXT]->",
                "(:Chunk {id: $to_chunk_id})",
                "RETURN count(r) AS count",
            )
        ),
        {"from_chunk_id": ids.chunk_a_id, "to_chunk_id": ids.chunk_b_id},
    )


async def count_query(
    resource: Neo4jResource,
    query: str,
    parameters: Mapping[str, object],
) -> int:
    result = await resource.driver.execute_query(
        query,
        parameters,
        database_=resource.database,
    )
    return int(result_records(result)[0]["count"])


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join("0.0" for _ in range(dimensions)) + "]"
