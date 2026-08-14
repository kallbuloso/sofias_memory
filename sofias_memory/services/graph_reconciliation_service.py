"""Dataset-scoped Neo4j projection reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.infrastructure.neo4j import Neo4jResource
from sofias_memory.infrastructure.postgres.repositories.graph_rebuild import (
    GraphRebuildRepository,
    GraphRebuildSnapshot,
)
from sofias_memory.infrastructure.postgres.session import session_scope
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.services.graph_rebuild_service import (
    GraphRebuildRepositoryFactory,
    GraphRebuildService,
)

ENTITY_IDS_CYPHER = "MATCH (n:Entity {dataset_id: $dataset_id}) RETURN n.id AS id"
CHUNK_IDS_CYPHER = "MATCH (n:Chunk {dataset_id: $dataset_id}) RETURN n.id AS id"
MENTIONED_IN_IDS_CYPHER = " ".join(
    (
        "MATCH (entity:Entity)-[r:MENTIONED_IN]->(chunk:Chunk)",
        "WHERE entity.dataset_id = $dataset_id OR chunk.dataset_id = $dataset_id",
        "RETURN r.mention_id AS mention_id,",
        "entity.id AS entity_id,",
        "chunk.id AS chunk_id",
    )
)
RELATES_TO_IDS_CYPHER = " ".join(
    (
        "MATCH (source:Entity)-[r:RELATES_TO]->(target:Entity)",
        "WHERE source.dataset_id = $dataset_id OR target.dataset_id = $dataset_id",
        "RETURN r.relation_id AS relation_id,",
        "source.id AS source_entity_id,",
        "target.id AS target_entity_id",
    )
)
NEXT_IDS_CYPHER = " ".join(
    (
        "MATCH (from_chunk:Chunk)-[r:NEXT]->(to_chunk:Chunk)",
        "WHERE from_chunk.dataset_id = $dataset_id OR to_chunk.dataset_id = $dataset_id",
        "RETURN from_chunk.id AS from_chunk_id,",
        "to_chunk.id AS to_chunk_id",
    )
)


@dataclass(frozen=True)
class GraphProjectionIdentitySnapshot:
    """Projection identities for one dataset, detached from Neo4j result objects."""

    entity_ids: frozenset[str]
    chunk_ids: frozenset[str]
    entity_mentions: frozenset[tuple[str, str, str]]
    relations: frozenset[tuple[str, str, str]]
    next_relationships: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class GraphReconciliationDiff:
    """Dataset-scoped projection divergence counts."""

    entities_missing: int = 0
    entities_extra: int = 0
    chunks_missing: int = 0
    chunks_extra: int = 0
    entity_mentions_missing: int = 0
    entity_mentions_extra: int = 0
    relations_missing: int = 0
    relations_extra: int = 0
    next_missing: int = 0
    next_extra: int = 0

    @property
    def has_divergence(self) -> bool:
        return any(
            (
                self.entities_missing,
                self.entities_extra,
                self.chunks_missing,
                self.chunks_extra,
                self.entity_mentions_missing,
                self.entity_mentions_extra,
                self.relations_missing,
                self.relations_extra,
                self.next_missing,
                self.next_extra,
            )
        )


@dataclass(frozen=True)
class GraphReconciliationResult:
    """Result of one graph reconciliation attempt."""

    diff: GraphReconciliationDiff
    rebuilt: bool


class GraphReconciliationService:
    """Compare PostgreSQL-authoritative graph state with the Neo4j projection."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        neo4j_resource: Neo4jResource,
        rebuild_service: GraphRebuildService,
        repository_factory: GraphRebuildRepositoryFactory = GraphRebuildRepository,
    ) -> None:
        self._session_factory = session_factory
        self._neo4j_resource = neo4j_resource
        self._rebuild_service = rebuild_service
        self._repository_factory = repository_factory

    async def reconcile_dataset(self, dataset_id: UUID) -> GraphReconciliationResult:
        expected = expected_projection_snapshot(await self._load_dataset_snapshot(dataset_id))
        actual = await read_dataset_projection_snapshot(self._neo4j_resource, dataset_id)
        diff = compare_projection_snapshots(expected=expected, actual=actual)
        if not diff.has_divergence:
            return GraphReconciliationResult(diff=diff, rebuilt=False)

        await self._rebuild_service.rebuild_dataset(dataset_id)
        reconciled = await read_dataset_projection_snapshot(self._neo4j_resource, dataset_id)
        post_rebuild_diff = compare_projection_snapshots(expected=expected, actual=reconciled)
        if post_rebuild_diff.has_divergence:
            raise DependencyUnavailableError(
                message="Neo4j graph projection did not converge after dataset rebuild.",
                details={"component": "neo4j_projection", **diff_details(post_rebuild_diff)},
            )
        return GraphReconciliationResult(diff=diff, rebuilt=True)

    async def _load_dataset_snapshot(self, dataset_id: UUID) -> GraphRebuildSnapshot:
        async with session_scope(self._session_factory) as session:
            repository = self._repository_factory(session)
            return await repository.load_dataset(dataset_id)


def expected_projection_snapshot(
    snapshot: GraphRebuildSnapshot,
) -> GraphProjectionIdentitySnapshot:
    """Convert the PostgreSQL rebuild snapshot to technical projection identities."""

    return GraphProjectionIdentitySnapshot(
        entity_ids=frozenset(command.aggregate_id for command in snapshot.entity_commands),
        chunk_ids=frozenset(command.aggregate_id for command in snapshot.chunk_commands),
        entity_mentions=frozenset(
            (
                command.aggregate_id,
                command.endpoints["entity_id"],
                command.endpoints["chunk_id"],
            )
            for command in snapshot.entity_mention_commands
        ),
        relations=frozenset(
            (
                command.aggregate_id,
                command.endpoints["source_entity_id"],
                command.endpoints["target_entity_id"],
            )
            for command in snapshot.relation_commands
        ),
        next_relationships=frozenset(
            (
                command.endpoints["from_chunk_id"],
                command.endpoints["to_chunk_id"],
            )
            for command in snapshot.next_commands
        ),
    )


async def read_dataset_projection_snapshot(
    resource: Neo4jResource,
    dataset_id: UUID,
) -> GraphProjectionIdentitySnapshot:
    """Read current Neo4j projection identities for one dataset."""

    dataset_id_text = str(dataset_id)
    return GraphProjectionIdentitySnapshot(
        entity_ids=await _read_single_ids(resource, ENTITY_IDS_CYPHER, dataset_id_text, "id"),
        chunk_ids=await _read_single_ids(resource, CHUNK_IDS_CYPHER, dataset_id_text, "id"),
        entity_mentions=cast(
            frozenset[tuple[str, str, str]],
            await _read_tuple_ids(
                resource,
                MENTIONED_IN_IDS_CYPHER,
                dataset_id_text,
                ("mention_id", "entity_id", "chunk_id"),
                "malformed_mention",
            ),
        ),
        relations=cast(
            frozenset[tuple[str, str, str]],
            await _read_tuple_ids(
                resource,
                RELATES_TO_IDS_CYPHER,
                dataset_id_text,
                ("relation_id", "source_entity_id", "target_entity_id"),
                "malformed_relation",
            ),
        ),
        next_relationships=cast(
            frozenset[tuple[str, str]],
            await _read_tuple_ids(
                resource,
                NEXT_IDS_CYPHER,
                dataset_id_text,
                ("from_chunk_id", "to_chunk_id"),
                "malformed_next",
            ),
        ),
    )


def compare_projection_snapshots(
    *,
    expected: GraphProjectionIdentitySnapshot,
    actual: GraphProjectionIdentitySnapshot,
) -> GraphReconciliationDiff:
    """Return deterministic missing/extra counts between expected and actual projection."""

    return GraphReconciliationDiff(
        entities_missing=len(expected.entity_ids - actual.entity_ids),
        entities_extra=len(actual.entity_ids - expected.entity_ids),
        chunks_missing=len(expected.chunk_ids - actual.chunk_ids),
        chunks_extra=len(actual.chunk_ids - expected.chunk_ids),
        entity_mentions_missing=len(expected.entity_mentions - actual.entity_mentions),
        entity_mentions_extra=len(actual.entity_mentions - expected.entity_mentions),
        relations_missing=len(expected.relations - actual.relations),
        relations_extra=len(actual.relations - expected.relations),
        next_missing=len(expected.next_relationships - actual.next_relationships),
        next_extra=len(actual.next_relationships - expected.next_relationships),
    )


def diff_details(diff: GraphReconciliationDiff) -> dict[str, int]:
    return {
        "entities_missing": diff.entities_missing,
        "entities_extra": diff.entities_extra,
        "chunks_missing": diff.chunks_missing,
        "chunks_extra": diff.chunks_extra,
        "entity_mentions_missing": diff.entity_mentions_missing,
        "entity_mentions_extra": diff.entity_mentions_extra,
        "relations_missing": diff.relations_missing,
        "relations_extra": diff.relations_extra,
        "next_missing": diff.next_missing,
        "next_extra": diff.next_extra,
    }


async def _read_single_ids(
    resource: Neo4jResource,
    query: str,
    dataset_id: str,
    field: str,
) -> frozenset[str]:
    result = await resource.driver.execute_query(
        query,
        {"dataset_id": dataset_id},
        database_=resource.database,
    )
    values: set[str] = set()
    for index, record in enumerate(_records(result)):
        value = _record_value(record, field)
        values.add(value if isinstance(value, str) and value else f"malformed_{field}:{index}")
    return frozenset(values)


async def _read_tuple_ids(
    resource: Neo4jResource,
    query: str,
    dataset_id: str,
    fields: tuple[str, ...],
    malformed_prefix: str,
) -> frozenset[tuple[str, ...]]:
    result = await resource.driver.execute_query(
        query,
        {"dataset_id": dataset_id},
        database_=resource.database,
    )
    values: set[tuple[str, ...]] = set()
    for index, record in enumerate(_records(result)):
        row = tuple(_record_value(record, field) for field in fields)
        if all(isinstance(value, str) and value for value in row):
            values.add(tuple(str(value) for value in row))
            continue
        values.add(tuple(f"{malformed_prefix}:{index}:{field}" for field in fields))
    return frozenset(values)


def _records(result: object) -> tuple[object, ...]:
    records = getattr(result, "records", ())
    if isinstance(records, list | tuple):
        return tuple(records)
    return tuple(records)


def _record_value(record: object, field: str) -> object:
    if hasattr(record, "data"):
        data = record.data()
        if isinstance(data, dict):
            return data.get(field)
    try:
        return record[field]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
