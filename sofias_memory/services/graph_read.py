"""Read-only graph traversal backed by PostgreSQL authoritative hydration.

Neo4j is used only to discover candidate identifiers within a bounded
neighborhood or path. Every identifier is then re-validated against
PostgreSQL (dataset ACTIVE, current generation, ``is_active``) before it is
returned to a client; nothing Neo4j returns is trusted on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.neo4j.graph_read import GraphPathRecord, GraphRelationEdge
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, Relation
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.graph import (
    GraphEntityNode,
    GraphPathResult,
    GraphSchemaResult,
    GraphSubgraphResult,
)
from sofias_memory.schemas.graph import GraphRelationEdge as GraphRelationEdgeModel

DATASET_NOT_FOUND_MESSAGE = "Dataset does not exist."
ENTITY_NOT_FOUND_MESSAGE = "Entity does not exist in this dataset."


class DatasetRepositoryForGraphRead(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class EntityRepositoryForGraphRead(Protocol):
    async def get_active_current_by_id(
        self, *, dataset_id: UUID, entity_id: UUID
    ) -> Entity | None: ...

    async def list_active_current_by_ids(
        self, *, dataset_id: UUID, entity_ids: list[UUID]
    ) -> list[Entity]: ...

    async def list_active_current_entity_types(self, *, dataset_id: UUID) -> list[str]: ...


class RelationRepositoryForGraphRead(Protocol):
    async def list_active_current_by_ids(
        self, *, dataset_id: UUID, relation_ids: list[UUID]
    ) -> list[Relation]: ...

    async def list_active_current_predicates(self, *, dataset_id: UUID) -> list[str]: ...


class GraphReadUnitOfWork(Protocol):
    datasets: DatasetRepositoryForGraphRead
    entities: EntityRepositoryForGraphRead
    relations: RelationRepositoryForGraphRead

    async def __aenter__(self) -> GraphReadUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


type UnitOfWorkFactory = Callable[[], GraphReadUnitOfWork]


class GraphTraversalClient(Protocol):
    async def subgraph_entity_ids(
        self,
        *,
        root_entity_id: UUID,
        dataset_id: UUID,
        depth: int,
        max_nodes: int,
    ) -> list[UUID]: ...

    async def relations_among(
        self,
        *,
        entity_ids: Sequence[UUID],
        dataset_id: UUID,
        max_relations: int,
    ) -> list[GraphRelationEdge]: ...

    async def shortest_path(
        self,
        *,
        from_entity_id: UUID,
        to_entity_id: UUID,
        dataset_id: UUID,
        max_depth: int,
    ) -> GraphPathRecord | None: ...


class GraphReadService:
    """Bounded graph schema/subgraph/path reads with PostgreSQL as authority."""

    def __init__(
        self,
        settings: Settings,
        *,
        graph_client: GraphTraversalClient | None = None,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._graph_client = graph_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def schema(self, *, dataset_slug: str) -> GraphSchemaResult:
        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset(uow, dataset_slug)
            # Capture plain scalar values while the session is still open:
            # the ORM Dataset instance is detached once __aexit__ closes the
            # session, and accessing its attributes afterwards raises
            # DetachedInstanceError against a real PostgreSQL session.
            dataset_id = dataset.id
            resolved_slug = dataset.slug
            entity_types = await uow.entities.list_active_current_entity_types(
                dataset_id=dataset_id
            )
            predicates = await uow.relations.list_active_current_predicates(dataset_id=dataset_id)
        return GraphSchemaResult(
            dataset_id=dataset_id,
            dataset=resolved_slug,
            entity_types=entity_types,
            relation_predicates=predicates,
        )

    async def subgraph(
        self,
        *,
        dataset_slug: str,
        entity_id: UUID,
        depth: int,
    ) -> GraphSubgraphResult:
        self._validate_subgraph_depth(depth)
        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset(uow, dataset_slug)
            dataset_id = dataset.id
            root = await uow.entities.get_active_current_by_id(
                dataset_id=dataset_id, entity_id=entity_id
            )
            if root is None:
                raise _not_found_error(ENTITY_NOT_FOUND_MESSAGE, {"entity_id": str(entity_id)})

        max_nodes = self._settings.recall_graph_max_nodes
        neo4j_ids = await self._traverse_subgraph(
            root_entity_id=entity_id, dataset_id=dataset_id, depth=depth, max_nodes=max_nodes
        )
        raw_candidate_ids = unique_ordered([entity_id, *neo4j_ids])
        candidate_ids = raw_candidate_ids[:max_nodes]

        async with self._unit_of_work_factory() as uow:
            entities = await uow.entities.list_active_current_by_ids(
                dataset_id=dataset_id, entity_ids=candidate_ids
            )
            hydrated_ids = [entity.id for entity in entities]
            edges = await self._relations_among(entity_ids=hydrated_ids, dataset_id=dataset_id)
            relation_ids = unique_ordered(edge.relation_id for edge in edges)
            relations = await uow.relations.list_active_current_by_ids(
                dataset_id=dataset_id, relation_ids=relation_ids
            )

            # Build response models while the session is still open; the ORM
            # rows above become detached once __aexit__ closes the session.
            hydrated_id_set = set(hydrated_ids)
            entity_nodes = [_entity_node(entity) for entity in entities]
            relation_edges = [
                _relation_edge(relation)
                for relation in relations
                if relation.source_entity_id in hydrated_id_set
                and relation.target_entity_id in hydrated_id_set
            ]

        truncated = len(raw_candidate_ids) > max_nodes or len(neo4j_ids) >= max_nodes
        return GraphSubgraphResult(
            dataset_id=dataset_id,
            root_entity_id=entity_id,
            depth=depth,
            entities=entity_nodes,
            relations=relation_edges,
            truncated=truncated,
        )

    async def path(
        self,
        *,
        dataset_slug: str,
        from_entity_id: UUID,
        to_entity_id: UUID,
        max_depth: int,
    ) -> GraphPathResult:
        self._validate_path_max_depth(max_depth)
        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset(uow, dataset_slug)
            dataset_id = dataset.id
            endpoints = await uow.entities.list_active_current_by_ids(
                dataset_id=dataset_id, entity_ids=[from_entity_id, to_entity_id]
            )
            endpoint_ids = {entity.id for entity in endpoints}
            same_entity_node = None
            if from_entity_id == to_entity_id and from_entity_id in endpoint_ids:
                same_entity = next(entity for entity in endpoints if entity.id == from_entity_id)
                same_entity_node = _entity_node(same_entity)

        if from_entity_id not in endpoint_ids:
            raise _not_found_error(ENTITY_NOT_FOUND_MESSAGE, {"entity_id": str(from_entity_id)})
        if to_entity_id not in endpoint_ids:
            raise _not_found_error(ENTITY_NOT_FOUND_MESSAGE, {"entity_id": str(to_entity_id)})

        if same_entity_node is not None:
            return GraphPathResult(
                dataset_id=dataset_id,
                from_entity_id=from_entity_id,
                to_entity_id=to_entity_id,
                max_depth=max_depth,
                found=True,
                entities=[same_entity_node],
                relations=[],
            )

        record = await self._shortest_path(
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            dataset_id=dataset_id,
            max_depth=max_depth,
        )
        empty_result = GraphPathResult(
            dataset_id=dataset_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            max_depth=max_depth,
            found=False,
            entities=[],
            relations=[],
        )
        if record is None or not record.entity_ids:
            return empty_result

        relation_ids = unique_ordered(edge.relation_id for edge in record.edges)
        async with self._unit_of_work_factory() as uow:
            entities = await uow.entities.list_active_current_by_ids(
                dataset_id=dataset_id, entity_ids=record.entity_ids
            )
            relations = await uow.relations.list_active_current_by_ids(
                dataset_id=dataset_id, relation_ids=relation_ids
            )

            entities_by_id = {entity.id: entity for entity in entities}
            relations_by_id = {relation.id: relation for relation in relations}
            if len(entities_by_id) != len(record.entity_ids):
                return empty_result
            if len(relations_by_id) != len(record.edges):
                return empty_result

            # Build response models while the session is still open.
            entity_nodes = [
                _entity_node(entities_by_id[entity_id]) for entity_id in record.entity_ids
            ]
            relation_edges = [
                _relation_edge(relations_by_id[edge.relation_id]) for edge in record.edges
            ]

        return GraphPathResult(
            dataset_id=dataset_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            max_depth=max_depth,
            found=True,
            entities=entity_nodes,
            relations=relation_edges,
        )

    def _validate_subgraph_depth(self, depth: int) -> None:
        if depth < 1 or depth > self._settings.graph_subgraph_max_depth:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Subgraph depth is out of the supported range.",
                details={
                    "depth": depth,
                    "max_depth": self._settings.graph_subgraph_max_depth,
                },
            )

    def _validate_path_max_depth(self, max_depth: int) -> None:
        if max_depth < 1 or max_depth > self._settings.graph_path_max_depth:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Path max_depth is out of the supported range.",
                details={
                    "max_depth": max_depth,
                    "hard_limit": self._settings.graph_path_max_depth,
                },
            )

    async def _require_active_dataset(self, uow: GraphReadUnitOfWork, dataset_slug: str) -> Dataset:
        dataset = await uow.datasets.get_by_slug(dataset_slug)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            raise _not_found_error(DATASET_NOT_FOUND_MESSAGE, {"dataset": dataset_slug})
        return dataset

    async def _traverse_subgraph(
        self, *, root_entity_id: UUID, dataset_id: UUID, depth: int, max_nodes: int
    ) -> list[UUID]:
        if self._graph_client is None:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.")
        try:
            return await self._graph_client.subgraph_entity_ids(
                root_entity_id=root_entity_id,
                dataset_id=dataset_id,
                depth=depth,
                max_nodes=max_nodes,
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.") from exc

    async def _relations_among(
        self, *, entity_ids: Sequence[UUID], dataset_id: UUID
    ) -> list[GraphRelationEdge]:
        if self._graph_client is None:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.")
        if not entity_ids:
            return []
        try:
            return await self._graph_client.relations_among(
                entity_ids=entity_ids,
                dataset_id=dataset_id,
                max_relations=self._settings.graph_subgraph_max_relations,
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.") from exc

    async def _shortest_path(
        self,
        *,
        from_entity_id: UUID,
        to_entity_id: UUID,
        dataset_id: UUID,
        max_depth: int,
    ) -> GraphPathRecord | None:
        if self._graph_client is None:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.")
        try:
            return await self._graph_client.shortest_path(
                from_entity_id=from_entity_id,
                to_entity_id=to_entity_id,
                dataset_id=dataset_id,
                max_depth=max_depth,
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph traversal is unavailable.") from exc


def _not_found_error(message: str, details: dict[str, str]) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message=message,
        details=details,
    )


def _entity_node(entity: Entity) -> GraphEntityNode:
    return GraphEntityNode(
        entity_id=entity.id,
        name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        importance_weight=entity.importance_weight,
    )


def _relation_edge(relation: Relation) -> GraphRelationEdgeModel:
    return GraphRelationEdgeModel(
        relation_id=relation.id,
        source_entity_id=relation.source_entity_id,
        target_entity_id=relation.target_entity_id,
        predicate=relation.predicate,
        description=relation.description,
        confidence=relation.confidence,
        importance_weight=relation.importance_weight,
    )


def unique_ordered(items: Iterable[UUID]) -> list[UUID]:
    unique: list[UUID] = []
    seen: set[UUID] = set()
    for item in items:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> GraphReadUnitOfWork:
        return cast(GraphReadUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
