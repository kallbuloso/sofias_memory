from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus
from sofias_memory.infrastructure.postgres.models import (
    Dataset,
    Entity,
    Feedback,
    PipelineRun,
    Query,
)
from sofias_memory.infrastructure.postgres.models.relation import Relation
from sofias_memory.infrastructure.postgres.repositories.entities import (
    EntityDuplicateCandidate,
    EntityEmbeddingCandidate,
)
from sofias_memory.infrastructure.postgres.repositories.feedback import UnappliedFeedback
from sofias_memory.infrastructure.postgres.repositories.relations import RelationEmbeddingCandidate
from sofias_memory.ports import ProjectionCommand
from sofias_memory.schemas.improve import ImproveRequest, ImproveResult
from sofias_memory.services.improve import (
    ImproveService,
    ImproveUnitOfWork,
    UnitOfWorkFactory,
    entity_embedding_text,
    normalize_feedback_score,
    relation_embedding_text,
    stream_update_weight,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.queries: list[Query] = []
        self.feedback: list[Feedback] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self.entities_by_chunk: dict[UUID, list[Entity]] = {}
        self.relations_by_chunk: dict[UUID, list[Relation]] = {}
        self.pipeline_runs: list[PipelineRun] = []
        self.run_current_steps_on_add: list[str | None] = []
        self.graph_commands: list[ProjectionCommand] = []
        self.committed_operations: list[list[str]] = []


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_slug(self, slug: str) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)


class FakeFeedbackRepository:
    def __init__(self, store: FakeStore, operations: list[str]) -> None:
        self._store = store
        self._operations = operations

    async def list_unapplied_for_dataset(self, dataset_id: UUID) -> list[UnappliedFeedback]:
        query_by_id = {query.id: query for query in self._store.queries}
        result: list[UnappliedFeedback] = []
        for feedback in self._store.feedback:
            query = query_by_id.get(feedback.query_id)
            if (
                query is None
                or feedback.applied_at is not None
                or dataset_id not in query.dataset_ids
            ):
                continue
            result.append(
                UnappliedFeedback(
                    id=feedback.id,
                    query_id=feedback.query_id,
                    target_type=feedback.target_type,
                    target_id=feedback.target_id,
                    score=feedback.score,
                    references=dict(query.references or {}),
                )
            )
        return result

    async def mark_applied(
        self,
        feedback_id: UUID,
        *,
        applied_at: datetime,
    ) -> Feedback | None:
        feedback = next(
            (item for item in self._store.feedback if item.id == feedback_id),
            None,
        )
        if feedback is None:
            return None
        feedback.applied_at = applied_at
        self._operations.append(f"feedback:{feedback_id}:applied")
        return feedback


class FakeEntityMentionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_entities_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Entity]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        entities: dict[UUID, Entity] = {}
        for chunk_id in chunk_ids:
            for entity in self._store.entities_by_chunk.get(chunk_id, []):
                if (
                    entity.dataset_id == dataset_id
                    and entity.generation == dataset.active_generation
                    and entity.is_active
                ):
                    entities[entity.id] = entity
        return [entities[entity_id] for entity_id in sorted(entities)]


class FakeEntityRepository:
    def __init__(self, store: FakeStore, operations: list[str]) -> None:
        self._store = store
        self._operations = operations

    async def list_missing_embedding_candidates(
        self,
        *,
        dataset_id: UUID,
    ) -> list[EntityEmbeddingCandidate]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        return [
            EntityEmbeddingCandidate(entity_id=item.id, name=item.name)
            for item in sorted(self._store.entities, key=lambda entity: entity.id)
            if item.dataset_id == dataset_id
            and item.generation == dataset.active_generation
            and item.is_active
            and item.embedding is None
        ]

    async def set_missing_embeddings_for_active_current(
        self,
        *,
        dataset_id: UUID,
        embeddings_by_entity_id: dict[UUID, list[float]],
    ) -> int:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        updated = 0
        for item in sorted(self._store.entities, key=lambda entity: entity.id):
            if (
                item.id in embeddings_by_entity_id
                and item.dataset_id == dataset_id
                and item.generation == dataset.active_generation
                and item.is_active
                and item.embedding is None
            ):
                item.embedding = embeddings_by_entity_id[item.id]
                updated += 1
                self._operations.append(f"entity:{item.id}:embedding")
        return updated

    async def list_duplicate_candidates(
        self,
        *,
        dataset_id: UUID,
        similarity_threshold: float,
    ) -> list[EntityDuplicateCandidate]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        entities = [
            item
            for item in sorted(self._store.entities, key=lambda entity: entity.id)
            if item.dataset_id == dataset_id
            and item.generation == dataset.active_generation
            and item.is_active
            and item.embedding is not None
        ]
        candidates: list[EntityDuplicateCandidate] = []
        for index, item in enumerate(entities):
            for candidate in entities[index + 1 :]:
                if item.entity_type.strip().casefold() != candidate.entity_type.strip().casefold():
                    continue
                similarity = cosine_similarity(
                    cast(list[float], item.embedding),
                    cast(list[float], candidate.embedding),
                )
                if similarity < similarity_threshold:
                    continue
                candidates.append(
                    EntityDuplicateCandidate(
                        entity_id=item.id,
                        entity_name=item.name,
                        candidate_id=candidate.id,
                        candidate_name=candidate.name,
                        entity_type=item.entity_type,
                        similarity=similarity,
                    )
                )
        return sorted(
            candidates,
            key=lambda candidate: (
                -candidate.similarity,
                candidate.entity_id,
                candidate.candidate_id,
            ),
        )


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_relations_for_chunks(
        self,
        *,
        dataset_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[Relation]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        relations: dict[UUID, Relation] = {}
        for chunk_id in chunk_ids:
            for relation in self._store.relations_by_chunk.get(chunk_id, []):
                if (
                    relation.dataset_id == dataset_id
                    and relation.generation == dataset.active_generation
                    and relation.is_active
                ):
                    relations[relation.id] = relation
        return [relations[relation_id] for relation_id in sorted(relations)]


class FakeRelationRepository:
    def __init__(self, store: FakeStore, operations: list[str]) -> None:
        self._store = store
        self._operations = operations

    async def list_missing_embedding_candidates(
        self,
        *,
        dataset_id: UUID,
    ) -> list[RelationEmbeddingCandidate]:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        entities_by_id = {entity.id: entity for entity in self._store.entities}
        candidates: list[RelationEmbeddingCandidate] = []
        for item in sorted(self._store.relations, key=lambda relation: relation.id):
            source_entity = entities_by_id.get(item.source_entity_id)
            target_entity = entities_by_id.get(item.target_entity_id)
            if (
                source_entity is None
                or target_entity is None
                or item.dataset_id != dataset_id
                or item.generation != dataset.active_generation
                or not item.is_active
                or item.embedding is not None
                or source_entity.dataset_id != dataset_id
                or source_entity.generation != dataset.active_generation
                or not source_entity.is_active
                or target_entity.dataset_id != dataset_id
                or target_entity.generation != dataset.active_generation
                or not target_entity.is_active
            ):
                continue
            candidates.append(
                RelationEmbeddingCandidate(
                    relation_id=item.id,
                    source_name=source_entity.name,
                    target_name=target_entity.name,
                    predicate=item.predicate,
                    description=item.description,
                )
            )
        return candidates

    async def set_missing_embeddings_for_active_current(
        self,
        *,
        dataset_id: UUID,
        embeddings_by_relation_id: dict[UUID, list[float]],
    ) -> int:
        dataset = next(dataset for dataset in self._store.datasets if dataset.id == dataset_id)
        updated = 0
        for item in sorted(self._store.relations, key=lambda relation: relation.id):
            if (
                item.id in embeddings_by_relation_id
                and item.dataset_id == dataset_id
                and item.generation == dataset.active_generation
                and item.is_active
                and item.embedding is None
            ):
                item.embedding = embeddings_by_relation_id[item.id]
                updated += 1
                self._operations.append(f"relation:{item.id}:embedding")
        return updated


class FakeGraphOutboxRepository:
    def __init__(self, store: FakeStore, operations: list[str]) -> None:
        self._store = store
        self._operations = operations

    async def add_projection_command(self, command: ProjectionCommand) -> object:
        self._store.graph_commands.append(command)
        self._operations.append(f"outbox:{command.aggregate_type}:{command.aggregate_id}")
        return object()


class FakePipelineRunRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, run: PipelineRun) -> PipelineRun:
        self._store.pipeline_runs.append(run)
        self._store.run_current_steps_on_add.append(run.current_step)
        return run

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return next((run for run in self._store.pipeline_runs if run.id == run_id), None)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._operations: list[str] = []
        self.datasets = FakeDatasetRepository(store)
        self.feedback = FakeFeedbackRepository(store, self._operations)
        self.entities = FakeEntityRepository(store, self._operations)
        self.entity_mentions = FakeEntityMentionRepository(store)
        self.relations = FakeRelationRepository(store, self._operations)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.graph_outbox = FakeGraphOutboxRepository(store, self._operations)
        self.pipeline_runs = FakePipelineRunRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.committed_operations.append(list(self._operations))


class FakeDrain:
    def __init__(self, processed: int = 0) -> None:
        self.processed = processed
        self.dataset_ids: list[UUID] = []

    async def process_dataset(self, dataset_id: UUID) -> object:
        self.dataset_ids.append(dataset_id)
        return type("DrainResult", (), {"processed": self.processed})()


class FakeEmbeddingClient:
    def __init__(self, dimensions: int = 3072) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.responses: list[list[float]] | None = None
        self.failure: Exception | None = None

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.failure is not None:
            raise self.failure
        if self.responses is not None:
            return self.responses
        return [[0.25] * self.dimensions for _ in texts]


def service_for(
    tmp_path: Path,
    store: FakeStore,
    *,
    drain: FakeDrain | None = None,
) -> tuple[ImproveService, FakeEmbeddingClient, FakeDrain]:
    embedding_client = FakeEmbeddingClient()
    resolved_drain = drain or FakeDrain()

    def create_uow() -> ImproveUnitOfWork:
        return cast(ImproveUnitOfWork, FakeUnitOfWork(store))

    return (
        ImproveService(
            make_settings(tmp_path),
            embedding_client=embedding_client,
            graph_projection_drain=resolved_drain,
            unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
        ),
        embedding_client,
        resolved_drain,
    )


def seed_dataset(store: FakeStore, *, slug: str = "main", generation: int = 0) -> Dataset:
    dataset = Dataset(
        id=uuid4(),
        name=slug,
        slug=slug,
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=generation,
    )
    store.datasets.append(dataset)
    return dataset


def query_for(dataset_id: UUID, chunk_ids: Sequence[UUID]) -> Query:
    return Query(
        id=uuid4(),
        query_text="What is relevant?",
        dataset_ids=[dataset_id],
        mode="rag",
        answer="Answer.",
        references={
            "items": [
                {
                    "source_id": str(uuid4()),
                    "document_id": str(uuid4()),
                    "chunk_id": str(chunk_id),
                    "chunk_ordinal": ordinal,
                    "score": 0.03,
                }
                for ordinal, chunk_id in enumerate(chunk_ids)
            ]
        },
        timings={"embedding": 1, "retrieval": 1, "graph": 0, "generation": 1, "total": 3},
        model="gpt-test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def feedback_for(
    query_id: UUID,
    *,
    target_type: str,
    score: int,
    target_id: UUID | None = None,
) -> Feedback:
    return Feedback(
        id=uuid4(),
        query_id=query_id,
        target_type=target_type,
        target_id=target_id,
        score=score,
        comment=None,
        applied_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def entity(dataset_id: UUID, *, generation: int, weight: float = 0.5) -> Entity:
    return Entity(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        canonical_key=f"concept:{uuid4()}",
        name="PostgreSQL",
        entity_type="Database",
        description="Source of truth.",
        aliases=[],
        properties={},
        confidence=1.0,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )


def relation(
    dataset_id: UUID,
    source_entity_id: UUID,
    target_entity_id: UUID,
    *,
    generation: int,
    weight: float = 0.5,
) -> Relation:
    return Relation(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=generation,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate="uses",
        description="Uses projection.",
        properties={},
        confidence=0.8,
        importance_weight=weight,
        embedding=None,
        is_active=True,
    )


def cosine_similarity(first: list[float], second: list[float]) -> float:
    dot_product = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = sqrt(sum(value * value for value in first))
    second_norm = sqrt(sum(value * value for value in second))
    return dot_product / (first_norm * second_norm)


def test_feedback_weight_normalization_and_streaming_formula() -> None:
    assert normalize_feedback_score(-1) == 0.0
    assert normalize_feedback_score(0) == 0.5
    assert normalize_feedback_score(1) == 1.0
    assert stream_update_weight(1.0, 0.0) == 0.9
    assert stream_update_weight(0.0, 1.0) == 0.1
    assert stream_update_weight(0.333333, 0.5) == 0.35


def test_relation_embedding_text_is_deterministic_and_omits_metadata() -> None:
    candidate = RelationEmbeddingCandidate(
        relation_id=uuid4(),
        source_name=" PostgreSQL ",
        target_name=" Sofias Memory ",
        predicate="stores",
        description=" Stores authoritative memory. ",
    )
    predicate_only = RelationEmbeddingCandidate(
        relation_id=uuid4(),
        source_name="Chunk",
        target_name="Document",
        predicate="belongs_to",
        description=" ",
    )

    assert (
        relation_embedding_text(candidate)
        == "PostgreSQL-›stores: Stores authoritative memory.-›Sofias Memory"
    )
    assert relation_embedding_text(predicate_only) == "Chunk-›belongs_to-›Document"


def test_entity_embedding_text_is_stripped_name_only() -> None:
    candidate = EntityEmbeddingCandidate(entity_id=uuid4(), name=" PostgreSQL ")

    assert entity_embedding_text(candidate) == "PostgreSQL"


@pytest.mark.asyncio
async def test_answer_and_reference_feedback_resolve_chunk_targets_and_enqueue_updated_weights(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    answer_chunk = uuid4()
    reference_chunk = uuid4()
    query = query_for(dataset.id, [answer_chunk, reference_chunk])
    store.queries.append(query)
    store.feedback.extend(
        [
            feedback_for(query.id, target_type="answer", score=1),
            feedback_for(query.id, target_type="reference", target_id=reference_chunk, score=-1),
        ]
    )
    first_entity = entity(dataset.id, generation=dataset.active_generation, weight=0.5)
    second_entity = entity(dataset.id, generation=dataset.active_generation, weight=0.5)
    first_relation = relation(
        dataset.id,
        first_entity.id,
        second_entity.id,
        generation=dataset.active_generation,
        weight=0.5,
    )
    store.entities_by_chunk = {
        answer_chunk: [first_entity],
        reference_chunk: [second_entity],
    }
    store.relations_by_chunk = {reference_chunk: [first_relation]}
    drain = FakeDrain(processed=3)

    service, embedding_client, _ = service_for(tmp_path, store, drain=drain)
    result = await service.improve(ImproveRequest())

    assert embedding_client.calls == []
    assert result.feedback_processed == 2
    assert result.feedback_applied == 2
    assert result.feedback_skipped == 0
    assert result.entities_updated == 2
    assert result.relations_updated == 1
    assert result.graph_events_enqueued == 3
    assert result.graph_events_processed == 3
    assert first_entity.importance_weight == 0.55
    assert second_entity.importance_weight == 0.495
    assert first_relation.importance_weight == 0.495
    assert all(feedback.applied_at is not None for feedback in store.feedback)
    assert [command.aggregate_type for command in store.graph_commands] == [
        "entity",
        "entity",
        "relation",
    ]
    relation_command = next(
        command for command in store.graph_commands if command.aggregate_type == "relation"
    )
    assert relation_command.properties["importance_weight"] == 0.495
    assert drain.dataset_ids == [dataset.id]
    apply_commit = next(
        operations
        for operations in store.committed_operations
        if any("outbox:" in item for item in operations)
    )
    assert any(":applied" in item for item in apply_commit)
    assert any(item.startswith("outbox:entity") for item in apply_commit)


@pytest.mark.asyncio
async def test_relation_embeddings_select_active_current_null_candidates_and_persist_in_order(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store, generation=3)
    source_entity = entity(dataset.id, generation=3)
    source_entity.name = "PostgreSQL"
    target_entity = entity(dataset.id, generation=3)
    target_entity.name = "Sofias Memory"
    stale_entity = entity(dataset.id, generation=2)
    inactive_entity = entity(dataset.id, generation=3)
    inactive_entity.is_active = False
    store.entities.extend([source_entity, target_entity, stale_entity, inactive_entity])
    selected_high_id = relation(dataset.id, source_entity.id, target_entity.id, generation=3)
    selected_low_id = relation(dataset.id, source_entity.id, target_entity.id, generation=3)
    selected_high_id.id = UUID("30000000-0000-0000-0000-000000000003")
    selected_low_id.id = UUID("10000000-0000-0000-0000-000000000001")
    selected_low_id.predicate = "stores"
    selected_low_id.description = "Stores authoritative memory."
    stale_relation = relation(dataset.id, source_entity.id, target_entity.id, generation=2)
    embedded_relation = relation(dataset.id, source_entity.id, target_entity.id, generation=3)
    embedded_relation.embedding = [0.9] * 3072
    inactive_endpoint_relation = relation(
        dataset.id,
        inactive_entity.id,
        target_entity.id,
        generation=3,
    )
    store.relations.extend(
        [
            selected_high_id,
            selected_low_id,
            stale_relation,
            embedded_relation,
            inactive_endpoint_relation,
        ]
    )
    service, embedding_client, drain = service_for(tmp_path, store)
    embedding_client.responses = [[0.1] * 3072, [0.2] * 3072]

    result = await service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert result.stages == ["relation_embeddings"]
    assert result.feedback_processed == 0
    assert result.feedback_applied == 0
    assert result.feedback_skipped == 0
    assert result.entities_updated == 0
    assert result.relations_updated == 0
    assert result.relations_embedded == 2
    assert result.graph_events_enqueued == 0
    assert result.graph_events_processed == 0
    assert drain.dataset_ids == []
    assert embedding_client.calls == [
        [
            "PostgreSQL-›stores: Stores authoritative memory.-›Sofias Memory",
            "PostgreSQL-›uses: Uses projection.-›Sofias Memory",
        ]
    ]
    assert selected_low_id.embedding == [0.1] * 3072
    assert selected_high_id.embedding == [0.2] * 3072
    assert stale_relation.embedding is None
    assert embedded_relation.embedding == [0.9] * 3072
    assert inactive_endpoint_relation.embedding is None

    rerun = await service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert rerun.relations_embedded == 0
    assert len(embedding_client.calls) == 1


@pytest.mark.asyncio
async def test_entity_deduplication_embeds_active_current_null_entities_and_detects_pairs(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store, generation=2)
    first = entity(dataset.id, generation=2)
    first.id = UUID("10000000-0000-0000-0000-000000000001")
    first.name = " PostgreSQL "
    first.entity_type = "Database"
    second = entity(dataset.id, generation=2)
    second.id = UUID("20000000-0000-0000-0000-000000000002")
    second.name = "Postgres"
    second.entity_type = " database "
    different_type = entity(dataset.id, generation=2)
    different_type.id = UUID("30000000-0000-0000-0000-000000000003")
    different_type.entity_type = "System"
    stale = entity(dataset.id, generation=1)
    inactive = entity(dataset.id, generation=2)
    inactive.is_active = False
    existing = entity(dataset.id, generation=2)
    existing.embedding = [0.7] * 3072
    store.entities.extend([second, existing, inactive, first, stale, different_type])
    service, embedding_client, drain = service_for(tmp_path, store)
    embedding_client.responses = [
        [1.0] + [0.0] * 3071,
        [0.96] + [0.04] * 3071,
        [0.0, 1.0] + [0.0] * 3070,
    ]

    result = await service.improve(ImproveRequest(stages=["entity_deduplication"]))

    assert result.feedback_processed == 0
    assert result.relations_embedded == 0
    assert result.entities_embedded == 3
    assert result.entity_duplicate_candidates == 1
    assert result.graph_events_enqueued == 0
    assert result.graph_events_processed == 0
    assert drain.dataset_ids == []
    assert embedding_client.calls == [["PostgreSQL", "Postgres", different_type.name]]
    assert first.embedding == [1.0] + [0.0] * 3071
    assert second.embedding == [0.96] + [0.04] * 3071
    assert different_type.embedding == [0.0, 1.0] + [0.0] * 3070
    assert stale.embedding is None
    assert inactive.embedding is None
    assert existing.embedding == [0.7] * 3072
    assert store.graph_commands == []

    rerun = await service.improve(ImproveRequest(stages=["entity_deduplication"]))

    assert rerun.entities_embedded == 0
    assert rerun.entity_duplicate_candidates == 1
    assert len(embedding_client.calls) == 1


@pytest.mark.asyncio
async def test_entity_embedding_provider_validation_and_failure_write_nothing(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    target = entity(dataset.id, generation=0)
    store.entities.append(target)
    service, embedding_client, _ = service_for(tmp_path, store)
    embedding_client.responses = []

    with pytest.raises(Exception, match="invalid response"):
        await service.improve(ImproveRequest(stages=["entity_deduplication"]))

    assert target.embedding is None

    embedding_client.responses = [[0.1] * 2]
    with pytest.raises(Exception, match="invalid response"):
        await service.improve(ImproveRequest(stages=["entity_deduplication"]))

    assert target.embedding is None

    embedding_client.responses = None
    embedding_client.failure = RuntimeError("secret provider failure")
    with pytest.raises(Exception, match="unavailable"):
        await service.improve(ImproveRequest(stages=["entity_deduplication"]))

    assert target.embedding is None


@pytest.mark.asyncio
async def test_relation_embedding_provider_validation_and_failure_write_nothing(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    source_entity = entity(dataset.id, generation=0)
    target_entity = entity(dataset.id, generation=0)
    store.entities.extend([source_entity, target_entity])
    target_relation = relation(dataset.id, source_entity.id, target_entity.id, generation=0)
    store.relations.append(target_relation)
    service, embedding_client, _ = service_for(tmp_path, store)
    embedding_client.responses = []

    with pytest.raises(Exception, match="invalid response"):
        await service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert target_relation.embedding is None

    embedding_client.responses = [[0.1] * 2]
    with pytest.raises(Exception, match="invalid response"):
        await service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert target_relation.embedding is None

    embedding_client.responses = None
    embedding_client.failure = RuntimeError("secret provider failure")
    with pytest.raises(Exception, match="unavailable"):
        await service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert target_relation.embedding is None


@pytest.mark.asyncio
async def test_omitted_stages_default_to_feedback_weights_then_relation_embeddings(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    chunk_id = uuid4()
    query = query_for(dataset.id, [chunk_id])
    store.queries.append(query)
    store.feedback.append(feedback_for(query.id, target_type="answer", score=1))
    source_entity = entity(dataset.id, generation=0, weight=0.5)
    source_entity.id = UUID("10000000-0000-0000-0000-000000000001")
    source_entity.name = "A"
    target_entity = entity(dataset.id, generation=0, weight=0.5)
    target_entity.id = UUID("20000000-0000-0000-0000-000000000002")
    target_entity.name = "B"
    store.entities.extend([source_entity, target_entity])
    store.entities_by_chunk[chunk_id] = [source_entity]
    target_relation = relation(dataset.id, source_entity.id, target_entity.id, generation=0)
    store.relations.append(target_relation)
    drain = FakeDrain(processed=1)
    service, embedding_client, _ = service_for(tmp_path, store, drain=drain)

    result = await service.improve(ImproveRequest())

    assert result.stages == ["feedback_weights", "relation_embeddings", "entity_deduplication"]
    assert result.feedback_processed == 1
    assert result.entities_updated == 1
    assert result.relations_embedded == 1
    assert result.entities_embedded == 2
    assert result.entity_duplicate_candidates == 1
    assert drain.dataset_ids == [dataset.id]
    assert embedding_client.calls == [["A-›uses: Uses projection.-›B"], ["A", "B"]]
    assert store.run_current_steps_on_add == ["feedback_weights"]

    relation_only_store = FakeStore()
    seed_dataset(relation_only_store)
    first_run_service, _, _ = service_for(tmp_path, relation_only_store)
    await first_run_service.improve(ImproveRequest(stages=["relation_embeddings"]))

    assert relation_only_store.run_current_steps_on_add == ["relation_embeddings"]


@pytest.mark.asyncio
async def test_improve_is_dataset_isolated_and_uses_only_current_active_knowledge(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store, generation=2)
    other_dataset = seed_dataset(store, slug="other", generation=2)
    target_chunk = uuid4()
    query = query_for(dataset.id, [target_chunk])
    other_query = query_for(other_dataset.id, [target_chunk])
    store.queries.extend([query, other_query])
    store.feedback.extend(
        [
            feedback_for(query.id, target_type="answer", score=1),
            feedback_for(other_query.id, target_type="answer", score=1),
        ]
    )
    active_entity = entity(dataset.id, generation=2, weight=0.2)
    stale_entity = entity(dataset.id, generation=1, weight=0.2)
    other_entity = entity(other_dataset.id, generation=2, weight=0.2)
    inactive_relation = relation(
        dataset.id,
        active_entity.id,
        stale_entity.id,
        generation=2,
        weight=0.2,
    )
    inactive_relation.is_active = False
    store.entities_by_chunk[target_chunk] = [active_entity, stale_entity, other_entity]
    store.relations_by_chunk[target_chunk] = [inactive_relation]

    service, _, _ = service_for(tmp_path, store)
    result = await service.improve(ImproveRequest(dataset="main", stages=["feedback_weights"]))

    assert result.feedback_processed == 1
    assert result.entities_updated == 1
    assert result.relations_updated == 0
    assert active_entity.importance_weight == 0.28
    assert stale_entity.importance_weight == 0.2
    assert other_entity.importance_weight == 0.2
    assert store.feedback[0].applied_at is not None
    assert store.feedback[1].applied_at is None
    assert [command.aggregate_id for command in store.graph_commands] == [str(active_entity.id)]


@pytest.mark.asyncio
async def test_applied_feedback_is_not_reapplied_and_no_target_feedback_is_consumed(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    target_chunk = uuid4()
    query = query_for(dataset.id, [target_chunk])
    store.queries.append(query)
    already_applied = feedback_for(query.id, target_type="answer", score=1)
    already_applied.applied_at = datetime(2026, 1, 2, tzinfo=UTC)
    no_target = feedback_for(query.id, target_type="reference", target_id=target_chunk, score=-1)
    store.feedback.extend([already_applied, no_target])

    service, embedding_client, _ = service_for(tmp_path, store)
    result = await service.improve(ImproveRequest(stages=["feedback_weights"]))

    assert embedding_client.calls == []
    assert result.feedback_processed == 1
    assert result.feedback_applied == 0
    assert result.feedback_skipped == 1
    assert result.entities_updated == 0
    assert result.relations_updated == 0
    assert result.graph_events_enqueued == 0
    assert no_target.applied_at is not None
    assert store.graph_commands == []


@pytest.mark.asyncio
async def test_improve_route_returns_envelope_and_requires_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = uuid4()
    run_id = uuid4()

    class FakeProjection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeOutboxProcessor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeBatchProcessor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeImproveService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def improve(self, request: ImproveRequest) -> ImproveResult:
            return ImproveResult(
                run_id=run_id,
                status=PipelineRunStatus.SUCCEEDED.value,
                dataset_id=dataset_id,
                generation=0,
                stages=request.stages or ["feedback_weights"],
                feedback_processed=1,
                feedback_applied=1,
                feedback_skipped=0,
                entities_updated=1,
                relations_updated=0,
                relations_embedded=0,
                entities_embedded=0,
                entity_duplicate_candidates=0,
                graph_events_enqueued=1,
                graph_events_processed=1,
            )

    monkeypatch.setattr("sofias_memory.api.routes.improve.Neo4jProjection", FakeProjection)
    monkeypatch.setattr(
        "sofias_memory.api.routes.improve.GraphOutboxProcessor",
        FakeOutboxProcessor,
    )
    monkeypatch.setattr(
        "sofias_memory.api.routes.improve.GraphOutboxBatchProcessor",
        FakeBatchProcessor,
    )
    monkeypatch.setattr("sofias_memory.api.routes.improve.ImproveService", FakeImproveService)
    monkeypatch.setattr("sofias_memory.api.routes.improve.app_neo4j_resource", lambda app: object())
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key_response = await client.post("/api/v1/improve", json={})
        response = await client.post(
            "/api/v1/improve",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"dataset": "main", "stages": ["feedback_weights"], "wait": False},
        )

    assert missing_key_response.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["run_id"] == str(run_id)
    assert body["data"]["dataset_id"] == str(dataset_id)
    assert body["data"]["feedback_processed"] == 1
    assert body["data"]["entities_updated"] == 1
    assert body["meta"]["request_id"]
