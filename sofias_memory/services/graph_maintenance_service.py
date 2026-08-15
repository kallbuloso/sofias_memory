"""PostgreSQL-authoritative graph hygiene and centrality maintenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, Relation
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import (
    ProjectionCommand,
    entity_upsert_command,
    relation_delete_command,
    relation_upsert_command,
)

IMPORTANCE_MARKER_KEY = "_sofias_memory_importance"
IMPORTANCE_MARKER_VERSION = "degree-v1"
IMPORTANCE_DECIMALS = 4


@dataclass(frozen=True)
class ImportanceComponents:
    feedback_weight: float
    centrality_weight: float

    @property
    def effective_weight(self) -> float:
        return effective_importance(self.feedback_weight, self.centrality_weight)


@dataclass(frozen=True)
class GraphMaintenanceCounts:
    relations_deactivated: int
    entities_importance_updated: int
    relations_importance_updated: int
    graph_events_enqueued: int


class DatasetRepositoryForGraphMaintenance(Protocol):
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...


class EntityRepositoryForGraphMaintenance(Protocol):
    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Entity]: ...


class RelationRepositoryForGraphMaintenance(Protocol):
    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Relation]: ...


class RelationEvidenceRepositoryForGraphMaintenance(Protocol):
    async def list_relation_ids_with_authoritative_evidence(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> set[UUID]: ...


class GraphOutboxRepositoryForGraphMaintenance(Protocol):
    async def add_projection_command(self, command: ProjectionCommand) -> object: ...


class GraphMaintenanceUnitOfWork(Protocol):
    datasets: DatasetRepositoryForGraphMaintenance
    entities: EntityRepositoryForGraphMaintenance
    relations: RelationRepositoryForGraphMaintenance
    relation_evidence: RelationEvidenceRepositoryForGraphMaintenance
    graph_outbox: GraphOutboxRepositoryForGraphMaintenance

    async def __aenter__(self) -> GraphMaintenanceUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], GraphMaintenanceUnitOfWork]


class GraphMaintenanceService:
    """Maintain graph hygiene and deterministic importance in PostgreSQL."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def maintain_dataset(
        self,
        dataset_id: UUID,
        *,
        generation: int,
    ) -> GraphMaintenanceCounts:
        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_id(dataset_id)
            if (
                dataset is None
                or dataset.status != DatasetStatus.ACTIVE
                or dataset.active_generation != generation
            ):
                return GraphMaintenanceCounts(0, 0, 0, 0)

            entities = await uow.entities.list_active_current_for_dataset(dataset_id=dataset_id)
            relations = await uow.relations.list_active_current_for_dataset(dataset_id=dataset_id)
            valid_relation_ids = (
                await uow.relation_evidence.list_relation_ids_with_authoritative_evidence(
                    dataset_id=dataset_id,
                    relation_ids=[relation.id for relation in relations],
                )
            )

            graph_commands: list[ProjectionCommand] = []
            relations_deactivated = 0
            active_relations: list[Relation] = []
            for relation in relations:
                if relation.id not in valid_relation_ids:
                    relation.is_active = False
                    relation.embedding = None
                    relations_deactivated += 1
                    graph_commands.append(
                        relation_delete_command(
                            relation_id=relation.id,
                            dataset_id=dataset_id,
                            source_entity_id=relation.source_entity_id,
                            target_entity_id=relation.target_entity_id,
                        )
                    )
                    continue
                active_relations.append(relation)

            centrality_by_entity_id = entity_centrality(
                entities=entities,
                relations=active_relations,
            )
            entities_importance_updated = update_entity_importance(
                entities,
                centrality_by_entity_id,
                graph_commands,
            )
            relations_importance_updated = update_relation_importance(
                active_relations,
                centrality_by_entity_id,
                graph_commands,
            )

            for command in graph_commands:
                await uow.graph_outbox.add_projection_command(command)
            if graph_commands:
                await uow.commit()

            return GraphMaintenanceCounts(
                relations_deactivated=relations_deactivated,
                entities_importance_updated=entities_importance_updated,
                relations_importance_updated=relations_importance_updated,
                graph_events_enqueued=len(graph_commands),
            )


def update_entity_importance(
    entities: Sequence[Entity],
    centrality_by_entity_id: dict[UUID, float],
    graph_commands: list[ProjectionCommand],
) -> int:
    updated = 0
    for entity in sorted(entities, key=lambda item: item.id):
        centrality = centrality_by_entity_id.get(entity.id, 0.0)
        current_properties = dict(entity.properties)
        current_weight = float(entity.importance_weight)
        next_components = next_importance_components(current_properties, current_weight, centrality)
        next_properties = properties_with_importance_marker(current_properties, next_components)
        next_weight = next_components.effective_weight
        if not _importance_changed(
            current_properties, current_weight, next_properties, next_weight
        ):
            continue
        entity.properties = next_properties
        entity.importance_weight = next_weight
        updated += 1
        graph_commands.append(
            entity_upsert_command(
                entity_id=entity.id,
                dataset_id=entity.dataset_id,
                name=entity.name,
                entity_type=entity.entity_type,
                description=entity.description,
                importance_weight=next_weight,
                generation=entity.generation,
            )
        )
    return updated


def update_relation_importance(
    relations: Sequence[Relation],
    centrality_by_entity_id: dict[UUID, float],
    graph_commands: list[ProjectionCommand],
) -> int:
    updated = 0
    for relation in sorted(relations, key=lambda item: item.id):
        centrality = round(
            (
                centrality_by_entity_id.get(relation.source_entity_id, 0.0)
                + centrality_by_entity_id.get(relation.target_entity_id, 0.0)
            )
            / 2.0,
            IMPORTANCE_DECIMALS,
        )
        current_properties = dict(relation.properties)
        current_weight = float(relation.importance_weight)
        next_components = next_importance_components(current_properties, current_weight, centrality)
        next_properties = properties_with_importance_marker(current_properties, next_components)
        next_weight = next_components.effective_weight
        if not _importance_changed(
            current_properties, current_weight, next_properties, next_weight
        ):
            continue
        relation.properties = next_properties
        relation.importance_weight = next_weight
        updated += 1
        graph_commands.append(
            relation_upsert_command(
                relation_id=relation.id,
                dataset_id=relation.dataset_id,
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
                predicate=relation.predicate,
                description=relation.description,
                confidence=float(relation.confidence),
                importance_weight=next_weight,
                generation=relation.generation,
            )
        )
    return updated


def entity_centrality(
    *,
    entities: Sequence[Entity],
    relations: Sequence[Relation],
) -> dict[UUID, float]:
    degree_by_entity_id = {entity.id: 0 for entity in entities}
    for relation in relations:
        if relation.source_entity_id == relation.target_entity_id:
            continue
        if relation.source_entity_id in degree_by_entity_id:
            degree_by_entity_id[relation.source_entity_id] += 1
        if relation.target_entity_id in degree_by_entity_id:
            degree_by_entity_id[relation.target_entity_id] += 1
    max_degree = max(degree_by_entity_id.values(), default=0)
    if max_degree == 0:
        return {entity_id: 0.0 for entity_id in degree_by_entity_id}
    return {
        entity_id: round(degree / max_degree, IMPORTANCE_DECIMALS)
        for entity_id, degree in degree_by_entity_id.items()
    }


def next_importance_components(
    properties: dict[str, object],
    current_importance_weight: float,
    centrality_weight: float,
) -> ImportanceComponents:
    current_components = importance_components_from_properties(
        properties,
        fallback_feedback_weight=current_importance_weight,
    )
    return ImportanceComponents(
        feedback_weight=current_components.feedback_weight,
        centrality_weight=centrality_weight,
    )


def importance_components_from_properties(
    properties: dict[str, object],
    *,
    fallback_feedback_weight: float,
) -> ImportanceComponents:
    components = valid_importance_components_from_properties(properties)
    if components is None:
        return ImportanceComponents(
            feedback_weight=clamp_weight(fallback_feedback_weight),
            centrality_weight=0.0,
        )
    return components


def valid_importance_components_from_properties(
    properties: Mapping[str, object],
) -> ImportanceComponents | None:
    marker = properties.get(IMPORTANCE_MARKER_KEY)
    if not isinstance(marker, Mapping) or marker.get("version") != IMPORTANCE_MARKER_VERSION:
        return None

    feedback_weight = marker.get("feedback_weight")
    centrality_weight = marker.get("centrality_weight")
    if not _is_valid_importance_weight(feedback_weight) or not _is_valid_importance_weight(
        centrality_weight
    ):
        return None
    feedback_weight_value = cast(Real, feedback_weight)
    centrality_weight_value = cast(Real, centrality_weight)

    return ImportanceComponents(
        feedback_weight=clamp_weight(float(feedback_weight_value)),
        centrality_weight=clamp_weight(float(centrality_weight_value)),
    )


def properties_with_importance_marker(
    properties: dict[str, object],
    components: ImportanceComponents,
) -> dict[str, object]:
    updated = dict(properties)
    updated[IMPORTANCE_MARKER_KEY] = {
        "version": IMPORTANCE_MARKER_VERSION,
        "feedback_weight": components.feedback_weight,
        "centrality_weight": components.centrality_weight,
    }
    return updated


def has_importance_marker(properties: dict[str, object]) -> bool:
    return valid_importance_components_from_properties(properties) is not None


def effective_importance(feedback_weight: float, centrality_weight: float) -> float:
    return round((clamp_weight(feedback_weight) + clamp_weight(centrality_weight)) / 2.0, 4)


def clamp_weight(value: float) -> float:
    numeric_value = float(value)
    if not isfinite(numeric_value):
        return 0.0
    return round(max(0.0, min(1.0, numeric_value)), IMPORTANCE_DECIMALS)


def _is_valid_importance_weight(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    numeric_value = float(value)
    return isfinite(numeric_value) and 0.0 <= numeric_value <= 1.0


def _importance_changed(
    current_properties: dict[str, object],
    current_weight: float,
    next_properties: dict[str, object],
    next_weight: float,
) -> bool:
    return current_properties != next_properties or round(current_weight, 4) != next_weight


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> GraphMaintenanceUnitOfWork:
        return cast(GraphMaintenanceUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
