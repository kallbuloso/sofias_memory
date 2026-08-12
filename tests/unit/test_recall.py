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
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Query
from sofias_memory.infrastructure.postgres.repositories.chunks import (
    ChunkRepository,
    RetrievedChunk,
)
from sofias_memory.schemas.recall import RecallFilters, RecallRequest, RecallResult
from sofias_memory.services.recall import (
    NO_EVIDENCE_ANSWER,
    RecallService,
    RecallUnitOfWork,
    UnitOfWorkFactory,
    reciprocal_rank_fusion,
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
        self.queries: list[Query] = []
        self.filters: list[object] = []
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
        self._store.filters.append(kwargs["filters"])
        return self._store.vector_results

    async def lexical_search(self, **kwargs: object) -> list[RetrievedChunk]:
        self._store.filters.append(kwargs["filters"])
        return self._store.lexical_results


class FakeQueryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, query: Query) -> Query:
        self._store.queries.append(query)
        return query


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.chunks = FakeRecallChunkRepository(store)
        self.queries = FakeQueryRepository(store)
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
async def test_recall_rejects_unavailable_modes_and_top_k(tmp_path: Path) -> None:
    store = FakeStore()
    seed_dataset(store)
    service, _, _ = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError, match="not available"):
        await service.recall(RecallRequest(query="memory", mode="hybrid"))
    with pytest.raises(SofiasMemoryError, match="configured maximum"):
        await service.recall(RecallRequest(query="memory", top_k=4))


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
                mode="chunks",
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
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/recall",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"query": "What is Sofias Memory?", "mode": "chunks"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["query_id"] == str(query_id)
    assert payload["data"]["mode"] == "chunks"
    assert payload["meta"]["request_id"]
