from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.neo4j.graph_read import GraphPathRecord, GraphRelationEdge
from sofias_memory.infrastructure.postgres.models import Dataset, Entity, Relation
from sofias_memory.services.graph_read import (
    GraphReadService,
    GraphReadUnitOfWork,
    UnitOfWorkFactory,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "recall_graph_max_nodes": 10,
        "graph_subgraph_max_depth": 3,
        "graph_subgraph_max_relations": 50,
        "graph_path_max_depth": 4,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_dataset(slug: str = "main", *, status: DatasetStatus = DatasetStatus.ACTIVE) -> Dataset:
    return Dataset(
        id=uuid4(),
        name=slug,
        slug=slug,
        description=None,
        status=status,
        active_generation=0,
    )


def make_entity(
    dataset_id: UUID,
    *,
    entity_id: UUID | None = None,
    entity_type: str = "person",
    generation: int = 0,
    is_active: bool = True,
    name: str = "Ada Lovelace",
) -> Entity:
    return Entity(
        id=entity_id or uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        canonical_key=name.lower(),
        name=name,
        entity_type=entity_type,
        description="A description.",
        aliases=[],
        properties={},
        confidence=0.9,
        importance_weight=0.5,
        embedding=None,
        is_active=is_active,
    )


def make_relation(
    dataset_id: UUID,
    *,
    relation_id: UUID | None = None,
    source_entity_id: UUID,
    target_entity_id: UUID,
    predicate: str = "knows",
    generation: int = 0,
    is_active: bool = True,
) -> Relation:
    return Relation(
        id=relation_id or uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate=predicate,
        description="A relation.",
        properties={},
        confidence=0.8,
        importance_weight=0.5,
        embedding=None,
        is_active=is_active,
    )


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_slug(self, slug: str) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)


def _entity_is_active_current(entity: Entity, dataset: Dataset) -> bool:
    return (
        entity.dataset_id == dataset.id
        and entity.generation == dataset.active_generation
        and entity.is_active
        and dataset.status == DatasetStatus.ACTIVE
    )


class FakeEntityRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _dataset(self, dataset_id: UUID) -> Dataset | None:
        return next((d for d in self._store.datasets if d.id == dataset_id), None)

    async def get_active_current_by_id(self, *, dataset_id: UUID, entity_id: UUID) -> Entity | None:
        entities = await self.list_active_current_by_ids(
            dataset_id=dataset_id, entity_ids=[entity_id]
        )
        return entities[0] if entities else None

    async def list_active_current_by_ids(
        self, *, dataset_id: UUID, entity_ids: list[UUID]
    ) -> list[Entity]:
        dataset = self._dataset(dataset_id)
        if dataset is None:
            return []
        ids = set(entity_ids)
        return [
            entity
            for entity in self._store.entities
            if entity.id in ids and _entity_is_active_current(entity, dataset)
        ]

    async def list_active_current_entity_types(self, *, dataset_id: UUID) -> list[str]:
        dataset = self._dataset(dataset_id)
        if dataset is None:
            return []
        types = {
            entity.entity_type
            for entity in self._store.entities
            if _entity_is_active_current(entity, dataset)
        }
        return sorted(types)


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _dataset(self, dataset_id: UUID) -> Dataset | None:
        return next((d for d in self._store.datasets if d.id == dataset_id), None)

    async def list_active_current_by_ids(
        self, *, dataset_id: UUID, relation_ids: list[UUID]
    ) -> list[Relation]:
        dataset = self._dataset(dataset_id)
        if dataset is None:
            return []
        ids = set(relation_ids)
        active_entity_ids = {
            entity.id
            for entity in self._store.entities
            if _entity_is_active_current(entity, dataset)
        }
        return [
            relation
            for relation in self._store.relations
            if relation.id in ids
            and relation.dataset_id == dataset.id
            and relation.generation == dataset.active_generation
            and relation.is_active
            and relation.source_entity_id in active_entity_ids
            and relation.target_entity_id in active_entity_ids
        ]

    async def list_active_current_predicates(self, *, dataset_id: UUID) -> list[str]:
        dataset = self._dataset(dataset_id)
        if dataset is None:
            return []
        active_entity_ids = {
            entity.id
            for entity in self._store.entities
            if _entity_is_active_current(entity, dataset)
        }
        predicates = {
            relation.predicate
            for relation in self._store.relations
            if relation.dataset_id == dataset.id
            and relation.generation == dataset.active_generation
            and relation.is_active
            and relation.source_entity_id in active_entity_ids
            and relation.target_entity_id in active_entity_ids
        }
        return sorted(predicates)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.entities = FakeEntityRepository(store)
        self.relations = FakeRelationRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class FakeGraphClient:
    def __init__(
        self,
        *,
        subgraph_ids: list[UUID] | None = None,
        edges: list[GraphRelationEdge] | None = None,
        path: GraphPathRecord | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.subgraph_ids = subgraph_ids or []
        self.edges = edges or []
        self.path = path
        self.raise_error = raise_error
        self.subgraph_calls: list[dict[str, object]] = []
        self.relations_calls: list[dict[str, object]] = []
        self.path_calls: list[dict[str, object]] = []

    async def subgraph_entity_ids(self, **kwargs: object) -> list[UUID]:
        if self.raise_error is not None:
            raise self.raise_error
        self.subgraph_calls.append(kwargs)
        return self.subgraph_ids

    async def relations_among(self, **kwargs: object) -> list[GraphRelationEdge]:
        if self.raise_error is not None:
            raise self.raise_error
        self.relations_calls.append(kwargs)
        return self.edges

    async def shortest_path(self, **kwargs: object) -> GraphPathRecord | None:
        if self.raise_error is not None:
            raise self.raise_error
        self.path_calls.append(kwargs)
        return self.path


def service_for(
    tmp_path: Path,
    store: FakeStore,
    *,
    graph_client: FakeGraphClient | None = None,
    settings: Settings | None = None,
) -> GraphReadService:
    def create_uow() -> GraphReadUnitOfWork:
        return cast(GraphReadUnitOfWork, FakeUnitOfWork(store))

    return GraphReadService(
        settings or make_settings(tmp_path),
        graph_client=graph_client,
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
    )


# --- GRAPH SCHEMA -----------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_dataset_not_found_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.schema(dataset_slug="missing")

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_schema_dataset_deleting_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    store.datasets.append(make_dataset("main", status=DatasetStatus.DELETING))
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.schema(dataset_slug="main")

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_schema_returns_unique_deterministic_entity_types_and_predicates(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    entity_a = make_entity(dataset.id, entity_type="person")
    entity_b = make_entity(dataset.id, entity_type="person")
    entity_c = make_entity(dataset.id, entity_type="place")
    store.entities.extend([entity_a, entity_b, entity_c])
    store.relations.append(
        make_relation(
            dataset.id,
            source_entity_id=entity_a.id,
            target_entity_id=entity_b.id,
            predicate="knows",
        )
    )
    store.relations.append(
        make_relation(
            dataset.id,
            source_entity_id=entity_a.id,
            target_entity_id=entity_c.id,
            predicate="knows",
        )
    )
    service = service_for(tmp_path, store)

    result = await service.schema(dataset_slug="main")

    assert result.entity_types == ["person", "place"]
    assert result.relation_predicates == ["knows"]
    assert result.node_labels == ["Entity", "Chunk"]
    assert result.relationship_types == ["RELATES_TO", "MENTIONED_IN", "NEXT"]


@pytest.mark.asyncio
async def test_schema_does_not_leak_other_dataset_types(tmp_path: Path) -> None:
    store = FakeStore()
    dataset_a = make_dataset("dataset-a")
    dataset_b = make_dataset("dataset-b")
    store.datasets.extend([dataset_a, dataset_b])
    store.entities.append(make_entity(dataset_b.id, entity_type="secret-type"))
    service = service_for(tmp_path, store)

    result = await service.schema(dataset_slug="dataset-a")

    assert result.entity_types == []


# --- SUBGRAPH ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_subgraph_root_valid_hydrates_entities_and_relations(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    neighbor = make_entity(dataset.id)
    store.entities.extend([root, neighbor])
    relation = make_relation(dataset.id, source_entity_id=root.id, target_entity_id=neighbor.id)
    store.relations.append(relation)
    graph_client = FakeGraphClient(
        subgraph_ids=[neighbor.id],
        edges=[
            GraphRelationEdge(
                relation_id=relation.id,
                source_entity_id=root.id,
                target_entity_id=neighbor.id,
            )
        ],
    )
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.subgraph(dataset_slug="main", entity_id=root.id, depth=2)

    assert {entity.entity_id for entity in result.entities} == {root.id, neighbor.id}
    assert [rel.relation_id for rel in result.relations] == [relation.id]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_subgraph_root_missing_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    store.datasets.append(make_dataset("main"))
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.subgraph(dataset_slug="main", entity_id=uuid4(), depth=2)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_subgraph_root_inactive_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    inactive_root = make_entity(dataset.id, is_active=False)
    store.entities.append(inactive_root)
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.subgraph(dataset_slug="main", entity_id=inactive_root.id, depth=2)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_subgraph_depth_zero_returns_400(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.subgraph(dataset_slug="main", entity_id=root.id, depth=0)

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_subgraph_depth_above_limit_returns_400(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    service = service_for(
        tmp_path, store, graph_client=FakeGraphClient(), settings=make_settings(tmp_path)
    )

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.subgraph(dataset_slug="main", entity_id=root.id, depth=4)

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_subgraph_stale_neo4j_ids_are_filtered_by_postgres(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    stale_id = uuid4()  # Neo4j still has it projected, PostgreSQL does not (or inactive).
    graph_client = FakeGraphClient(subgraph_ids=[stale_id])
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.subgraph(dataset_slug="main", entity_id=root.id, depth=2)

    assert {entity.entity_id for entity in result.entities} == {root.id}


@pytest.mark.asyncio
async def test_subgraph_does_not_leak_other_dataset_entities(tmp_path: Path) -> None:
    store = FakeStore()
    dataset_a = make_dataset("dataset-a")
    dataset_b = make_dataset("dataset-b")
    store.datasets.extend([dataset_a, dataset_b])
    root = make_entity(dataset_a.id)
    other_dataset_entity = make_entity(dataset_b.id)
    store.entities.extend([root, other_dataset_entity])
    graph_client = FakeGraphClient(subgraph_ids=[other_dataset_entity.id])
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.subgraph(dataset_slug="dataset-a", entity_id=root.id, depth=2)

    assert {entity.entity_id for entity in result.entities} == {root.id}


@pytest.mark.asyncio
async def test_subgraph_result_is_limited_to_max_nodes(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    extra_ids = [uuid4() for _ in range(20)]
    for extra_id in extra_ids:
        store.entities.append(make_entity(dataset.id, entity_id=extra_id))
    graph_client = FakeGraphClient(subgraph_ids=extra_ids)
    service = service_for(
        tmp_path,
        store,
        graph_client=graph_client,
        settings=make_settings(tmp_path, recall_graph_max_nodes=5),
    )

    result = await service.subgraph(dataset_slug="main", entity_id=root.id, depth=2)

    assert len(result.entities) <= 5
    assert result.truncated is True


@pytest.mark.asyncio
async def test_subgraph_neo4j_unavailable_returns_503(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    graph_client = FakeGraphClient(raise_error=RuntimeError("boom"))
    service = service_for(tmp_path, store, graph_client=graph_client)

    with pytest.raises(DependencyUnavailableError):
        await service.subgraph(dataset_slug="main", entity_id=root.id, depth=2)


@pytest.mark.asyncio
async def test_subgraph_without_graph_client_returns_503(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    root = make_entity(dataset.id)
    store.entities.append(root)
    service = service_for(tmp_path, store, graph_client=None)

    with pytest.raises(DependencyUnavailableError):
        await service.subgraph(dataset_slug="main", entity_id=root.id, depth=2)


# --- PATH ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_same_entity_returns_trivial_path_without_neo4j(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    entity = make_entity(dataset.id)
    store.entities.append(entity)
    graph_client = FakeGraphClient()
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.path(
        dataset_slug="main", from_entity_id=entity.id, to_entity_id=entity.id, max_depth=4
    )

    assert result.found is True
    assert [e.entity_id for e in result.entities] == [entity.id]
    assert result.relations == []
    assert graph_client.path_calls == []


@pytest.mark.asyncio
async def test_path_valid_from_to_returns_hydrated_path(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    relation = make_relation(dataset.id, source_entity_id=a.id, target_entity_id=b.id)
    store.relations.append(relation)
    graph_client = FakeGraphClient(
        path=GraphPathRecord(
            entity_ids=[a.id, b.id],
            edges=[
                GraphRelationEdge(
                    relation_id=relation.id, source_entity_id=a.id, target_entity_id=b.id
                )
            ],
        )
    )
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.path(
        dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=4
    )

    assert result.found is True
    assert [e.entity_id for e in result.entities] == [a.id, b.id]
    assert [r.relation_id for r in result.relations] == [relation.id]


@pytest.mark.asyncio
async def test_path_entity_missing_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    store.entities.append(a)
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.path(
            dataset_slug="main", from_entity_id=a.id, to_entity_id=uuid4(), max_depth=4
        )

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_path_not_found_returns_safe_empty_result(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    graph_client = FakeGraphClient(path=None)
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.path(
        dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=4
    )

    assert result.found is False
    assert result.entities == []
    assert result.relations == []


@pytest.mark.asyncio
async def test_path_max_depth_invalid_returns_400(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.path(dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=0)

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_path_max_depth_above_hard_limit_returns_400(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    service = service_for(tmp_path, store, graph_client=FakeGraphClient())

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.path(dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=5)

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_path_stale_relation_is_filtered_and_returns_safe_empty(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    # Neo4j still has the relation projected, but PostgreSQL marks it inactive.
    stale_relation = make_relation(
        dataset.id, source_entity_id=a.id, target_entity_id=b.id, is_active=False
    )
    store.relations.append(stale_relation)
    graph_client = FakeGraphClient(
        path=GraphPathRecord(
            entity_ids=[a.id, b.id],
            edges=[
                GraphRelationEdge(
                    relation_id=stale_relation.id, source_entity_id=a.id, target_entity_id=b.id
                )
            ],
        )
    )
    service = service_for(tmp_path, store, graph_client=graph_client)

    result = await service.path(
        dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=4
    )

    assert result.found is False
    assert result.entities == []
    assert result.relations == []


@pytest.mark.asyncio
async def test_path_neo4j_unavailable_returns_503(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset("main")
    store.datasets.append(dataset)
    a = make_entity(dataset.id)
    b = make_entity(dataset.id)
    store.entities.extend([a, b])
    graph_client = FakeGraphClient(raise_error=RuntimeError("boom"))
    service = service_for(tmp_path, store, graph_client=graph_client)

    with pytest.raises(DependencyUnavailableError):
        await service.path(dataset_slug="main", from_entity_id=a.id, to_entity_id=b.id, max_depth=4)
