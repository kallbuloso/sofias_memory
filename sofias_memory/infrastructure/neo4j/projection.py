"""Concrete Neo4j projection applier."""

from __future__ import annotations

from collections.abc import Mapping

from sofias_memory.infrastructure.neo4j.driver import Neo4jResource
from sofias_memory.ports.graph_projection import (
    ProjectionCommand,
    ProjectionPropertyValue,
    validate_projection_command,
)

ENTITY_LABEL = "Entity"
CHUNK_LABEL = "Chunk"
RELATES_TO_TYPE = "RELATES_TO"
MENTIONED_IN_TYPE = "MENTIONED_IN"
NEXT_TYPE = "NEXT"

ENTITY_UPSERT_CYPHER = " ".join(
    (
        "MERGE (n:Entity {id: $id})",
        "SET n.dataset_id = $dataset_id,",
        "n.name = $name,",
        "n.entity_type = $entity_type,",
        "n.description = $description,",
        "n.importance_weight = $importance_weight,",
        "n.generation = $generation",
        "RETURN n.id AS id",
    )
)
CHUNK_UPSERT_CYPHER = " ".join(
    (
        "MERGE (n:Chunk {id: $id})",
        "SET n.dataset_id = $dataset_id,",
        "n.source_id = $source_id,",
        "n.document_id = $document_id,",
        "n.ordinal = $ordinal,",
        "n.generation = $generation",
        "RETURN n.id AS id",
    )
)
RELATION_UPSERT_CYPHER = " ".join(
    (
        "MATCH (source:Entity {id: $source_entity_id})",
        "MATCH (target:Entity {id: $target_entity_id})",
        "MERGE (source)-[r:RELATES_TO {relation_id: $relation_id}]->(target)",
        "SET r.predicate = $predicate,",
        "r.description = $description,",
        "r.confidence = $confidence,",
        "r.importance_weight = $importance_weight,",
        "r.generation = $generation",
        "RETURN r.relation_id AS relation_id",
    )
)
ENTITY_MENTION_UPSERT_CYPHER = " ".join(
    (
        "MATCH (entity:Entity {id: $entity_id})",
        "MATCH (chunk:Chunk {id: $chunk_id})",
        "MERGE (entity)-[r:MENTIONED_IN {mention_id: $mention_id}]->(chunk)",
        "SET r.confidence = $confidence",
        "RETURN r.mention_id AS mention_id",
    )
)
CHUNK_NEXT_UPSERT_CYPHER = " ".join(
    (
        "MATCH (from_chunk:Chunk {id: $from_chunk_id})",
        "MATCH (to_chunk:Chunk {id: $to_chunk_id})",
        "MERGE (from_chunk)-[r:NEXT]->(to_chunk)",
        "RETURN from_chunk.id AS from_chunk_id, to_chunk.id AS to_chunk_id",
    )
)

ENTITY_DELETE_CYPHER = "MATCH (n:Entity {id: $id}) DETACH DELETE n"
CHUNK_DELETE_CYPHER = "MATCH (n:Chunk {id: $id}) DETACH DELETE n"
RELATION_DELETE_CYPHER = " ".join(
    (
        "MATCH (:Entity {id: $source_entity_id})",
        "-[r:RELATES_TO {relation_id: $relation_id}]->",
        "(:Entity {id: $target_entity_id})",
        "DELETE r",
    )
)
ENTITY_MENTION_DELETE_CYPHER = " ".join(
    (
        "MATCH (:Entity {id: $entity_id})",
        "-[r:MENTIONED_IN {mention_id: $mention_id}]->",
        "(:Chunk {id: $chunk_id})",
        "DELETE r",
    )
)
CHUNK_NEXT_DELETE_CYPHER = " ".join(
    (
        "MATCH (:Chunk {id: $from_chunk_id})",
        "-[r:NEXT]->",
        "(:Chunk {id: $to_chunk_id})",
        "DELETE r",
    )
)

DATASET_ENTITY_CLEANUP_CYPHER = "MATCH (n:Entity {dataset_id: $dataset_id}) DETACH DELETE n"
DATASET_CHUNK_CLEANUP_CYPHER = "MATCH (n:Chunk {dataset_id: $dataset_id}) DETACH DELETE n"
GLOBAL_ENTITY_CLEANUP_CYPHER = "MATCH (n:Entity) DETACH DELETE n"
GLOBAL_CHUNK_CLEANUP_CYPHER = "MATCH (n:Chunk) DETACH DELETE n"


class ProjectionEndpointMissingError(RuntimeError):
    """Relationship projection endpoints are not currently present in Neo4j."""


class Neo4jProjection:
    """Apply one validated projection command to Neo4j."""

    def __init__(self, resource: Neo4jResource) -> None:
        self._resource = resource

    async def apply(self, command: ProjectionCommand) -> None:
        command = validate_projection_command(command)
        if command.operation == "upsert":
            await self._apply_upsert(command)
            return
        await self._apply_delete(command)

    async def _apply_upsert(self, command: ProjectionCommand) -> None:
        query, params, requires_endpoint_match = _upsert_query(command)
        result = await self._resource.driver.execute_query(
            query,
            params,
            database_=self._resource.database,
        )
        if requires_endpoint_match and not _has_records(result):
            raise ProjectionEndpointMissingError("projection endpoint missing")

    async def _apply_delete(self, command: ProjectionCommand) -> None:
        query, params = _delete_query(command)
        await self._resource.driver.execute_query(
            query,
            params,
            database_=self._resource.database,
        )


async def delete_dataset_projection(resource: Neo4jResource, dataset_id: str) -> None:
    """Delete only Sofias projection nodes for one dataset."""

    params = {"dataset_id": dataset_id}
    await resource.driver.execute_query(
        DATASET_ENTITY_CLEANUP_CYPHER,
        params,
        database_=resource.database,
    )
    await resource.driver.execute_query(
        DATASET_CHUNK_CLEANUP_CYPHER,
        params,
        database_=resource.database,
    )


async def delete_all_projection(resource: Neo4jResource) -> None:
    """Delete only Sofias projection labels, preserving arbitrary other Neo4j data."""

    await resource.driver.execute_query(GLOBAL_ENTITY_CLEANUP_CYPHER, database_=resource.database)
    await resource.driver.execute_query(GLOBAL_CHUNK_CLEANUP_CYPHER, database_=resource.database)


def _upsert_query(
    command: ProjectionCommand,
) -> tuple[str, Mapping[str, ProjectionPropertyValue], bool]:
    if command.aggregate_type == "entity":
        return ENTITY_UPSERT_CYPHER, command.properties, False
    if command.aggregate_type == "chunk":
        return CHUNK_UPSERT_CYPHER, command.properties, False
    if command.aggregate_type == "relation":
        return (
            RELATION_UPSERT_CYPHER,
            {**command.endpoints, **command.properties},
            True,
        )
    if command.aggregate_type == "entity_mention":
        return (
            ENTITY_MENTION_UPSERT_CYPHER,
            {**command.endpoints, **command.properties},
            True,
        )
    if command.aggregate_type == "chunk_next":
        return CHUNK_NEXT_UPSERT_CYPHER, command.endpoints, True
    raise AssertionError("validated projection command has unsupported aggregate_type")


def _delete_query(command: ProjectionCommand) -> tuple[str, Mapping[str, str]]:
    if command.aggregate_type == "entity":
        return ENTITY_DELETE_CYPHER, {"id": command.identity["id"]}
    if command.aggregate_type == "chunk":
        return CHUNK_DELETE_CYPHER, {"id": command.identity["id"]}
    if command.aggregate_type == "relation":
        return (
            RELATION_DELETE_CYPHER,
            {
                "relation_id": command.identity["relation_id"],
                **command.endpoints,
            },
        )
    if command.aggregate_type == "entity_mention":
        return (
            ENTITY_MENTION_DELETE_CYPHER,
            {
                "mention_id": command.identity["mention_id"],
                **command.endpoints,
            },
        )
    if command.aggregate_type == "chunk_next":
        return CHUNK_NEXT_DELETE_CYPHER, command.endpoints
    raise AssertionError("validated projection command has unsupported aggregate_type")


def _has_records(result: object) -> bool:
    records = getattr(result, "records", ())
    return bool(records)
