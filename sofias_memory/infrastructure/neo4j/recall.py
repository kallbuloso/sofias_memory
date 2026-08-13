"""Read-only Neo4j graph recall primitives."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sofias_memory.infrastructure.neo4j.driver import Neo4jResource

GRAPH_NEIGHBORHOOD_CYPHER = """
UNWIND $seed_chunk_ids AS seed_chunk_id
MATCH (chunk:Chunk {id: seed_chunk_id})
WHERE chunk.dataset_id IN $dataset_ids
MATCH (seed_entity:Entity)-[:MENTIONED_IN]->(chunk)
WHERE seed_entity.dataset_id IN $dataset_ids
OPTIONAL MATCH (seed_entity)-[relation:RELATES_TO]-(neighbor:Entity)
WHERE neighbor.dataset_id IN $dataset_ids
RETURN
  seed_chunk_id AS seed_chunk_id,
  seed_entity.id AS seed_entity_id,
  neighbor.id AS neighbor_entity_id,
  relation.relation_id AS relation_id
LIMIT $limit
"""


@dataclass(frozen=True)
class Neo4jGraphRecallRecord:
    """IDs discovered from a bounded Neo4j neighborhood."""

    seed_chunk_id: UUID
    seed_entity_id: UUID
    neighbor_entity_id: UUID | None
    relation_id: UUID | None


class Neo4jGraphRecall:
    """Retrieve one-hop graph neighborhoods seeded by ranked chunk IDs."""

    def __init__(self, resource: Neo4jResource) -> None:
        self._resource = resource

    async def retrieve(
        self,
        *,
        seed_chunk_ids: Sequence[UUID],
        dataset_ids: Sequence[UUID],
        limit: int,
    ) -> list[Neo4jGraphRecallRecord]:
        if not seed_chunk_ids or not dataset_ids or limit <= 0:
            return []

        result = await self._resource.driver.execute_query(
            GRAPH_NEIGHBORHOOD_CYPHER,
            {
                "seed_chunk_ids": [str(chunk_id) for chunk_id in seed_chunk_ids],
                "dataset_ids": [str(dataset_id) for dataset_id in dataset_ids],
                "limit": limit,
            },
            database_=self._resource.database,
        )
        records = getattr(result, "records", ())
        return [_record_from_neo4j(record) for record in records]


def _record_from_neo4j(record: object) -> Neo4jGraphRecallRecord:
    return Neo4jGraphRecallRecord(
        seed_chunk_id=UUID(str(_record_value(record, "seed_chunk_id"))),
        seed_entity_id=UUID(str(_record_value(record, "seed_entity_id"))),
        neighbor_entity_id=_optional_uuid(_record_value(record, "neighbor_entity_id")),
        relation_id=_optional_uuid(_record_value(record, "relation_id")),
    )


def _record_value(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record[key]
    return record[key]  # type: ignore[index]


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return UUID(str(value))
