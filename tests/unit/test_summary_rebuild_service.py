from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, SummaryTargetType
from sofias_memory.infrastructure.postgres.models import Chunk, Dataset, Document, Summary
from sofias_memory.infrastructure.postgres.repositories.chunks import ChunkSummaryRebuildSnapshot
from sofias_memory.infrastructure.postgres.repositories.documents import (
    DocumentSummaryRebuildCandidate,
)
from sofias_memory.services.cognify import (
    GRAPH_EXTRACTION_PROMPT_VERSION,
    KNOWLEDGE_EXTRACTION_VERSION,
    document_summary_id,
    document_summary_metadata,
)
from sofias_memory.services.summary_rebuild_service import (
    SummaryRebuildService,
    SummaryRebuildUnitOfWork,
    UnitOfWorkFactory,
    dataset_summary_id,
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
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self.summaries: list[Summary] = []
        self.commits = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.id == dataset_id), None)


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> list[DocumentSummaryRebuildCandidate]:
        return [
            DocumentSummaryRebuildCandidate(
                id=document.id,
                dataset_id=document.dataset_id,
                source_id=document.source_id,
                generation=document.generation,
                metadata=dict(document.metadata_),
            )
            for document in sorted(self._store.documents, key=lambda item: item.id)
            if document.dataset_id == dataset_id
            and document.generation == generation
            and document.is_active
        ]

    async def get_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        document_id: UUID,
    ) -> Document | None:
        return next(
            (
                document
                for document in self._store.documents
                if document.id == document_id
                and document.dataset_id == dataset_id
                and document.generation == generation
                and document.is_active
            ),
            None,
        )


class FakeChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_document_summary(
        self,
        *,
        document_id: UUID,
        generation: int,
    ) -> list[ChunkSummaryRebuildSnapshot]:
        return [
            ChunkSummaryRebuildSnapshot(
                id=chunk.id,
                document_id=chunk.document_id,
                generation=chunk.generation,
                ordinal=chunk.ordinal,
                metadata=dict(chunk.metadata_),
            )
            for chunk in sorted(self._store.chunks, key=lambda item: (item.ordinal, item.id))
            if chunk.document_id == document_id
            and chunk.generation == generation
            and chunk.is_active
        ]


class FakeSummaryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, summary: Summary) -> Summary:
        self._store.summaries.append(summary)
        return summary

    async def get_by_id(self, summary_id: UUID) -> Summary | None:
        return next(
            (summary for summary in self._store.summaries if summary.id == summary_id),
            None,
        )

    async def list_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> list[Summary]:
        return [
            summary
            for summary in self._store.summaries
            if summary.dataset_id == dataset_id
            and summary.generation == generation
            and summary.target_type == target_type
            and summary.target_id == target_id
            and summary.level == level
            and summary.is_active
        ]

    async def deactivate_active_for_target_except(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
        keep_summary_id: UUID | None,
    ) -> int:
        summaries = await self.list_active_for_target(
            dataset_id=dataset_id,
            generation=generation,
            target_type=target_type,
            target_id=target_id,
            level=level,
        )
        deactivated = 0
        for summary in summaries:
            if keep_summary_id is not None and summary.id == keep_summary_id:
                continue
            summary.is_active = False
            deactivated += 1
        return deactivated


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.datasets = FakeDatasetRepository(store)
        self.documents = FakeDocumentRepository(store)
        self.chunks = FakeChunkRepository(store)
        self.summaries = FakeSummaryRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


class FakeDocumentSummaryClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.failure: Exception | None = None
        self.summary = "Rebuilt document summary."

    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        self.calls.append(list(chunk_summaries))
        if self.failure is not None:
            raise self.failure
        return self.summary


class FakeDatasetSummaryClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.summary = "Rebuilt dataset summary."

    async def summarize(self, document_summaries: Sequence[str]) -> str:
        self.calls.append(list(document_summaries))
        return self.summary


class FakeEmbeddingClient:
    def __init__(self, dimensions: int = 3072) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.failure: Exception | None = None

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.failure is not None:
            raise self.failure
        return [[0.1] * self.dimensions for _ in texts]


def service_for(
    tmp_path: Path,
    store: FakeStore,
) -> tuple[
    SummaryRebuildService,
    FakeDocumentSummaryClient,
    FakeDatasetSummaryClient,
    FakeEmbeddingClient,
]:
    document_client = FakeDocumentSummaryClient()
    dataset_client = FakeDatasetSummaryClient()
    embedding_client = FakeEmbeddingClient()

    def create_uow() -> SummaryRebuildUnitOfWork:
        return cast(SummaryRebuildUnitOfWork, FakeUnitOfWork(store))

    return (
        SummaryRebuildService(
            make_settings(tmp_path),
            embedding_client=embedding_client,
            document_summary_client=document_client,
            dataset_summary_client=dataset_client,
            unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
        ),
        document_client,
        dataset_client,
        embedding_client,
    )


@pytest.mark.asyncio
async def test_complete_document_and_dataset_summaries_are_noop(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    document = seed_document(store, dataset)
    document_summary = seed_document_summary(store, document, text="Current document summary.")
    document.metadata_ = document_summary_metadata(
        {},
        summary_id=document_summary.id,
        llm_model="gpt-test",
        embedding_model="embedding-test",
        config_fingerprint="old-fingerprint",
    )
    seed_dataset_summary(store, dataset, text="Current dataset summary.")
    service, document_client, dataset_client, embedding_client = service_for(tmp_path, store)

    result = await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)

    assert result.document_summaries_rebuilt == 0
    assert result.dataset_summaries_rebuilt == 0
    assert result.summaries_deactivated == 0
    assert document_client.calls == []
    assert dataset_client.calls == []
    assert embedding_client.calls == []


@pytest.mark.asyncio
async def test_missing_document_summary_rebuilds_document_and_dataset_summary(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    document = seed_document(store, dataset)
    old_summary = seed_summary(
        store,
        dataset,
        target_type=SummaryTargetType.DOCUMENT,
        target_id=document.id,
        text="Old document summary.",
    )
    seed_chunk(store, document, ordinal=1, summary="Second chunk.")
    seed_chunk(store, document, ordinal=0, summary="First chunk.")
    service, document_client, dataset_client, embedding_client = service_for(tmp_path, store)

    result = await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)

    expected_document_summary_id = document_summary_id(document.id, generation=document.generation)
    expected_dataset_summary_id = dataset_summary_id(
        dataset.id,
        generation=dataset.active_generation,
    )
    assert document_client.calls == [["First chunk.", "Second chunk."]]
    assert dataset_client.calls == [[document_client.summary]]
    assert embedding_client.calls == [[document_client.summary], [dataset_client.summary]]
    assert result.document_summaries_rebuilt == 1
    assert result.dataset_summaries_rebuilt == 1
    assert result.summaries_deactivated == 1
    assert old_summary.is_active is False
    assert any(
        summary.id == expected_document_summary_id
        and summary.target_type == SummaryTargetType.DOCUMENT
        and summary.target_id == document.id
        and summary.is_active
        for summary in store.summaries
    )
    assert any(
        summary.id == expected_dataset_summary_id
        and summary.target_type == SummaryTargetType.DATASET
        and summary.target_id == dataset.id
        and summary.level == 0
        and summary.is_active
        for summary in store.summaries
    )
    assert {summary.target_type for summary in store.summaries} <= {
        SummaryTargetType.DOCUMENT,
        SummaryTargetType.DATASET,
    }


@pytest.mark.asyncio
async def test_summary_rebuild_failure_does_not_persist_partial_summary(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    document = seed_document(store, dataset)
    old_summary = seed_summary(
        store,
        dataset,
        target_type=SummaryTargetType.DOCUMENT,
        target_id=document.id,
        text="Old document summary.",
    )
    seed_chunk(store, document, ordinal=0, summary="Chunk summary.")
    service, document_client, _, _ = service_for(tmp_path, store)
    document_client.failure = RuntimeError("provider unavailable")

    with pytest.raises(DependencyUnavailableError, match="Document summary"):
        await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)

    assert old_summary.is_active is True
    assert len(store.summaries) == 1
    assert store.commits == 0


@pytest.mark.asyncio
async def test_summary_embedding_failure_does_not_persist_partial_summary(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    document = seed_document(store, dataset)
    seed_chunk(store, document, ordinal=0, summary="Chunk summary.")
    service, _, _, embedding_client = service_for(tmp_path, store)
    embedding_client.failure = RuntimeError("embedding unavailable")

    with pytest.raises(DependencyUnavailableError, match="Embedding"):
        await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)

    assert store.summaries == []
    assert store.commits == 0


@pytest.mark.asyncio
async def test_dataset_summary_uses_document_summaries_in_deterministic_order(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    first = seed_document(store, dataset)
    first.id = UUID("10000000-0000-0000-0000-000000000001")
    second = seed_document(store, dataset)
    second.id = UUID("20000000-0000-0000-0000-000000000002")
    first_summary = seed_document_summary(store, first, text="First document.")
    second_summary = seed_document_summary(store, second, text="Second document.")
    first.metadata_ = document_summary_metadata(
        {},
        summary_id=first_summary.id,
        llm_model="gpt-test",
        embedding_model="embedding-test",
        config_fingerprint="old-fingerprint",
    )
    second.metadata_ = document_summary_metadata(
        {},
        summary_id=second_summary.id,
        llm_model="gpt-test",
        embedding_model="embedding-test",
        config_fingerprint="old-fingerprint",
    )
    service, document_client, dataset_client, _ = service_for(tmp_path, store)

    result = await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)

    assert document_client.calls == []
    assert dataset_client.calls == [["First document.", "Second document."]]
    assert result.document_summaries_rebuilt == 0
    assert result.dataset_summaries_rebuilt == 1


@pytest.mark.asyncio
async def test_missing_chunk_summaries_fail_safely(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = seed_dataset(store)
    document = seed_document(store, dataset)
    seed_chunk(store, document, ordinal=0, summary=None)
    service, _, _, _ = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError, match="complete knowledge extraction"):
        await service.rebuild_dataset(dataset.id, generation=dataset.active_generation)


def seed_dataset(store: FakeStore) -> Dataset:
    dataset = Dataset(
        id=uuid4(),
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    store.datasets.append(dataset)
    return dataset


def seed_document(store: FakeStore, dataset: Dataset) -> Document:
    document = Document(
        id=uuid4(),
        dataset_id=dataset.id,
        source_id=uuid4(),
        generation=dataset.active_generation,
        title="Document",
        language="und",
        normalized_text="Text.",
        text_sha256="a" * 64,
        token_count=1,
        metadata_={},
        is_active=True,
    )
    store.documents.append(document)
    return document


def seed_chunk(
    store: FakeStore,
    document: Document,
    *,
    ordinal: int,
    summary: str | None,
) -> Chunk:
    metadata: dict[str, object] = {}
    if summary is not None:
        metadata["knowledge_extraction"] = {
            "version": KNOWLEDGE_EXTRACTION_VERSION,
            "prompt_version": GRAPH_EXTRACTION_PROMPT_VERSION,
            "summary": summary,
        }
    chunk = Chunk(
        id=uuid4(),
        dataset_id=document.dataset_id,
        document_id=document.id,
        source_id=document.source_id,
        generation=document.generation,
        ordinal=ordinal,
        text="Chunk text.",
        content_sha256="b" * 64,
        token_count=2,
        start_char=0,
        end_char=10,
        section_path=[],
        metadata_=metadata,
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )
    store.chunks.append(chunk)
    return chunk


def seed_document_summary(store: FakeStore, document: Document, *, text: str) -> Summary:
    summary = Summary(
        id=document_summary_id(document.id, generation=document.generation),
        dataset_id=document.dataset_id,
        generation=document.generation,
        target_type=SummaryTargetType.DOCUMENT,
        target_id=document.id,
        level=0,
        text=text,
        embedding=[0.2] * 3072,
        is_active=True,
    )
    store.summaries.append(summary)
    return summary


def seed_dataset_summary(store: FakeStore, dataset: Dataset, *, text: str) -> Summary:
    summary = Summary(
        id=dataset_summary_id(dataset.id, generation=dataset.active_generation),
        dataset_id=dataset.id,
        generation=dataset.active_generation,
        target_type=SummaryTargetType.DATASET,
        target_id=dataset.id,
        level=0,
        text=text,
        embedding=[0.3] * 3072,
        is_active=True,
    )
    store.summaries.append(summary)
    return summary


def seed_summary(
    store: FakeStore,
    dataset: Dataset,
    *,
    target_type: SummaryTargetType,
    target_id: UUID,
    text: str,
) -> Summary:
    summary = Summary(
        id=uuid4(),
        dataset_id=dataset.id,
        generation=dataset.active_generation,
        target_type=target_type,
        target_id=target_id,
        level=0,
        text=text,
        embedding=[0.4] * 3072,
        is_active=True,
    )
    store.summaries.append(summary)
    return summary
