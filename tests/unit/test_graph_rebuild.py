from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.neo4j import (
    DATASET_CHUNK_CLEANUP_CYPHER,
    DATASET_ENTITY_CLEANUP_CYPHER,
    GLOBAL_CHUNK_CLEANUP_CYPHER,
    GLOBAL_ENTITY_CLEANUP_CYPHER,
    Neo4jResource,
)
from sofias_memory.infrastructure.postgres.repositories.graph_rebuild import (
    ChunkRebuildRow,
    DatasetRebuildRow,
    EntityMentionRebuildRow,
    EntityRebuildRow,
    GraphRebuildRepository,
    GraphRebuildSnapshot,
    RelationRebuildRow,
    build_graph_rebuild_snapshot,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.ports import ProjectionCommand
from sofias_memory.services.graph_rebuild_service import GraphRebuildService

DATASET_ID = "10000000-0000-0000-0000-000000000001"
OTHER_DATASET_ID = "10000000-0000-0000-0000-000000000002"
SOURCE_ID = "10000000-0000-0000-0000-000000000101"
DOCUMENT_ID = "10000000-0000-0000-0000-000000000201"


class RecordingProjection:
    def __init__(self) -> None:
        self.commands: list[ProjectionCommand] = []

    async def apply(self, command: ProjectionCommand) -> None:
        self.commands.append(command)


class RecordingNeo4jDriver:
    def __init__(self) -> None:
        self.execute_query_calls: list[dict[str, object]] = []

    async def verify_connectivity(self, **config: object) -> None:
        raise AssertionError("rebuild must not call verify_connectivity")

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> object:
        self.execute_query_calls.append(
            {
                "query": query_,
                "parameters": dict(parameters_ or {}),
                "database_": database_,
            }
        )
        return object()

    async def close(self) -> None:
        return None


class FakeSession:
    async def close(self) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> AsyncSession:
        return cast(AsyncSession, FakeSession())


class FakeRebuildRepository(GraphRebuildRepository):
    snapshot: GraphRebuildSnapshot
    load_dataset_calls: list[UUID] = []
    load_all_calls = 0

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_dataset(self, dataset_id: UUID) -> GraphRebuildSnapshot:
        type(self).load_dataset_calls.append(dataset_id)
        return type(self).snapshot

    async def load_all(self) -> GraphRebuildSnapshot:
        type(self).load_all_calls += 1
        return type(self).snapshot


def dataset_row(
    dataset_id: str = DATASET_ID,
    *,
    status: DatasetStatus = DatasetStatus.ACTIVE,
    active_generation: int = 1,
) -> DatasetRebuildRow:
    return DatasetRebuildRow(
        id=dataset_id,
        status=status,
        active_generation=active_generation,
    )


def entity_row(
    entity_id: str,
    *,
    dataset_id: str = DATASET_ID,
    generation: int = 1,
    is_active: bool = True,
) -> EntityRebuildRow:
    return EntityRebuildRow(
        id=entity_id,
        dataset_id=dataset_id,
        generation=generation,
        name=f"Entity {entity_id[-4:]}",
        entity_type="test",
        description="Test entity.",
        importance_weight=0.5,
        is_active=is_active,
    )


def chunk_row(
    chunk_id: str,
    *,
    dataset_id: str = DATASET_ID,
    document_id: str = DOCUMENT_ID,
    generation: int = 1,
    ordinal: int = 0,
    is_active: bool = True,
) -> ChunkRebuildRow:
    return ChunkRebuildRow(
        id=chunk_id,
        dataset_id=dataset_id,
        source_id=SOURCE_ID,
        document_id=document_id,
        generation=generation,
        ordinal=ordinal,
        is_active=is_active,
    )


def relation_row(
    relation_id: str,
    *,
    source_entity_id: str,
    target_entity_id: str,
    dataset_id: str = DATASET_ID,
    generation: int = 1,
    is_active: bool = True,
) -> RelationRebuildRow:
    return RelationRebuildRow(
        id=relation_id,
        dataset_id=dataset_id,
        generation=generation,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate="knows",
        description="Test relation.",
        confidence=0.8,
        importance_weight=0.7,
        is_active=is_active,
    )


def mention_row(
    mention_id: str,
    *,
    entity_id: str,
    chunk_id: str,
) -> EntityMentionRebuildRow:
    return EntityMentionRebuildRow(
        id=mention_id,
        entity_id=entity_id,
        chunk_id=chunk_id,
        confidence=0.9,
    )


def test_snapshot_projects_only_active_dataset_generation_and_rows() -> None:
    active_entity = "10000000-0000-0000-0000-000000000301"
    old_entity = "10000000-0000-0000-0000-000000000302"
    inactive_entity = "10000000-0000-0000-0000-000000000303"
    active_chunk = "10000000-0000-0000-0000-000000000401"
    old_chunk = "10000000-0000-0000-0000-000000000402"
    inactive_chunk = "10000000-0000-0000-0000-000000000403"

    snapshot = build_graph_rebuild_snapshot(
        datasets=(dataset_row(), dataset_row(OTHER_DATASET_ID, status=DatasetStatus.DELETED)),
        entities=(
            entity_row(active_entity),
            entity_row(old_entity, generation=0),
            entity_row(inactive_entity, is_active=False),
            entity_row("10000000-0000-0000-0000-000000000304", dataset_id=OTHER_DATASET_ID),
        ),
        chunks=(
            chunk_row(active_chunk),
            chunk_row(old_chunk, generation=0),
            chunk_row(inactive_chunk, is_active=False),
            chunk_row("10000000-0000-0000-0000-000000000404", dataset_id=OTHER_DATASET_ID),
        ),
        mentions=(),
        relations=(),
    )

    assert [command.aggregate_id for command in snapshot.entity_commands] == [active_entity]
    assert [command.aggregate_id for command in snapshot.chunk_commands] == [active_chunk]
    assert snapshot.dataset_ids == (DATASET_ID,)


def test_mentions_relations_and_next_require_projectable_endpoints_without_gap_skip() -> None:
    entity_a = "10000000-0000-0000-0000-000000000301"
    entity_b = "10000000-0000-0000-0000-000000000302"
    inactive_entity = "10000000-0000-0000-0000-000000000303"
    chunk_0 = "10000000-0000-0000-0000-000000000401"
    chunk_1 = "10000000-0000-0000-0000-000000000402"
    chunk_3 = "10000000-0000-0000-0000-000000000403"

    snapshot = build_graph_rebuild_snapshot(
        datasets=(dataset_row(),),
        entities=(
            entity_row(entity_a),
            entity_row(entity_b),
            entity_row(inactive_entity, is_active=False),
        ),
        chunks=(
            chunk_row(chunk_0, ordinal=0),
            chunk_row(chunk_1, ordinal=1),
            chunk_row(chunk_3, ordinal=3),
        ),
        mentions=(
            mention_row(
                "10000000-0000-0000-0000-000000000501",
                entity_id=entity_a,
                chunk_id=chunk_0,
            ),
            mention_row(
                "10000000-0000-0000-0000-000000000502",
                entity_id=inactive_entity,
                chunk_id=chunk_0,
            ),
        ),
        relations=(
            relation_row(
                "10000000-0000-0000-0000-000000000601",
                source_entity_id=entity_a,
                target_entity_id=entity_b,
            ),
            relation_row(
                "10000000-0000-0000-0000-000000000602",
                source_entity_id=entity_a,
                target_entity_id=inactive_entity,
            ),
        ),
    )

    assert [command.aggregate_id for command in snapshot.entity_mention_commands] == [
        "10000000-0000-0000-0000-000000000501"
    ]
    assert [command.aggregate_id for command in snapshot.relation_commands] == [
        "10000000-0000-0000-0000-000000000601"
    ]
    assert [
        (command.identity["from_chunk_id"], command.identity["to_chunk_id"])
        for command in snapshot.next_commands
    ] == [(chunk_0, chunk_1)]


@pytest.mark.asyncio
async def test_rebuild_dataset_cleans_target_dataset_then_projects_in_adr_order() -> None:
    snapshot = build_graph_rebuild_snapshot(
        datasets=(dataset_row(),),
        entities=(entity_row("10000000-0000-0000-0000-000000000301"),),
        chunks=(chunk_row("10000000-0000-0000-0000-000000000401"),),
        mentions=(),
        relations=(),
    )
    FakeRebuildRepository.snapshot = snapshot
    FakeRebuildRepository.load_dataset_calls = []
    driver = RecordingNeo4jDriver()
    projection = RecordingProjection()
    service = GraphRebuildService(
        session_factory=cast(AsyncSessionFactory, FakeSessionFactory()),
        neo4j_resource=Neo4jResource(driver, database="neo4j-test"),
        projection=projection,
        repository_factory=FakeRebuildRepository,
    )

    result = await service.rebuild_dataset(DATASET_ID)

    cleanup_calls = driver.execute_query_calls[-2:]
    assert cleanup_calls == [
        {
            "query": DATASET_ENTITY_CLEANUP_CYPHER,
            "parameters": {"dataset_id": DATASET_ID},
            "database_": "neo4j-test",
        },
        {
            "query": DATASET_CHUNK_CLEANUP_CYPHER,
            "parameters": {"dataset_id": DATASET_ID},
            "database_": "neo4j-test",
        },
    ]
    assert [command.aggregate_type for command in projection.commands] == ["entity", "chunk"]
    assert result.entities == 1
    assert result.chunks == 1


@pytest.mark.asyncio
async def test_rebuild_all_cleans_only_sofias_projection_labels() -> None:
    FakeRebuildRepository.snapshot = build_graph_rebuild_snapshot(
        datasets=(dataset_row(),),
        entities=(),
        chunks=(),
        mentions=(),
        relations=(),
    )
    driver = RecordingNeo4jDriver()
    service = GraphRebuildService(
        session_factory=cast(AsyncSessionFactory, FakeSessionFactory()),
        neo4j_resource=Neo4jResource(driver, database="neo4j-test"),
        projection=RecordingProjection(),
        repository_factory=FakeRebuildRepository,
    )

    await service.rebuild_all()

    cleanup_queries = [call["query"] for call in driver.execute_query_calls[-2:]]
    assert cleanup_queries == [GLOBAL_ENTITY_CLEANUP_CYPHER, GLOBAL_CHUNK_CLEANUP_CYPHER]
    assert "MATCH (n) DETACH DELETE n" not in cleanup_queries


@pytest.mark.asyncio
async def test_rebuild_dataset_can_be_repeated_with_same_convergent_commands() -> None:
    snapshot = build_graph_rebuild_snapshot(
        datasets=(dataset_row(),),
        entities=(entity_row("10000000-0000-0000-0000-000000000301"),),
        chunks=(chunk_row("10000000-0000-0000-0000-000000000401"),),
        mentions=(),
        relations=(),
    )
    FakeRebuildRepository.snapshot = snapshot
    projection = RecordingProjection()
    service = GraphRebuildService(
        session_factory=cast(AsyncSessionFactory, FakeSessionFactory()),
        neo4j_resource=Neo4jResource(RecordingNeo4jDriver(), database="neo4j-test"),
        projection=projection,
        repository_factory=FakeRebuildRepository,
    )

    first = await service.rebuild_dataset(DATASET_ID)
    second = await service.rebuild_dataset(DATASET_ID)

    assert first == second
    assert projection.commands[:2] == projection.commands[2:]
