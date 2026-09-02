from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Query, Summary
from sofias_memory.infrastructure.postgres.repositories.chunks import (
    ChunkRepository,
    RetrievedChunk,
)
from sofias_memory.infrastructure.postgres.repositories.entities import RecalledEntity
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RecalledRelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.relations import RecalledRelation
from sofias_memory.infrastructure.postgres.repositories.summaries import (
    RecalledDocumentSummary,
    RetrievedDocumentSummary,
    SummaryRepository,
)
from sofias_memory.schemas.recall import RecallFilters, RecallRequest, RecallResult
from sofias_memory.services.recall import (
    NO_EVIDENCE_ANSWER,
    GraphRecallRecord,
    RecallService,
    RecallUnitOfWork,
    UnitOfWorkFactory,
    reciprocal_rank_fusion,
)
from tests.unit._app_factory import create_app

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
        "recall_default_top_k": 2,
        "recall_max_top_k": 3,
        "recall_vector_top_k": 5,
        "recall_lexical_top_k": 5,
        "recall_rrf_k": 60,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class FakeEmbeddingClient:
    def __init__(self, dimensions: int = 3072) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * self.dimensions for _ in texts]


class FakeRagAnswerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def answer(self, query: str, context: str) -> str:
        self.calls.append((query, context))
        return "Grounded answer."


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.vector_results: list[RetrievedChunk] = []
        self.lexical_results: list[RetrievedChunk] = []
        self.summary_vector_results: list[RetrievedDocumentSummary] = []
        self.queries: list[Query] = []
        self.filters: list[object] = []
        self.vector_search_calls = 0
        self.lexical_search_calls = 0
        self.summary_vector_search_calls: list[dict[str, object]] = []
        self.entities: list[RecalledEntity] = []
        self.relations: list[RecalledRelation] = []
        self.evidence: list[RecalledRelationEvidence] = []
        self.summaries: list[RecalledDocumentSummary] = []
        self.commits = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_slug(self, slug: str) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)


class FakeRecallChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def vector_search(self, **kwargs: object) -> list[RetrievedChunk]:
        self._store.vector_search_calls += 1
        self._store.filters.append(kwargs["filters"])
        return self._store.vector_results

    async def lexical_search(self, **kwargs: object) -> list[RetrievedChunk]:
        self._store.lexical_search_calls += 1
        self._store.filters.append(kwargs["filters"])
        return self._store.lexical_results


class FakeQueryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, query: Query) -> Query:
        self._store.queries.append(query)
        return query


class FakeEntityRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_recall(self, **kwargs: object) -> list[RecalledEntity]:
        ids = set(cast(list[UUID], kwargs["entity_ids"]))
        return [entity for entity in self._store.entities if entity.id in ids]


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_recall(self, **kwargs: object) -> list[RecalledRelation]:
        ids = set(cast(list[UUID], kwargs["relation_ids"]))
        return [relation for relation in self._store.relations if relation.id in ids]


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_recall(self, **kwargs: object) -> list[RecalledRelationEvidence]:
        self._store.filters.append(kwargs["filters"])
        ids = set(cast(list[UUID], kwargs["relation_ids"]))
        filters = cast(RecallFilters, kwargs["filters"])
        return [
            evidence
            for evidence in self._store.evidence
            if evidence.relation_id in ids
            and (not filters.source_ids or evidence.source_id in filters.source_ids)
        ]


class FakeSummaryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_document_summaries_for_recall(
        self,
        **kwargs: object,
    ) -> list[RecalledDocumentSummary]:
        ids = set(cast(list[UUID], kwargs["document_ids"]))
        return [summary for summary in self._store.summaries if summary.document_id in ids]

    async def vector_search_document_summaries(
        self,
        **kwargs: object,
    ) -> list[RetrievedDocumentSummary]:
        self._store.summary_vector_search_calls.append(dict(kwargs))
        return self._store.summary_vector_results[: cast(int, kwargs["limit"])]


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.chunks = FakeRecallChunkRepository(store)
        self.queries = FakeQueryRepository(store)
        self.entities = FakeEntityRepository(store)
        self.relations = FakeRelationRepository(store)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.summaries = FakeSummaryRepository(store)
        self._store = store

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(
    tmp_path: Path,
    store: FakeStore,
    *,
    settings: Settings | None = None,
    embedding_client: FakeEmbeddingClient | None = None,
    rag_client: FakeRagAnswerClient | None = None,
    graph_client: object | None = None,
) -> tuple[RecallService, FakeEmbeddingClient, FakeRagAnswerClient]:
    embedding = embedding_client or FakeEmbeddingClient()
    rag = rag_client or FakeRagAnswerClient()

    def create_uow() -> RecallUnitOfWork:
        return cast(RecallUnitOfWork, FakeUnitOfWork(store))

    return (
        RecallService(
            settings or make_settings(tmp_path),
            embedding_client=embedding,
            rag_answer_client=rag,
            graph_recall_client=graph_client,  # type: ignore[arg-type]
            unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
        ),
        embedding,
        rag,
    )


def seed_dataset(store: FakeStore, slug: str = "main") -> Dataset:
    dataset = Dataset(
        id=uuid4(),
        name=slug,
        slug=slug,
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    store.datasets.append(dataset)
    return dataset


def retrieved_chunk(dataset_id: UUID, *, chunk_id: UUID | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id or uuid4(),
        dataset_id=dataset_id,
        source_id=uuid4(),
        source_name="source.txt",
        source_url=None,
        document_id=uuid4(),
        ordinal=0,
        text="Sofias Memory keeps grounded memory.",
        start_char=0,
        end_char=36,
    )


def retrieved_summary(
    dataset_id: UUID,
    *,
    summary_id: UUID | None = None,
    score: float = 0.91,
) -> RetrievedDocumentSummary:
    return RetrievedDocumentSummary(
        summary_id=summary_id or uuid4(),
        dataset_id=dataset_id,
        source_id=uuid4(),
        source_name="source.txt",
        source_url="https://example.com/source",
        document_id=uuid4(),
        text="Document summary about Sofias Memory.",
        score=score,
    )


class FakeGraphRecallClient:
    def __init__(self, records: list[GraphRecallRecord] | None = None) -> None:
        self.records = records or []
        self.calls: list[dict[str, object]] = []

    async def retrieve(self, **kwargs: object) -> list[GraphRecallRecord]:
        self.calls.append(dict(kwargs))
        return self.records


class FakeGraphRecord:
    def __init__(
        self,
        *,
        seed_chunk_id: UUID,
        seed_entity_id: UUID,
        neighbor_entity_id: UUID | None = None,
        relation_id: UUID | None = None,
    ) -> None:
        self.seed_chunk_id = seed_chunk_id
        self.seed_entity_id = seed_entity_id
        self.neighbor_entity_id = neighbor_entity_id
        self.relation_id = relation_id


def test_recall_request_validation_normalizes_datasets_and_dates() -> None:
    request = RecallRequest(datasets=["main", " main "], query="  What is memory?  ")

    assert request.datasets == ["main"]
    assert request.query == "What is memory?"
    with pytest.raises(ValidationError):
        RecallRequest(query=" ")
    with pytest.raises(ValidationError):
        RecallFilters(
            created_after=datetime(2026, 1, 2, tzinfo=UTC),
            created_before=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_reciprocal_rank_fusion_is_deterministic_and_rewards_both_channels() -> None:
    dataset_id = uuid4()
    first = retrieved_chunk(dataset_id)
    shared = retrieved_chunk(dataset_id)
    lexical_only = retrieved_chunk(dataset_id)

    hits = reciprocal_rank_fusion([first, shared], [shared, lexical_only], rrf_k=60)

    assert [hit.chunk_id for hit in hits] == [
        shared.chunk_id,
        first.chunk_id,
        lexical_only.chunk_id,
    ]
    assert hits[0].vector_rank == 2
    assert hits[0].lexical_rank == 1


def test_postgres_recall_scope_filters_to_active_generation_and_supported_filters() -> None:
    repository = ChunkRepository(cast(object, AsyncMock()))
    statement = repository._base_recall_statement(
        [uuid4()],
        RecallFilters(
            source_ids=[uuid4()],
            created_after=datetime(2026, 1, 1, tzinfo=UTC),
            created_before=datetime(2026, 1, 2, tzinfo=UTC),
            metadata={"origin": "smoke"},
        ),
    )
    rendered = str(statement)

    assert "chunks.generation = datasets.active_generation" in rendered
    assert "documents.generation = datasets.active_generation" in rendered
    assert "chunks.is_active IS true" in rendered
    assert "documents.is_active IS true" in rendered
    assert "sources.status" in rendered
    assert "sources.metadata @>" in rendered


def test_summary_recall_scope_filters_document_target_active_generation_and_order() -> None:
    repository = SummaryRepository(cast(object, AsyncMock()))
    statement = repository._base_document_summary_recall_statement(
        [uuid4()],
        RecallFilters(
            source_ids=[uuid4()],
            created_after=datetime(2026, 1, 1, tzinfo=UTC),
            created_before=datetime(2026, 1, 2, tzinfo=UTC),
            metadata={"origin": "smoke"},
        ),
    )
    distance = Summary.embedding.cosine_distance([0.1] * 3072).label("distance")
    rendered = str(statement.add_columns(distance).order_by(distance.asc(), Summary.id).limit(3))

    assert "summaries.target_type" in rendered
    assert "summaries.level" in rendered
    assert "summaries.is_active IS true" in rendered
    assert "documents.is_active IS true" in rendered
    assert "sources.status" in rendered
    assert "datasets.status" in rendered
    assert "summaries.generation = datasets.active_generation" in rendered
    assert "documents.generation = datasets.active_generation" in rendered
    assert "sources.metadata @>" in rendered
    assert "ORDER BY distance ASC, summaries.id" in rendered


@pytest.mark.asyncio
async def test_summaries_recall_uses_summary_vector_search_without_chunks_graph_or_llm(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    first = retrieved_summary(dataset.id, summary_id=UUID("10000000-0000-0000-0000-000000000001"))
    second = retrieved_summary(
        dataset.id,
        summary_id=UUID("10000000-0000-0000-0000-000000000002"),
        score=0.84,
    )
    store.summary_vector_results = [first, second]
    graph = FakeGraphRecallClient()
    service, embedding, rag = service_for(tmp_path, store, graph_client=graph)
    filters = RecallFilters(
        source_ids=[first.source_id],
        created_after=datetime(2026, 1, 1, tzinfo=UTC),
        created_before=datetime(2026, 1, 2, tzinfo=UTC),
        metadata={"origin": "unit"},
    )

    result = await service.recall(
        RecallRequest(
            query="summary memory",
            mode="summaries",
            top_k=1,
            include_references=True,
            filters=filters,
        )
    )

    assert embedding.calls == [["summary memory"]]
    assert store.vector_search_calls == 0
    assert store.lexical_search_calls == 0
    assert store.summary_vector_search_calls == [
        {
            "dataset_ids": [dataset.id],
            "query_embedding": [0.1] * 3072,
            "limit": 1,
            "filters": filters,
        }
    ]
    assert graph.calls == []
    assert rag.calls == []
    assert len(result.context) == 1
    summary_context = result.context[0]
    assert summary_context.summary_id == first.summary_id
    assert summary_context.source_id == first.source_id
    assert summary_context.source_name == first.source_name
    assert summary_context.document_id == first.document_id
    assert summary_context.url == first.source_url
    assert summary_context.text == first.text
    assert summary_context.score == first.score
    assert result.answer is None
    assert result.references == []
    assert result.entities == []
    assert result.relations == []
    assert result.timings_ms["graph"] == 0
    assert result.timings_ms["generation"] == 0
    assert store.queries[-1].mode == "summaries"
    assert store.queries[-1].answer is None
    assert store.queries[-1].references == {"items": []}
    assert store.queries[-1].model is None


@pytest.mark.asyncio
async def test_summaries_recall_zero_results_is_stable(tmp_path: Path) -> None:
    store = FakeStore()
    seed_dataset(store)
    service, embedding, rag = service_for(tmp_path, store)

    result = await service.recall(RecallRequest(query="missing", mode="summaries"))

    assert embedding.calls == [["missing"]]
    assert result.answer is None
    assert result.context == []
    assert result.references == []
    assert result.entities == []
    assert result.relations == []
    assert rag.calls == []
    assert store.vector_search_calls == 0
    assert store.lexical_search_calls == 0
    assert len(store.summary_vector_search_calls) == 1


@pytest.mark.asyncio
async def test_chunks_recall_fuses_context_references_filters_and_audits(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    vector_only = retrieved_chunk(dataset.id)
    shared = retrieved_chunk(dataset.id)
    store.vector_results = [vector_only, shared]
    store.lexical_results = [shared]
    service, embedding, rag = service_for(tmp_path, store)
    filters = RecallFilters(source_ids=[shared.source_id], metadata={"origin": "test"})

    result = await service.recall(
        RecallRequest(query="memory", mode="chunks", top_k=2, filters=filters)
    )

    assert [item.chunk_id for item in result.context] == [shared.chunk_id, vector_only.chunk_id]
    assert len(result.references) == 2
    assert embedding.calls == [["memory"]]
    assert rag.calls == []
    assert store.filters == [filters, filters]
    assert store.queries[0].query_text == "memory"
    assert store.queries[0].answer is None
    assert store.queries[0].references["items"][0]["chunk_id"] == str(shared.chunk_id)
    assert set(result.timings_ms) == {"embedding", "retrieval", "graph", "generation", "total"}


@pytest.mark.asyncio
async def test_rag_only_context_zero_evidence_and_query_privacy(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    hit = retrieved_chunk(dataset.id)
    store.vector_results = [hit]
    service, _, rag = service_for(tmp_path, store)

    context_only = await service.recall(
        RecallRequest(query="memory", mode="rag", only_context=True, include_references=False)
    )
    assert context_only.answer is None
    assert context_only.references == []
    assert rag.calls == []
    assert store.queries[-1].references["items"]

    store.vector_results = []
    zero_evidence = await service.recall(RecallRequest(query="nothing", mode="rag"))
    assert zero_evidence.answer == NO_EVIDENCE_ANSWER
    assert rag.calls == []

    store.vector_results = [hit]
    private_settings = make_settings(tmp_path, store_query_content=False)
    private_service, _, private_rag = service_for(tmp_path, store, settings=private_settings)
    rag_result = await private_service.recall(RecallRequest(query="memory", mode="rag"))
    assert rag_result.answer == "Grounded answer."
    assert private_rag.calls
    assert store.queries[-1].query_text is None
    assert store.queries[-1].answer is None
    assert store.queries[-1].references["items"]


@pytest.mark.asyncio
async def test_recall_rejects_top_k_above_configured_maximum(tmp_path: Path) -> None:
    store = FakeStore()
    seed_dataset(store)
    service, _, _ = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError, match="configured maximum"):
        await service.recall(RecallRequest(query="memory", top_k=4))


@pytest.mark.asyncio
async def test_triplets_uses_seed_chunks_graph_ids_and_postgres_hydration(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    high_seed = retrieved_chunk(dataset.id)
    low_seed = retrieved_chunk(dataset.id)
    entity_a = UUID("10000000-0000-0000-0000-000000000001")
    entity_b = UUID("10000000-0000-0000-0000-000000000002")
    relation_id = UUID("10000000-0000-0000-0000-000000000003")
    store.vector_results = [high_seed, low_seed]
    store.lexical_results = [high_seed]
    store.entities = [
        RecalledEntity(entity_b, "Neo4j", "Database", "Graph projection.", 0.8),
        RecalledEntity(entity_a, "PostgreSQL", "Database", "Source of truth.", 1.0),
    ]
    store.relations = [
        RecalledRelation(
            relation_id,
            entity_a,
            entity_b,
            "projects_to",
            "Sofias Memory projects graph records.",
            0.9,
            0.7,
        )
    ]
    store.evidence = [
        RecalledRelationEvidence(
            relation_id=relation_id,
            chunk_id=high_seed.chunk_id,
            quote="PostgreSQL is projected to Neo4j.",
            confidence=0.9,
            dataset_id=dataset.id,
            source_id=high_seed.source_id,
            source_name=high_seed.source_name,
            source_url=None,
            document_id=high_seed.document_id,
            chunk_ordinal=high_seed.ordinal,
            start_char=0,
            end_char=32,
        )
    ]
    graph = FakeGraphRecallClient(
        [
            FakeGraphRecord(
                seed_chunk_id=high_seed.chunk_id,
                seed_entity_id=entity_a,
                neighbor_entity_id=entity_b,
                relation_id=relation_id,
            )
        ]
    )
    service, embedding, rag = service_for(tmp_path, store, graph_client=graph)

    result = await service.recall(RecallRequest(query="graph", mode="triplets"))

    assert embedding.calls == [["graph"]]
    assert rag.calls == []
    assert graph.calls[0]["seed_chunk_ids"] == [high_seed.chunk_id, low_seed.chunk_id]
    assert [entity.entity_id for entity in result.entities] == [entity_a, entity_b]
    assert result.relations[0].relation_id == relation_id
    assert result.relations[0].evidence == "PostgreSQL is projected to Neo4j."
    assert result.references[0].chunk_id == high_seed.chunk_id
    assert store.queries[-1].mode == "triplets"


@pytest.mark.asyncio
async def test_graph_and_hybrid_generation_and_only_context(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    seed = retrieved_chunk(dataset.id)
    entity_a = uuid4()
    entity_b = uuid4()
    relation_id = uuid4()
    store.vector_results = [seed]
    store.entities = [
        RecalledEntity(entity_a, "Sofias Memory", "System", "Persistent memory.", 1.0),
        RecalledEntity(entity_b, "Neo4j", "Database", "Graph database.", 0.7),
    ]
    store.relations = [
        RecalledRelation(relation_id, entity_a, entity_b, "uses", "Uses Neo4j.", 0.8, 0.8)
    ]
    store.evidence = [
        RecalledRelationEvidence(
            relation_id=relation_id,
            chunk_id=uuid4(),
            quote="Sofias Memory uses Neo4j as projection.",
            confidence=0.9,
            dataset_id=dataset.id,
            source_id=seed.source_id,
            source_name=seed.source_name,
            source_url=None,
            document_id=seed.document_id,
            chunk_ordinal=seed.ordinal,
            start_char=0,
            end_char=42,
        )
    ]
    store.summaries = [
        RecalledDocumentSummary(document_id=seed.document_id, text="Document summary.")
    ]
    graph = FakeGraphRecallClient(
        [
            FakeGraphRecord(
                seed_chunk_id=seed.chunk_id,
                seed_entity_id=entity_a,
                neighbor_entity_id=entity_b,
                relation_id=relation_id,
            )
        ]
    )
    service, _, rag = service_for(tmp_path, store, graph_client=graph)

    graph_result = await service.recall(RecallRequest(query="graph?", mode="graph"))
    hybrid_result = await service.recall(RecallRequest(query="hybrid?", mode="hybrid"))
    context_only = await service.recall(
        RecallRequest(query="context?", mode="graph", only_context=True)
    )

    assert graph_result.answer == "Grounded answer."
    assert hybrid_result.answer == "Grounded answer."
    assert context_only.answer is None
    assert len(rag.calls) == 2
    assert "[RELATIONS/TRIPLETS]" in rag.calls[0][1]
    assert "[DOCUMENT SUMMARIES]" in rag.calls[1][1]
    assert "[GRAPH]" in rag.calls[1][1]


@pytest.mark.asyncio
async def test_empty_graph_does_not_call_llm_or_hallucinate(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    store.vector_results = [retrieved_chunk(dataset.id)]
    service, _, rag = service_for(tmp_path, store, graph_client=FakeGraphRecallClient())

    result = await service.recall(RecallRequest(query="graph?", mode="graph"))

    assert result.answer == NO_EVIDENCE_ANSWER
    assert result.entities == []
    assert result.relations == []
    assert rag.calls == []


@pytest.mark.asyncio
async def test_graph_evidence_respects_source_filters(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    seed = retrieved_chunk(dataset.id)
    entity_a = uuid4()
    entity_b = uuid4()
    relation_id = uuid4()
    store.vector_results = [seed]
    store.entities = [
        RecalledEntity(entity_a, "A", "Concept", "A.", 0.5),
        RecalledEntity(entity_b, "B", "Concept", "B.", 0.5),
    ]
    store.relations = [
        RecalledRelation(relation_id, entity_a, entity_b, "related", "A to B.", 1.0, 0.5)
    ]
    store.evidence = [
        RecalledRelationEvidence(
            relation_id=relation_id,
            chunk_id=uuid4(),
            quote="Filtered-out quote.",
            confidence=1.0,
            dataset_id=dataset.id,
            source_id=uuid4(),
            source_name="other.txt",
            source_url=None,
            document_id=seed.document_id,
            chunk_ordinal=0,
            start_char=0,
            end_char=19,
        )
    ]
    graph = FakeGraphRecallClient(
        [
            FakeGraphRecord(
                seed_chunk_id=seed.chunk_id,
                seed_entity_id=entity_a,
                neighbor_entity_id=entity_b,
                relation_id=relation_id,
            )
        ]
    )
    service, _, _ = service_for(tmp_path, store, graph_client=graph)

    result = await service.recall(
        RecallRequest(
            query="graph?",
            mode="triplets",
            filters=RecallFilters(source_ids=[seed.source_id]),
        )
    )

    assert result.relations[0].evidence is None
    assert {reference.chunk_id for reference in result.references} == {seed.chunk_id}


@pytest.mark.asyncio
async def test_neo4j_failure_is_safe_dependency_error(tmp_path: Path) -> None:
    class FailingGraphRecallClient:
        async def retrieve(self, **kwargs: object) -> list[GraphRecallRecord]:
            raise RuntimeError("bolt://user:secret@example")

    store = FakeStore()
    dataset = seed_dataset(store)
    store.vector_results = [retrieved_chunk(dataset.id)]
    service, _, _ = service_for(tmp_path, store, graph_client=FailingGraphRecallClient())

    with pytest.raises(SofiasMemoryError) as exc_info:
        await service.recall(RecallRequest(query="graph?", mode="triplets"))

    assert exc_info.value.message == "Neo4j graph recall is unavailable."
    assert "secret" not in exc_info.value.message


@pytest.mark.asyncio
async def test_recall_route_returns_the_standard_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_id = uuid4()

    class FakeRecallService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def recall(self, request: RecallRequest) -> RecallResult:
            assert request.query == "What is Sofias Memory?"
            return RecallResult(
                query_id=query_id,
                mode=request.mode,
                answer=None,
                context=[],
                references=[],
                timings_ms={
                    "embedding": 0,
                    "retrieval": 0,
                    "graph": 0,
                    "generation": 0,
                    "total": 0,
                },
            )

    monkeypatch.setattr("sofias_memory.api.routes.recall.RecallService", FakeRecallService)
    injected_resources: list[object] = []

    def fake_app_neo4j_resource(app: object) -> object:
        resource = object()
        injected_resources.append(resource)
        return resource

    monkeypatch.setattr(
        "sofias_memory.api.routes.recall.app_neo4j_resource", fake_app_neo4j_resource
    )
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/recall",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"query": "What is Sofias Memory?", "mode": "chunks"},
        )
        graph_response = await client.post(
            "/api/v1/recall",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"query": "What is Sofias Memory?", "mode": "graph"},
        )

    assert response.status_code == 200
    assert graph_response.status_code == 200
    payload = response.json()
    assert payload["data"]["query_id"] == str(query_id)
    assert payload["data"]["mode"] == "chunks"
    assert payload["meta"]["request_id"]
    assert graph_response.json()["data"]["mode"] == "graph"
    assert len(injected_resources) == 1
