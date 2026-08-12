from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    PipelineRunStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    PipelineRun,
    Source,
)
from sofias_memory.schemas.cognify import CognifyRequest
from sofias_memory.services.cognify import CognifyService, CognifyUnitOfWork, UnitOfWorkFactory

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
        "chunk_max_tokens": 24,
        "chunk_overlap_tokens": 6,
        "chunk_min_tokens": 4,
        "embedding_dimensions": 3072,
        "embedding_batch_size": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class FakeEmbeddingClient:
    def __init__(self, *, dimensions: int = 3072) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.125] * self.dimensions for _ in texts]


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.sources: list[Source] = []
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self.pipeline_runs: list[PipelineRun] = []
        self.loaded_datasets: list[Dataset] = []
        self.commits = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_slug(self, slug: str) -> Dataset | None:
        dataset = next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)
        if dataset is not None:
            self._store.loaded_datasets.append(dataset)
        return dataset


class FakeSourceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return next((source for source in self._store.sources if source.id == source_id), None)

    async def list_pending_for_dataset(self, dataset_id: UUID) -> list[Source]:
        return [
            source
            for source in self._store.sources
            if source.dataset_id == dataset_id and source.status == SourceStatus.PENDING
        ]


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
    ) -> Document | None:
        return next(
            (
                document
                for document in self._store.documents
                if document.source_id == source_id
                and document.generation == generation
                and document.is_active
            ),
            None,
        )


class FakeChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add_many(self, chunks: Sequence[Chunk]) -> list[Chunk]:
        self._store.chunks.extend(chunks)
        return list(chunks)

    async def exists_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> bool:
        return any(
            chunk.source_id == source_id
            and chunk.generation == generation
            and (chunk.is_active or not active_only)
            for chunk in self._store.chunks
        )


class FakePipelineRunRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, run: PipelineRun) -> PipelineRun:
        self._store.pipeline_runs.append(run)
        return run

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return next((run for run in self._store.pipeline_runs if run.id == run_id), None)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.sources = FakeSourceRepository(store)
        self.documents = FakeDocumentRepository(store)
        self.chunks = FakeChunkRepository(store)
        self.pipeline_runs = FakePipelineRunRepository(store)
        self._store = store

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for dataset in self._store.loaded_datasets:
            expire = getattr(dataset, "expire", None)
            if callable(expire):
                expire()
        self._store.loaded_datasets = []
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(
    tmp_path: Path,
    store: FakeStore,
    embedding_client: FakeEmbeddingClient,
) -> CognifyService:
    def create_uow() -> CognifyUnitOfWork:
        return cast(CognifyUnitOfWork, FakeUnitOfWork(store))

    return CognifyService(
        make_settings(tmp_path),
        embedding_client=embedding_client,
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
    )


def seed_pending_source(store: FakeStore, text: str) -> tuple[Dataset, Source, Document]:
    dataset = Dataset(
        id=uuid4(),
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    source = Source(
        id=uuid4(),
        dataset_id=dataset.id,
        kind=SourceKind.TEXT,
        name="note",
        mime_type="text/plain",
        original_uri=None,
        storage_uri=None,
        content_sha256="a" * 64,
        normalized_sha256="b" * 64,
        byte_size=len(text.encode("utf-8")),
        metadata_={},
        status=SourceStatus.PENDING,
        version=1,
    )
    document = Document(
        id=uuid4(),
        dataset_id=dataset.id,
        source_id=source.id,
        generation=dataset.active_generation,
        title="note",
        language="und",
        normalized_text=text,
        text_sha256="b" * 64,
        token_count=-1,
        metadata_={},
        is_active=True,
    )
    store.datasets.append(dataset)
    store.sources.append(source)
    store.documents.append(document)
    return dataset, source, document


class ExpiringDataset:
    def __init__(self) -> None:
        self._id = uuid4()
        self._slug = "main"
        self._active_generation = 0
        self._status = DatasetStatus.ACTIVE
        self._expired = False

    @property
    def id(self) -> UUID:
        self._raise_if_expired()
        return self._id

    @property
    def slug(self) -> str:
        self._raise_if_expired()
        return self._slug

    @property
    def active_generation(self) -> int:
        self._raise_if_expired()
        return self._active_generation

    @property
    def status(self) -> DatasetStatus:
        self._raise_if_expired()
        return self._status

    def expire(self) -> None:
        self._expired = True

    def _raise_if_expired(self) -> None:
        if self._expired:
            raise AssertionError("expired ORM dataset was accessed outside its UnitOfWork")


@pytest.mark.asyncio
async def test_cognify_processes_pending_source_and_persists_chunks(tmp_path: Path) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(
        store,
        "First paragraph has memory context.\n\nSecond paragraph keeps enough text. " * 6,
    )
    embedding_client = FakeEmbeddingClient()

    result = await service_for(tmp_path, store, embedding_client).cognify(CognifyRequest())

    assert result.status == PipelineRunStatus.SUCCEEDED.value
    assert result.dataset_id == dataset.id
    assert result.sources_processed == 1
    assert result.chunks == len(store.chunks)
    assert result.chunks > 0
    assert source.status == SourceStatus.ACTIVE
    assert document.token_count > 0
    assert all(chunk.source_id == source.id for chunk in store.chunks)
    assert all(len(chunk.embedding) == 3072 for chunk in store.chunks)
    assert all(chunk.lexical == "" for chunk in store.chunks)
    assert store.pipeline_runs[0].pipeline_type == PipelineType.COGNIFY
    assert store.pipeline_runs[0].status == PipelineRunStatus.SUCCEEDED
    assert "normalized_text" not in store.pipeline_runs[0].input


@pytest.mark.asyncio
async def test_cognify_uses_dataset_snapshot_outside_read_unit_of_work(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = ExpiringDataset()
    dataset_id = dataset.id
    source = Source(
        id=uuid4(),
        dataset_id=dataset_id,
        kind=SourceKind.TEXT,
        name="note",
        mime_type="text/plain",
        original_uri=None,
        storage_uri=None,
        content_sha256="a" * 64,
        normalized_sha256="b" * 64,
        byte_size=20,
        metadata_={},
        status=SourceStatus.PENDING,
        version=1,
    )
    document = Document(
        id=uuid4(),
        dataset_id=dataset_id,
        source_id=source.id,
        generation=0,
        title="note",
        language="und",
        normalized_text="Snapshot text that reaches embedding.",
        text_sha256="b" * 64,
        token_count=-1,
        metadata_={},
        is_active=True,
    )
    store.datasets.append(cast(Dataset, dataset))
    store.sources.append(source)
    store.documents.append(document)

    result = await service_for(tmp_path, store, FakeEmbeddingClient()).cognify(CognifyRequest())

    assert result.dataset_id == dataset_id
    assert source.status == SourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_cognify_invalid_embedding_dimension_marks_source_and_run_failed(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    _, source, _ = seed_pending_source(store, "Sofias Memory needs real embeddings.")
    service = service_for(tmp_path, store, FakeEmbeddingClient(dimensions=3))

    with pytest.raises(DependencyUnavailableError):
        await service.cognify(CognifyRequest())

    assert store.chunks == []
    assert source.status == SourceStatus.FAILED
    assert store.pipeline_runs[0].status == PipelineRunStatus.FAILED


@pytest.mark.asyncio
async def test_cognify_reexecution_does_not_duplicate_existing_chunks(tmp_path: Path) -> None:
    store = FakeStore()
    _, source, document = seed_pending_source(store, "Already chunked text.")
    existing_chunk = Chunk(
        id=uuid4(),
        dataset_id=source.dataset_id,
        document_id=document.id,
        source_id=source.id,
        generation=document.generation,
        ordinal=0,
        text=document.normalized_text,
        content_sha256="c" * 64,
        token_count=3,
        start_char=0,
        end_char=len(document.normalized_text),
        section_path=[],
        metadata_={},
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )
    store.chunks.append(existing_chunk)
    source.status = SourceStatus.ACTIVE
    embedding_client = FakeEmbeddingClient()

    result = await service_for(tmp_path, store, embedding_client).cognify(CognifyRequest())

    assert result.sources_processed == 0
    assert result.chunks == 0
    assert store.chunks == [existing_chunk]
    assert embedding_client.calls == []


@pytest.mark.asyncio
async def test_cognify_rejects_unsupported_wait_and_rebuild(tmp_path: Path) -> None:
    store = FakeStore()
    service = service_for(tmp_path, store, FakeEmbeddingClient())

    with pytest.raises(SofiasMemoryError, match="wait=true"):
        await service.cognify(CognifyRequest(wait=False))
    with pytest.raises(SofiasMemoryError, match="rebuild"):
        await service.cognify(CognifyRequest(rebuild=True))
