"""Read-only Neo4j graph traversal primitives for the graph/provenance API.

Every query here is strictly read-only (no ``MERGE``/``CREATE``/``DELETE``/``SET``)
and returns technical identifiers only. Depth/max-depth values are always
server-validated, closed-range integers before they are spliced into the Cypher
text, because Neo4j does not support query parameters for variable-length
relationship bounds. No other request input reaches the query text; entity ids
and dataset ids are always passed as bound parameters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sofias_memory.infrastructure.neo4j.driver import Neo4jResource

SUBGRAPH_ENTITY_IDS_CYPHER_TEMPLATE = """
MATCH (root:Entity {{id: $root_id, dataset_id: $dataset_id}})
CALL {{
  WITH root
  MATCH path = (root)-[:RELATES_TO*0..{depth}]-(neighbor:Entity)
  WHERE neighbor.dataset_id = $dataset_id
    AND all(node IN nodes(path) WHERE node.dataset_id = $dataset_id)
  RETURN DISTINCT neighbor.id AS entity_id
}}
RETURN entity_id
LIMIT $max_nodes
"""

RELATIONS_AMONG_CYPHER = """
UNWIND $entity_ids AS entity_id
MATCH (source:Entity {id: entity_id, dataset_id: $dataset_id})
      -[r:RELATES_TO]->(target:Entity)
WHERE target.id IN $entity_ids AND target.dataset_id = $dataset_id
RETURN DISTINCT
  r.relation_id AS relation_id,
  source.id AS source_entity_id,
  target.id AS target_entity_id
LIMIT $max_relations
"""

SHORTEST_PATH_CYPHER_TEMPLATE = """
MATCH (from:Entity {{id: $from_id, dataset_id: $dataset_id}})
MATCH (to:Entity {{id: $to_id, dataset_id: $dataset_id}})
CALL {{
  WITH from, to
  MATCH path = shortestPath((from)-[:RELATES_TO*1..{max_depth}]-(to))
  WHERE all(node IN nodes(path) WHERE node.dataset_id = $dataset_id)
  RETURN path
  LIMIT 1
}}
RETURN
  [node IN nodes(path) | node.id] AS entity_ids,
  [rel IN relationships(path) | {{
    relation_id: rel.relation_id,
    source_entity_id: startNode(rel).id,
    target_entity_id: endNode(rel).id
  }}] AS edges
"""


@dataclass(frozen=True)
class GraphRelationEdge:
    """A directed ``RELATES_TO`` edge discovered by Neo4j traversal."""

    relation_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID


@dataclass(frozen=True)
class GraphPathRecord:
    """A bounded, ordered path discovered by Neo4j traversal."""

    entity_ids: list[UUID]
    edges: list[GraphRelationEdge]


class Neo4jGraphRead:
    """Bounded, read-only Neo4j traversal for graph/provenance endpoints."""

    def __init__(self, resource: Neo4jResource) -> None:
        self._resource = resource

    async def subgraph_entity_ids(
        self,
        *,
        root_entity_id: UUID,
        dataset_id: UUID,
        depth: int,
        max_nodes: int,
    ) -> list[UUID]:
        if depth <= 0 or max_nodes <= 0:
            return []
        cypher = SUBGRAPH_ENTITY_IDS_CYPHER_TEMPLATE.format(depth=depth)
        result = await self._resource.driver.execute_query(
            cypher,
            {
                "root_id": str(root_entity_id),
                "dataset_id": str(dataset_id),
                "max_nodes": max_nodes,
            },
            database_=self._resource.database,
        )
        records = getattr(result, "records", ())
        return [UUID(str(_value(record, "entity_id"))) for record in records]

    async def relations_among(
        self,
        *,
        entity_ids: Sequence[UUID],
        dataset_id: UUID,
        max_relations: int,
    ) -> list[GraphRelationEdge]:
        if not entity_ids or max_relations <= 0:
            return []
        result = await self._resource.driver.execute_query(
            RELATIONS_AMONG_CYPHER,
            {
                "entity_ids": [str(entity_id) for entity_id in entity_ids],
                "dataset_id": str(dataset_id),
                "max_relations": max_relations,
            },
            database_=self._resource.database,
        )
        records = getattr(result, "records", ())
        return [_relation_edge_from_record(record) for record in records]

    async def shortest_path(
        self,
        *,
        from_entity_id: UUID,
        to_entity_id: UUID,
        dataset_id: UUID,
        max_depth: int,
    ) -> GraphPathRecord | None:
        if max_depth <= 0:
            return None
        cypher = SHORTEST_PATH_CYPHER_TEMPLATE.format(max_depth=max_depth)
        result = await self._resource.driver.execute_query(
            cypher,
            {
                "from_id": str(from_entity_id),
                "to_id": str(to_entity_id),
                "dataset_id": str(dataset_id),
            },
            database_=self._resource.database,
        )
        records = getattr(result, "records", ())
        if not records:
            return None
        record = records[0]
        raw_entity_ids = cast(Sequence[object], _value(record, "entity_ids"))
        raw_edges = cast(Sequence[object], _value(record, "edges"))
        entity_ids = [UUID(str(value)) for value in raw_entity_ids]
        edges = [_relation_edge_from_mapping(edge) for edge in raw_edges]
        return GraphPathRecord(entity_ids=entity_ids, edges=edges)


def _relation_edge_from_record(record: object) -> GraphRelationEdge:
    return GraphRelationEdge(
        relation_id=UUID(str(_value(record, "relation_id"))),
        source_entity_id=UUID(str(_value(record, "source_entity_id"))),
        target_entity_id=UUID(str(_value(record, "target_entity_id"))),
    )


def _relation_edge_from_mapping(value: object) -> GraphRelationEdge:
    return GraphRelationEdge(
        relation_id=UUID(str(_mapping_value(value, "relation_id"))),
        source_entity_id=UUID(str(_mapping_value(value, "source_entity_id"))),
        target_entity_id=UUID(str(_mapping_value(value, "target_entity_id"))),
    )


def _value(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record[key]
    return record[key]  # type: ignore[index]


def _mapping_value(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value[key]
    return value[key]  # type: ignore[index]
