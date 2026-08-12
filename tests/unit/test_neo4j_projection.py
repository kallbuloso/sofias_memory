from __future__ import annotations

from collections.abc import Mapping

import pytest

from sofias_memory.infrastructure.neo4j import (
    CHUNK_NEXT_DELETE_CYPHER,
    CHUNK_NEXT_UPSERT_CYPHER,
    CHUNK_UPSERT_CYPHER,
    ENTITY_DELETE_CYPHER,
    ENTITY_MENTION_DELETE_CYPHER,
    ENTITY_MENTION_UPSERT_CYPHER,
    ENTITY_UPSERT_CYPHER,
    RELATION_DELETE_CYPHER,
    RELATION_UPSERT_CYPHER,
    Neo4jProjection,
    Neo4jResource,
    ProjectionEndpointMissingError,
)
from sofias_memory.ports import (
    GRAPH_PROJECTION_SCHEMA_VERSION,
    ProjectionCommand,
    ProjectionValidationError,
    projection_command_from_payload,
)

CONFIGURED_DATABASE = "sofias-memory-projection-test"
DATASET_ID = "10000000-0000-0000-0000-000000000001"
ENTITY_A_ID = "10000000-0000-0000-0000-000000000101"
ENTITY_B_ID = "10000000-0000-0000-0000-000000000102"
CHUNK_1_ID = "10000000-0000-0000-0000-000000000201"
CHUNK_2_ID = "10000000-0000-0000-0000-000000000202"
SOURCE_ID = "10000000-0000-0000-0000-000000000301"
DOCUMENT_ID = "10000000-0000-0000-0000-000000000401"
RELATION_ID = "10000000-0000-0000-0000-000000000501"
MENTION_ID = "10000000-0000-0000-0000-000000000601"


class FakeRecord:
    def data(self) -> Mapping[str, object]:
        return {}


class FakeResult:
    def __init__(self, record_count: int = 1) -> None:
        self.records = [FakeRecord() for _ in range(record_count)]


class RecordingNeo4jDriver:
    def __init__(self, *, record_count: int = 1) -> None:
        self.record_count = record_count
        self.execute_query_calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def verify_connectivity(self, **config: object) -> None:
        raise AssertionError("projection must not verify connectivity")

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> FakeResult:
        self.execute_query_calls.append(
            {
                "query": query_,
                "parameters": dict(parameters_ or {}),
                "database_": database_,
            }
        )
        return FakeResult(self.record_count)

    async def close(self) -> None:
        self.close_calls += 1


def make_projection(driver: RecordingNeo4jDriver) -> Neo4jProjection:
    return Neo4jProjection(Neo4jResource(driver, database=CONFIGURED_DATABASE))


def entity_payload(entity_id: str = ENTITY_A_ID, *, name: str = "Sofia") -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "entity",
        "operation": "upsert",
        "dataset_id": DATASET_ID,
        "aggregate_id": entity_id,
        "identity": {"id": entity_id},
        "properties": {
            "id": entity_id,
            "dataset_id": DATASET_ID,
            "name": name,
            "entity_type": "person",
            "description": "A remembered person.",
            "importance_weight": 0.75,
            "generation": 1,
        },
    }


def chunk_payload(chunk_id: str = CHUNK_1_ID, *, ordinal: int = 0) -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "chunk",
        "operation": "upsert",
        "dataset_id": DATASET_ID,
        "aggregate_id": chunk_id,
        "identity": {"id": chunk_id},
        "properties": {
            "id": chunk_id,
            "dataset_id": DATASET_ID,
            "source_id": SOURCE_ID,
            "document_id": DOCUMENT_ID,
            "ordinal": ordinal,
            "generation": 1,
        },
    }


def relation_payload(operation: str = "upsert") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "relation",
        "operation": operation,
        "dataset_id": DATASET_ID,
        "aggregate_id": RELATION_ID,
        "identity": {"relation_id": RELATION_ID},
        "endpoints": {
            "source_entity_id": ENTITY_A_ID,
            "target_entity_id": ENTITY_B_ID,
        },
    }
    if operation == "upsert":
        payload["properties"] = {
            "relation_id": RELATION_ID,
            "predicate": "knows",
            "description": "Sofia knows Ada.",
            "confidence": 0.9,
            "importance_weight": 0.8,
            "generation": 1,
        }
    return payload


def entity_mention_payload(operation: str = "upsert") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "entity_mention",
        "operation": operation,
        "dataset_id": DATASET_ID,
        "aggregate_id": MENTION_ID,
        "identity": {"mention_id": MENTION_ID},
        "endpoints": {"entity_id": ENTITY_A_ID, "chunk_id": CHUNK_1_ID},
    }
    if operation == "upsert":
        payload["properties"] = {"mention_id": MENTION_ID, "confidence": 0.95}
    return payload


def chunk_next_payload(operation: str = "upsert") -> dict[str, object]:
    return {
        "schema_version": GRAPH_PROJECTION_SCHEMA_VERSION,
        "aggregate_type": "chunk_next",
        "operation": operation,
        "dataset_id": DATASET_ID,
        "aggregate_id": CHUNK_1_ID,
        "identity": {"from_chunk_id": CHUNK_1_ID, "to_chunk_id": CHUNK_2_ID},
        "endpoints": {"from_chunk_id": CHUNK_1_ID, "to_chunk_id": CHUNK_2_ID},
        "properties": {},
    }


def test_projection_command_validates_payload_contract() -> None:
    command = projection_command_from_payload(entity_payload())

    assert command.schema_version == GRAPH_PROJECTION_SCHEMA_VERSION
    assert command.aggregate_type == "entity"
    assert command.operation == "upsert"
    assert command.aggregate_id == ENTITY_A_ID
    assert command.identity == {"id": ENTITY_A_ID}
    assert command.properties["id"] == ENTITY_A_ID


def test_projection_command_round_trips_through_outbox_payload() -> None:
    command = projection_command_from_payload(relation_payload())

    assert ProjectionCommand.from_payload(command.to_payload()) == command


def test_projection_command_rejects_identity_mismatch() -> None:
    payload = entity_payload()
    payload["aggregate_id"] = ENTITY_B_ID

    with pytest.raises(ProjectionValidationError, match="aggregate_id"):
        projection_command_from_payload(payload)


@pytest.mark.asyncio
async def test_entity_and_chunk_upserts_merge_by_id() -> None:
    driver = RecordingNeo4jDriver()
    projection = make_projection(driver)

    await projection.apply(projection_command_from_payload(entity_payload()))
    await projection.apply(projection_command_from_payload(chunk_payload()))

    assert driver.execute_query_calls[0] == {
        "query": ENTITY_UPSERT_CYPHER,
        "parameters": entity_payload()["properties"],
        "database_": CONFIGURED_DATABASE,
    }
    assert "MERGE (n:Entity {id: $id})" in ENTITY_UPSERT_CYPHER
    assert driver.execute_query_calls[1]["query"] == CHUNK_UPSERT_CYPHER
    assert "MERGE (n:Chunk {id: $id})" in CHUNK_UPSERT_CYPHER
    assert "text" not in driver.execute_query_calls[1]["parameters"]
    assert "embedding" not in driver.execute_query_calls[1]["parameters"]


@pytest.mark.asyncio
async def test_relationship_upserts_use_stable_identities() -> None:
    driver = RecordingNeo4jDriver()
    projection = make_projection(driver)

    await projection.apply(projection_command_from_payload(relation_payload()))
    await projection.apply(projection_command_from_payload(entity_mention_payload()))
    await projection.apply(projection_command_from_payload(chunk_next_payload()))

    assert driver.execute_query_calls[0]["query"] == RELATION_UPSERT_CYPHER
    assert driver.execute_query_calls[0]["parameters"]["relation_id"] == RELATION_ID
    assert "MATCH (source:Entity {id: $source_entity_id})" in RELATION_UPSERT_CYPHER
    assert driver.execute_query_calls[1]["query"] == ENTITY_MENTION_UPSERT_CYPHER
    assert driver.execute_query_calls[1]["parameters"]["mention_id"] == MENTION_ID
    assert "mention_id: $mention_id" in ENTITY_MENTION_UPSERT_CYPHER
    assert driver.execute_query_calls[2]["query"] == CHUNK_NEXT_UPSERT_CYPHER
    assert driver.execute_query_calls[2]["parameters"] == {
        "from_chunk_id": CHUNK_1_ID,
        "to_chunk_id": CHUNK_2_ID,
    }


@pytest.mark.asyncio
async def test_missing_relationship_endpoint_raises_retryable_error() -> None:
    driver = RecordingNeo4jDriver(record_count=0)
    projection = make_projection(driver)

    with pytest.raises(ProjectionEndpointMissingError, match="endpoint missing"):
        await projection.apply(projection_command_from_payload(relation_payload()))


@pytest.mark.asyncio
async def test_delete_commands_use_snapshot_identity_without_postgres_row() -> None:
    driver = RecordingNeo4jDriver(record_count=0)
    projection = make_projection(driver)

    await projection.apply(
        ProjectionCommand(
            schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
            aggregate_type="entity",
            operation="delete",
            dataset_id=DATASET_ID,
            aggregate_id=ENTITY_A_ID,
            identity={"id": ENTITY_A_ID},
            endpoints={},
            properties={},
        )
    )
    await projection.apply(projection_command_from_payload(relation_payload("delete")))
    await projection.apply(projection_command_from_payload(entity_mention_payload("delete")))
    await projection.apply(projection_command_from_payload(chunk_next_payload("delete")))

    assert driver.execute_query_calls[0]["query"] == ENTITY_DELETE_CYPHER
    assert driver.execute_query_calls[1]["query"] == RELATION_DELETE_CYPHER
    assert driver.execute_query_calls[2]["query"] == ENTITY_MENTION_DELETE_CYPHER
    assert driver.execute_query_calls[3]["query"] == CHUNK_NEXT_DELETE_CYPHER


@pytest.mark.asyncio
async def test_invalid_command_does_not_execute_neo4j() -> None:
    driver = RecordingNeo4jDriver()
    projection = make_projection(driver)
    command = ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity",
        operation="upsert",
        dataset_id=DATASET_ID,
        aggregate_id=ENTITY_B_ID,
        identity={"id": ENTITY_A_ID},
        endpoints={},
        properties=projection_command_from_payload(entity_payload()).properties,
    )

    with pytest.raises(ProjectionValidationError):
        await projection.apply(command)

    assert driver.execute_query_calls == []


@pytest.mark.asyncio
async def test_replay_uses_the_same_projection_identity() -> None:
    driver = RecordingNeo4jDriver()
    projection = make_projection(driver)
    command = projection_command_from_payload(relation_payload())

    await projection.apply(command)
    await projection.apply(command)

    assert driver.execute_query_calls[0] == driver.execute_query_calls[1]
