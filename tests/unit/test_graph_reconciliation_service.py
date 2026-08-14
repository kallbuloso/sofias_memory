from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.neo4j import Neo4jResource
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
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import (
    CHUNK_IDS_CYPHER,
    ENTITY_IDS_CYPHER,
    MENTIONED_IN_IDS_CYPHER,
    NEXT_IDS_CYPHER,
    RELATES_TO_IDS_CYPHER,
    GraphProjectionIdentitySnapshot,
    GraphReconciliationService,
    compare_projection_snapshots,
    expected_projection_snapshot,
)

DATASET_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_DATASET_ID = UUID("10000000-0000-0000-0000-000000000002")
ENTITY_A_ID = "10000000-0000-0000-0000-000000000101"
ENTITY_B_ID = "10000000-0000-0000-0000-000000000102"
CHUNK_A_ID = "10000000-0000-0000-0000-000000000201"
CHUNK_B_ID = "10000000-0000-0000-0000-000000000202"
SOURCE_ID = "10000000-0000-0000-0000-000000000301"
DOCUMENT_ID = "10000000-0000-0000-0000-000000000401"
MENTION_ID = "10000000-0000-0000-0000-000000000501"
RELATION_ID = "10000000-0000-0000-0000-000000000601"


class FakeRecord:
    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = dict(data)

    def data(self) -> dict[str, object]:
        return dict(self._data)


class FakeResult:
    def __init__(self, rows: list[Mapping[str, object]]) -> None:
        self.records = [FakeRecord(row) for row in rows]


class SnapshotNeo4jDriver:
    def __init__(self, snapshots: list[GraphProjectionIdentitySnapshot]) -> None:
        self.snapshots = snapshots
        self.execute_query_calls: list[dict[str, object]] = []

    async def verify_connectivity(self, **config: object) -> None:
        raise AssertionError("graph reconciliation must not verify connectivity")

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
        snapshot_index = min(
            (len(self.execute_query_calls) - 1) // 5,
            len(self.snapshots) - 1,
        )
        snapshot = self.snapshots[snapshot_index]
        return FakeResult(_rows_for_query(query_, snapshot))

    async def close(self) -> None:
        return None


class FakeSession:
    def in_transaction(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> AsyncSession:
        return cast(AsyncSession, FakeSession())


class FakeGraphRebuildRepository(GraphRebuildRepository):
    snapshot: GraphRebuildSnapshot
    load_dataset_calls: list[UUID] = []

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_dataset(self, dataset_id: UUID) -> GraphRebuildSnapshot:
        type(self).load_dataset_calls.append(dataset_id)
        return type(self).snapshot


class FakeGraphRebuildService:
    def __init__(self) -> None:
        self.rebuild_dataset_calls: list[UUID] = []
        self.rebuild_all_calls = 0

    async def rebuild_dataset(self, dataset_id: UUID) -> object:
        self.rebuild_dataset_calls.append(dataset_id)
        return object()

    async def rebuild_all(self) -> object:
        self.rebuild_all_calls += 1
        raise AssertionError("graph reconciliation must never call rebuild_all")


def test_compare_projection_snapshots_represents_known_missing_entities_regression() -> None:
    expected = GraphProjectionIdentitySnapshot(
        entity_ids=frozenset(f"entity-{index:02d}" for index in range(88)),
        chunk_ids=frozenset(),
        entity_mentions=frozenset(),
        relations=frozenset(),
        next_relationships=frozenset(),
    )
    actual = GraphProjectionIdentitySnapshot(
        entity_ids=frozenset(f"entity-{index:02d}" for index in range(44)),
        chunk_ids=frozenset(),
        entity_mentions=frozenset(),
        relations=frozenset(),
        next_relationships=frozenset(),
    )

    diff = compare_projection_snapshots(expected=expected, actual=actual)

    assert diff.entities_missing == 44
    assert diff.has_divergence is True


@pytest.mark.asyncio
async def test_graph_reconciliation_convergent_projection_is_noop() -> None:
    snapshot = authoritative_snapshot()
    expected = expected_projection_snapshot(snapshot)
    driver = SnapshotNeo4jDriver([expected])
    rebuild_service = FakeGraphRebuildService()
    service = reconciliation_service(
        driver=driver,
        rebuild_service=rebuild_service,
        snapshot=snapshot,
    )

    result = await service.reconcile_dataset(DATASET_ID)

    assert result.diff.has_divergence is False
    assert result.rebuilt is False
    assert rebuild_service.rebuild_dataset_calls == []
    assert rebuild_service.rebuild_all_calls == 0
    assert all(
        call["parameters"] == {"dataset_id": str(DATASET_ID)} for call in driver.execute_query_calls
    )


@pytest.mark.asyncio
async def test_graph_reconciliation_rebuilds_dataset_and_revalidates_all_projection_types() -> None:
    snapshot = authoritative_snapshot()
    expected = expected_projection_snapshot(snapshot)
    divergent = GraphProjectionIdentitySnapshot(
        entity_ids=frozenset({ENTITY_A_ID}),
        chunk_ids=frozenset({CHUNK_A_ID, CHUNK_B_ID, "extra-chunk"}),
        entity_mentions=frozenset(),
        relations=frozenset(
            {
                (RELATION_ID, ENTITY_A_ID, ENTITY_B_ID),
                ("extra-relation", ENTITY_A_ID, ENTITY_B_ID),
            }
        ),
        next_relationships=frozenset(),
    )
    driver = SnapshotNeo4jDriver([divergent, expected])
    rebuild_service = FakeGraphRebuildService()
    service = reconciliation_service(
        driver=driver,
        rebuild_service=rebuild_service,
        snapshot=snapshot,
    )

    result = await service.reconcile_dataset(DATASET_ID)

    assert result.rebuilt is True
    assert result.diff.entities_missing == 1
    assert result.diff.chunks_extra == 1
    assert result.diff.entity_mentions_missing == 1
    assert result.diff.relations_extra == 1
    assert result.diff.next_missing == 1
    assert rebuild_service.rebuild_dataset_calls == [DATASET_ID]
    assert rebuild_service.rebuild_all_calls == 0
    assert len(driver.execute_query_calls) == 10


@pytest.mark.asyncio
async def test_graph_reconciliation_fails_safely_when_rebuild_does_not_converge() -> None:
    snapshot = authoritative_snapshot()
    expected = expected_projection_snapshot(snapshot)
    divergent = GraphProjectionIdentitySnapshot(
        entity_ids=frozenset({ENTITY_A_ID}),
        chunk_ids=expected.chunk_ids,
        entity_mentions=expected.entity_mentions,
        relations=expected.relations,
        next_relationships=expected.next_relationships,
    )
    driver = SnapshotNeo4jDriver([divergent, divergent])
    rebuild_service = FakeGraphRebuildService()
    service = reconciliation_service(
        driver=driver,
        rebuild_service=rebuild_service,
        snapshot=snapshot,
    )

    with pytest.raises(DependencyUnavailableError, match="did not converge"):
        await service.reconcile_dataset(DATASET_ID)

    assert rebuild_service.rebuild_dataset_calls == [DATASET_ID]
    assert rebuild_service.rebuild_all_calls == 0


def authoritative_snapshot() -> GraphRebuildSnapshot:
    return build_graph_rebuild_snapshot(
        datasets=(
            DatasetRebuildRow(
                id=str(DATASET_ID),
                status=DatasetStatus.ACTIVE,
                active_generation=1,
            ),
        ),
        entities=(
            entity_row(ENTITY_A_ID),
            entity_row(ENTITY_B_ID),
        ),
        chunks=(
            chunk_row(CHUNK_A_ID, ordinal=0),
            chunk_row(CHUNK_B_ID, ordinal=1),
        ),
        mentions=(
            EntityMentionRebuildRow(
                id=MENTION_ID,
                entity_id=ENTITY_A_ID,
                chunk_id=CHUNK_A_ID,
                confidence=0.8,
            ),
        ),
        relations=(
            RelationRebuildRow(
                id=RELATION_ID,
                dataset_id=str(DATASET_ID),
                generation=1,
                source_entity_id=ENTITY_A_ID,
                target_entity_id=ENTITY_B_ID,
                predicate="uses",
                description="Uses projection.",
                confidence=0.9,
                importance_weight=0.5,
                is_active=True,
            ),
        ),
    )


def entity_row(entity_id: str, *, dataset_id: str = str(DATASET_ID)) -> EntityRebuildRow:
    return EntityRebuildRow(
        id=entity_id,
        dataset_id=dataset_id,
        generation=1,
        name=f"Entity {entity_id[-4:]}",
        entity_type="Concept",
        description="Description.",
        importance_weight=0.5,
        is_active=True,
    )


def chunk_row(
    chunk_id: str,
    *,
    dataset_id: str = str(DATASET_ID),
    ordinal: int,
) -> ChunkRebuildRow:
    return ChunkRebuildRow(
        id=chunk_id,
        dataset_id=dataset_id,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        generation=1,
        ordinal=ordinal,
        is_active=True,
    )


def reconciliation_service(
    *,
    driver: SnapshotNeo4jDriver,
    rebuild_service: FakeGraphRebuildService,
    snapshot: GraphRebuildSnapshot,
) -> GraphReconciliationService:
    FakeGraphRebuildRepository.snapshot = snapshot
    FakeGraphRebuildRepository.load_dataset_calls = []
    return GraphReconciliationService(
        session_factory=cast(AsyncSessionFactory, FakeSessionFactory()),
        neo4j_resource=Neo4jResource(driver, database="neo4j"),
        rebuild_service=cast(GraphRebuildService, rebuild_service),
        repository_factory=FakeGraphRebuildRepository,
    )


def _rows_for_query(
    query: str,
    snapshot: GraphProjectionIdentitySnapshot,
) -> list[Mapping[str, object]]:
    if query == ENTITY_IDS_CYPHER:
        return [{"id": value} for value in sorted(snapshot.entity_ids)]
    if query == CHUNK_IDS_CYPHER:
        return [{"id": value} for value in sorted(snapshot.chunk_ids)]
    if query == MENTIONED_IN_IDS_CYPHER:
        return [
            {"mention_id": mention_id, "entity_id": entity_id, "chunk_id": chunk_id}
            for mention_id, entity_id, chunk_id in sorted(snapshot.entity_mentions)
        ]
    if query == RELATES_TO_IDS_CYPHER:
        return [
            {
                "relation_id": relation_id,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
            }
            for relation_id, source_entity_id, target_entity_id in sorted(snapshot.relations)
        ]
    if query == NEXT_IDS_CYPHER:
        return [
            {"from_chunk_id": from_chunk_id, "to_chunk_id": to_chunk_id}
            for from_chunk_id, to_chunk_id in sorted(snapshot.next_relationships)
        ]
    raise AssertionError(f"unexpected query: {query}")
