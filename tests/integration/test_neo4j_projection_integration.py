from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import uuid4

import pytest

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    Neo4jResource,
    create_neo4j_resource_from_settings,
)
from sofias_memory.ports import GRAPH_PROJECTION_SCHEMA_VERSION, projection_command_from_payload

NEO4J_PROJECTION_TESTS_ENV = "SOFIAS_MEMORY_RUN_NEO4J_PROJECTION_TESTS"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_neo4j_projection_idempotency_against_configured_database() -> None:
    if os.environ.get(NEO4J_PROJECTION_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_PROJECTION_TESTS_ENV}=1 to run Neo4j projection tests")

    ids = ProjectionIds()
    resource = create_neo4j_resource_from_settings(load_settings())
    projection = Neo4jProjection(resource)
    try:
        await cleanup_projection(resource, ids)

        commands = [
            entity_payload(ids.entity_a_id, ids.dataset_id, name="Sofia"),
            entity_payload(ids.entity_b_id, ids.dataset_id, name="Ada"),
            chunk_payload(
                ids.chunk_1_id,
                ids.dataset_id,
                ids.source_id,
                ids.document_id,
                ordinal=0,
            ),
            chunk_payload(
                ids.chunk_2_id,
                ids.dataset_id,
                ids.source_id,
                ids.document_id,
                ordinal=1,
            ),
            relation_payload(ids),
            entity_mention_payload(ids),
            chunk_next_payload(ids),
        ]

        for payload in commands:
            await projection.apply(projection_command_from_payload(payload))
        for payload in commands:
            await projection.apply(projection_command_from_payload(payload))

        assert await node_count(resource, "Entity", ids.entity_a_id) == 1
        assert await node_count(resource, "Entity", ids.entity_b_id) == 1
        assert await node_count(resource, "Chunk", ids.chunk_1_id) == 1
        assert await node_count(resource, "Chunk", ids.chunk_2_id) == 1
        assert await relation_count(resource, ids) == 1
        assert await mention_count(resource, ids) == 1
        assert await next_count(resource, ids) == 1

        await projection.apply(
            projection_command_from_payload(
                entity_payload(ids.entity_a_id, ids.dataset_id, name="Sofia Updated")
            )
        )
        assert await entity_name(resource, ids.entity_a_id) == "Sofia Updated"

        for payload in (
            relation_payload(ids, operation="delete"),
            entity_mention_payload(ids, operation="delete"),
            chunk_next_payload(ids, operation="delete"),
        ):
            await projection.apply(projection_command_from_payload(payload))

        assert await relation_count(resource, ids) == 0
        assert await mention_count(resource, ids) == 0
        assert await next_count(resource, ids) == 0

        for payload in (
            entity_delete_payload(ids.entity_a_id, ids.dataset_id),
            entity_delete_payload(ids.entity_b_id, ids.dataset_id),
            chunk_delete_payload(ids.chunk_1_id, ids.dataset_id),
            chunk_delete_payload(ids.chunk_2_id, ids.dataset_id),
        ):
            await projection.apply(projection_command_from_payload(payload))

        assert await node_count(resource, "Entity", ids.entity_a_id) == 0
        assert await node_count(resource, "Chunk", ids.chunk_1_id) == 0
    finally:
        await cleanup_projection(resource, ids)
        await resource.close()


class ProjectionIds:
    def __init__(self) -> None:
        self.dataset_id = str(uuid4())
        self.entity_a_id = str(uuid4())
        self.entity_b_id = str(uuid4())
        self.chunk_1_id = str(uuid4())
        self.chunk_2_id = str(uuid4())
        self.source_id = str(uuid4())
        self.document_id = str(uuid4())
        self.relation_id = str(uuid4())
        self.mention_id = str(uuid4())


def entity_payload(entity_id: str, dataset_id: str, *, name: str) -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "entity",
        "operation": "upsert",
        "dataset_id": dataset_id,
        "aggregate_id": entity_id,
        "identity": {"id": entity_id},
        "properties": {
            "id": entity_id,
            "dataset_id": dataset_id,
            "name": name,
            "entity_type": "person",
            "description": "Integration test entity.",
            "importance_weight": 0.5,
            "generation": 1,
        },
    }


def chunk_payload(
    chunk_id: str,
    dataset_id: str,
    source_id: str,
    document_id: str,
    *,
    ordinal: int,
) -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "chunk",
        "operation": "upsert",
        "dataset_id": dataset_id,
        "aggregate_id": chunk_id,
        "identity": {"id": chunk_id},
        "properties": {
            "id": chunk_id,
            "dataset_id": dataset_id,
            "source_id": source_id,
            "document_id": document_id,
            "ordinal": ordinal,
            "generation": 1,
        },
    }


def relation_payload(ids: ProjectionIds, *, operation: str = "upsert") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "relation",
        "operation": operation,
        "dataset_id": ids.dataset_id,
        "aggregate_id": ids.relation_id,
        "identity": {"relation_id": ids.relation_id},
        "endpoints": {
            "source_entity_id": ids.entity_a_id,
            "target_entity_id": ids.entity_b_id,
        },
    }
    if operation == "upsert":
        payload["properties"] = {
            "relation_id": ids.relation_id,
            "predicate": "knows",
            "description": "Integration relation.",
            "confidence": 0.8,
            "importance_weight": 0.7,
            "generation": 1,
        }
    return payload


def entity_mention_payload(ids: ProjectionIds, *, operation: str = "upsert") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "entity_mention",
        "operation": operation,
        "dataset_id": ids.dataset_id,
        "aggregate_id": ids.mention_id,
        "identity": {"mention_id": ids.mention_id},
        "endpoints": {"entity_id": ids.entity_a_id, "chunk_id": ids.chunk_1_id},
    }
    if operation == "upsert":
        payload["properties"] = {"mention_id": ids.mention_id, "confidence": 0.9}
    return payload


def chunk_next_payload(ids: ProjectionIds, *, operation: str = "upsert") -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "chunk_next",
        "operation": operation,
        "dataset_id": ids.dataset_id,
        "aggregate_id": ids.chunk_1_id,
        "identity": {"from_chunk_id": ids.chunk_1_id, "to_chunk_id": ids.chunk_2_id},
        "endpoints": {"from_chunk_id": ids.chunk_1_id, "to_chunk_id": ids.chunk_2_id},
        "properties": {},
    }


def entity_delete_payload(entity_id: str, dataset_id: str) -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "entity",
        "operation": "delete",
        "dataset_id": dataset_id,
        "aggregate_id": entity_id,
        "identity": {"id": entity_id},
    }


def chunk_delete_payload(chunk_id: str, dataset_id: str) -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "chunk",
        "operation": "delete",
        "dataset_id": dataset_id,
        "aggregate_id": chunk_id,
        "identity": {"id": chunk_id},
    }


async def cleanup_projection(resource: Neo4jResource, ids: ProjectionIds) -> None:
    node_ids = [ids.entity_a_id, ids.entity_b_id, ids.chunk_1_id, ids.chunk_2_id]
    await resource.driver.execute_query(
        "MATCH (n) WHERE n.id IN $node_ids DETACH DELETE n",
        {"node_ids": node_ids},
        database_=resource.database,
    )


async def node_count(resource: Neo4jResource, label: str, node_id: str) -> int:
    if label not in {"Entity", "Chunk"}:
        raise AssertionError("unexpected integration test label")
    result = await resource.driver.execute_query(
        f"MATCH (n:{label} {{id: $node_id}}) RETURN count(n) AS count",
        {"node_id": node_id},
        database_=resource.database,
    )
    return result_count(result)


async def relation_count(resource: Neo4jResource, ids: ProjectionIds) -> int:
    result = await resource.driver.execute_query(
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
        database_=resource.database,
    )
    return result_count(result)


async def mention_count(resource: Neo4jResource, ids: ProjectionIds) -> int:
    result = await resource.driver.execute_query(
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
            "chunk_id": ids.chunk_1_id,
            "mention_id": ids.mention_id,
        },
        database_=resource.database,
    )
    return result_count(result)


async def next_count(resource: Neo4jResource, ids: ProjectionIds) -> int:
    result = await resource.driver.execute_query(
        " ".join(
            (
                "MATCH (:Chunk {id: $from_chunk_id})",
                "-[r:NEXT]->",
                "(:Chunk {id: $to_chunk_id})",
                "RETURN count(r) AS count",
            )
        ),
        {"from_chunk_id": ids.chunk_1_id, "to_chunk_id": ids.chunk_2_id},
        database_=resource.database,
    )
    return result_count(result)


async def entity_name(resource: Neo4jResource, entity_id: str) -> str:
    result = await resource.driver.execute_query(
        "MATCH (n:Entity {id: $entity_id}) RETURN n.name AS name",
        {"entity_id": entity_id},
        database_=resource.database,
    )
    records = result_records(result)
    return str(records[0]["name"])


def result_count(result: object) -> int:
    records = result_records(result)
    return int(records[0]["count"])


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]
