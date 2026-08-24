from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, Relation, RelationEvidence
from sofias_memory.ports import ProjectionCommand
from sofias_memory.services.graph_maintenance_service import (
    IMPORTANCE_MARKER_KEY,
    GraphMaintenanceService,
    GraphMaintenanceUnitOfWork,
    ImportanceComponents,
    UnitOfWorkFactory,
    has_importance_marker,
    properties_with_importance_marker,
    valid_importance_components_from_properties,
)


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self.relation_evidence: list[RelationEvidence] = []
        self.valid_relation_ids: set[UUID] = set()
        self.graph_commands: list[ProjectionCommand] = []
        self.commits = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.id == dataset_id), None)


class FakeEntityRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Entity]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        return [
            entity
            for entity in sorted(self._store.entities, key=lambda item: item.id)
            if entity.dataset_id == dataset_id
            and entity.generation == dataset.active_generation
            and entity.is_active
        ]

    async def get_active_current_by_id(self, *, dataset_id: UUID, entity_id: UUID) -> Entity | None:
        matches = await self.list_active_current_for_dataset(dataset_id=dataset_id)
        return next((entity for entity in matches if entity.id == entity_id), None)


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Relation]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        entities_by_id = {entity.id: entity for entity in self._store.entities}
        relations: list[Relation] = []
        for relation in sorted(self._store.relations, key=lambda item: item.id):
            source = entities_by_id.get(relation.source_entity_id)
            target = entities_by_id.get(relation.target_entity_id)
            if (
                source is None
                or target is None
                or relation.dataset_id != dataset_id
                or relation.generation != dataset.active_generation
                or not relation.is_active
                or source.dataset_id != dataset_id
                or source.generation != dataset.active_generation
                or not source.is_active
                or target.dataset_id != dataset_id
                or target.generation != dataset.active_generation
                or not target.is_active
            ):
                continue
            relations.append(relation)
        return relations

    async def get_active_current_by_id(
        self, *, dataset_id: UUID, relation_id: UUID
    ) -> Relation | None:
        matches = await self.list_active_current_for_dataset(dataset_id=dataset_id)
        return next((relation for relation in matches if relation.id == relation_id), None)


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_relation_ids_with_authoritative_evidence(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> set[UUID]:
        del dataset_id
        requested_ids = set(relation_ids)
        return self._store.valid_relation_ids & requested_ids


class FakeGraphOutboxRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add_projection_command(self, command: ProjectionCommand) -> object:
        self._store.graph_commands.append(command)
        return object()


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.datasets = FakeDatasetRepository(store)
        self.entities = FakeEntityRepository(store)
        self.relations = FakeRelationRepository(store)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.graph_outbox = FakeGraphOutboxRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> GraphMaintenanceService:
    def create_uow() -> GraphMaintenanceUnitOfWork:
        return cast(GraphMaintenanceUnitOfWork, FakeUnitOfWork(store))

    return GraphMaintenanceService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


@pytest.mark.asyncio
async def test_graph_maintenance_hygiene_centrality_outbox_and_idempotency() -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    first = entity(dataset.id)
    second = entity(dataset.id)
    isolated = entity(dataset.id)
    store.entities.extend([first, second, isolated])
    valid_relation = relation(dataset.id, first.id, second.id, weight=0.5)
    invalid_relation = relation(dataset.id, second.id, isolated.id, weight=0.5)
    store.relations.extend([invalid_relation, valid_relation])
    store.relation_evidence.append(
        RelationEvidence(
            relation_id=invalid_relation.id,
            chunk_id=uuid4(),
            quote="historical evidence is preserved",
            confidence=0.9,
        )
    )
    store.valid_relation_ids = {valid_relation.id}
    service = service_for(store)

    result = await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert result.relations_deactivated == 1
    assert result.entities_importance_updated == 3
    assert result.relations_importance_updated == 1
    assert result.graph_events_enqueued == 5
    assert invalid_relation.is_active is False
    assert store.relation_evidence
    assert first.importance_weight == 0.75
    assert second.importance_weight == 0.75
    assert isolated.importance_weight == 0.25
    assert valid_relation.importance_weight == 0.75
    assert store.commits == 1
    assert [command.operation for command in store.graph_commands] == [
        "delete",
        "upsert",
        "upsert",
        "upsert",
        "upsert",
    ]
    assert store.graph_commands[0].aggregate_type == "relation"
    assert store.graph_commands[0].aggregate_id == str(invalid_relation.id)

    previous_command_count = len(store.graph_commands)
    rerun = await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert rerun.relations_deactivated == 0
    assert rerun.entities_importance_updated == 0
    assert rerun.relations_importance_updated == 0
    assert rerun.graph_events_enqueued == 0
    assert len(store.graph_commands) == previous_command_count
    assert store.commits == 1


@pytest.mark.asyncio
async def test_graph_maintenance_ignores_other_dataset_and_stale_generation() -> None:
    store = FakeStore()
    dataset = seed_dataset(store, generation=2)
    other_dataset = seed_dataset(store, generation=2)
    current = entity(dataset.id, generation=2)
    stale = entity(dataset.id, generation=1)
    other = entity(other_dataset.id, generation=2)
    store.entities.extend([current, stale, other])
    stale_relation = relation(dataset.id, current.id, stale.id, generation=2)
    other_relation = relation(other_dataset.id, other.id, other.id, generation=2)
    store.relations.extend([stale_relation, other_relation])
    service = service_for(store)

    result = await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert result.relations_deactivated == 0
    assert result.graph_events_enqueued == 1
    assert current.importance_weight == 0.25
    assert stale.importance_weight == 0.5
    assert other.importance_weight == 0.5
    assert stale_relation.is_active is True
    assert other_relation.is_active is True


@pytest.mark.parametrize(
    ("valid_relation_ids", "expected_active"),
    [
        (set(), False),
        ({UUID("10000000-0000-0000-0000-000000000001")}, True),
    ],
)
@pytest.mark.asyncio
async def test_only_authoritative_evidence_keeps_relation_active(
    valid_relation_ids: set[UUID],
    expected_active: bool,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    source = entity(dataset.id)
    target = entity(dataset.id)
    store.entities.extend([source, target])
    target_relation = relation(dataset.id, source.id, target.id)
    target_relation.id = UUID("10000000-0000-0000-0000-000000000001")
    store.relations.append(target_relation)
    store.valid_relation_ids = set(valid_relation_ids)
    service = service_for(store)

    await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert target_relation.is_active is expected_active


def test_valid_importance_marker_preserves_components() -> None:
    properties = properties_with_importance_marker(
        {"kept": True},
        ImportanceComponents(feedback_weight=0.3, centrality_weight=0.9),
    )

    components = valid_importance_components_from_properties(properties)

    assert components == ImportanceComponents(feedback_weight=0.3, centrality_weight=0.9)
    assert has_importance_marker(properties) is True


@pytest.mark.parametrize(
    "marker",
    [
        {"version": "degree-v1", "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": 0.5},
        {"version": "unknown", "feedback_weight": 0.5, "centrality_weight": 0.5},
        "degree-v1",
        {"version": "degree-v1", "feedback_weight": "0.5", "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": True, "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": float("nan"), "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": float("inf"), "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": -0.1, "centrality_weight": 0.5},
        {"version": "degree-v1", "feedback_weight": 0.5, "centrality_weight": 1.1},
    ],
)
def test_invalid_importance_markers_are_treated_as_legacy(marker: object) -> None:
    properties = {IMPORTANCE_MARKER_KEY: marker}

    assert valid_importance_components_from_properties(properties) is None
    assert has_importance_marker(properties) is False


@pytest.mark.asyncio
async def test_graph_maintenance_repairs_invalid_marker_and_is_idempotent() -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    target = entity(dataset.id, weight=0.6)
    target.properties = {IMPORTANCE_MARKER_KEY: {"version": "degree-v1", "centrality_weight": 1.0}}
    store.entities.append(target)
    service = service_for(store)

    result = await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert result.entities_importance_updated == 1
    assert result.graph_events_enqueued == 1
    assert target.importance_weight == 0.3
    assert target.properties[IMPORTANCE_MARKER_KEY] == {
        "version": "degree-v1",
        "feedback_weight": 0.6,
        "centrality_weight": 0.0,
    }

    rerun = await service.maintain_dataset(dataset.id, generation=dataset.active_generation)

    assert rerun.entities_importance_updated == 0
    assert rerun.graph_events_enqueued == 0
    assert target.importance_weight == 0.3


def seed_dataset(store: FakeStore, *, generation: int = 0) -> Dataset:
    dataset = Dataset(
        id=uuid4(),
        name="main",
        slug=f"main-{len(store.datasets)}",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=generation,
    )
    store.datasets.append(dataset)
    return dataset


def entity(dataset_id: UUID, *, generation: int = 0, weight: float = 0.5) -> Entity:
    return Entity(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        canonical_key=f"entity:{uuid4()}",
        name="Entity",
        entity_type="Concept",
        description="Entity.",
        aliases=[],
        properties={},
        confidence=0.9,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )


def relation(
    dataset_id: UUID,
    source_entity_id: UUID,
    target_entity_id: UUID,
    *,
    generation: int = 0,
    weight: float = 0.5,
) -> Relation:
    return Relation(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate="relates_to",
        description="Relates to.",
        properties=properties_with_importance_marker(
            {},
            ImportanceComponents(feedback_weight=weight, centrality_weight=0.0),
        ),
        confidence=0.8,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )
