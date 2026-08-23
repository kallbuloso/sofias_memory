from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    SourceKind,
    SourceStatus,
    SummaryTargetType,
)
from sofias_memory.domain.document_reset import RESET_DOCUMENT_METADATA_KEY
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    PipelineRun,
    Relation,
    RelationEvidence,
    Source,
    Summary,
)
from sofias_memory.ports import ProjectionCommand
from sofias_memory.schemas.cognify import CognifyRequest
from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    ExtractedEntity,
    ExtractedRelation,
)
from sofias_memory.services.cognify import (
    GRAPH_EXTRACTION_PROMPT_VERSION,
    KNOWLEDGE_EXTRACTION_VERSION,
    CognifyProcessOutcome,
    CognifyService,
    CognifyUnitOfWork,
    UnitOfWorkFactory,
    cognify_run_input,
    cognify_source_ids_from_run_input,
    document_summary_id,
    document_summary_metadata,
    is_chunk_knowledge_extracted,
    rebuild_document_id,
)
from sofias_memory.services.forget import reset_document_for_recognify

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


class FakeKnowledgeExtractionClient:
    def __init__(self, extraction: ChunkKnowledgeExtraction | None = None) -> None:
        self.extraction = extraction or ChunkKnowledgeExtraction(
            summary="A retrieval summary.",
            entities=[],
            relations=[],
        )
        self.calls: list[str] = []

    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction:
        self.calls.append(chunk_text)
        return self.extraction


class FailingKnowledgeExtractionClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction:
        self.calls.append(chunk_text)
        raise ValueError("invalid after repair")


class FakeDocumentSummaryClient:
    def __init__(self, summary: str = "A retrieval-ready document summary.") -> None:
        self.summary = summary
        self.calls: list[list[str]] = []

    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        self.calls.append(list(chunk_summaries))
        return self.summary


class FailingDocumentSummaryClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def summarize(self, chunk_summaries: Sequence[str]) -> str:
        self.calls.append(list(chunk_summaries))
        raise ValueError("invalid after repair")


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.sources: list[Source] = []
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self.entities: list[Entity] = []
        self.entity_mentions: list[EntityMention] = []
        self.relations: list[Relation] = []
        self.relation_evidence: list[RelationEvidence] = []
        self.summaries: list[Summary] = []
        self.graph_outbox_commands: list[ProjectionCommand] = []
        self.pipeline_runs: list[PipelineRun] = []
        self.loaded_datasets: list[Dataset] = []
        self.commits = 0
        self.savepoints = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        dataset = next(
            (dataset for dataset in self._store.datasets if dataset.id == dataset_id), None
        )
        if dataset is not None:
            self._store.loaded_datasets.append(dataset)
        return dataset


class FakeSourceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return next((source for source in self._store.sources if source.id == source_id), None)

    async def list_for_cognify(self, dataset_id: UUID) -> list[Source]:
        return [
            source
            for source in self._store.sources
            if source.dataset_id == dataset_id
            and source.status in {SourceStatus.PENDING, SourceStatus.ACTIVE}
        ]

    async def list_for_dataset_not_deleted(self, dataset_id: UUID) -> list[Source]:
        return [
            source
            for source in self._store.sources
            if source.dataset_id == dataset_id and source.status != SourceStatus.DELETED
        ]


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, document: Document) -> Document:
        self._store.documents.append(document)
        return document

    async def get_by_id(self, document_id: UUID) -> Document | None:
        return next(
            (document for document in self._store.documents if document.id == document_id),
            None,
        )

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

    async def get_by_id(self, chunk_id: UUID) -> Chunk | None:
        return next((chunk for chunk in self._store.chunks if chunk.id == chunk_id), None)

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]:
        return [
            chunk
            for chunk in self._store.chunks
            if chunk.source_id == source_id
            and chunk.generation == generation
            and (chunk.is_active or not active_only)
        ]


class FakeEntityRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, entity: Entity) -> Entity:
        self._store.entities.append(entity)
        return entity

    async def get_active_by_canonical_key(
        self, *, dataset_id: UUID, canonical_key: str
    ) -> Entity | None:
        return next(
            (
                entity
                for entity in self._store.entities
                if entity.dataset_id == dataset_id
                and entity.canonical_key == canonical_key
                and entity.is_active
            ),
            None,
        )


class FakeEntityMentionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, mention: EntityMention) -> EntityMention:
        self._store.entity_mentions.append(mention)
        return mention

    async def exists_for_entity_chunk(self, *, entity_id: UUID, chunk_id: UUID) -> bool:
        return any(
            mention.entity_id == entity_id and mention.chunk_id == chunk_id
            for mention in self._store.entity_mentions
        )


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, relation: Relation) -> Relation:
        self._store.relations.append(relation)
        return relation

    async def get_active_by_identity(
        self,
        *,
        source_entity_id: UUID,
        target_entity_id: UUID,
        predicate: str,
        generation: int,
    ) -> Relation | None:
        return next(
            (
                relation
                for relation in self._store.relations
                if relation.source_entity_id == source_entity_id
                and relation.target_entity_id == target_entity_id
                and relation.predicate == predicate
                and relation.generation == generation
                and relation.is_active
            ),
            None,
        )


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, evidence: RelationEvidence) -> RelationEvidence:
        self._store.relation_evidence.append(evidence)
        return evidence

    async def exists_for_relation_chunk(self, *, relation_id: UUID, chunk_id: UUID) -> bool:
        return any(
            evidence.relation_id == relation_id and evidence.chunk_id == chunk_id
            for evidence in self._store.relation_evidence
        )


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

    async def get_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> Summary | None:
        return next(
            (
                summary
                for summary in self._store.summaries
                if summary.dataset_id == dataset_id
                and summary.generation == generation
                and summary.target_type == target_type
                and summary.target_id == target_id
                and summary.level == level
                and summary.is_active
            ),
            None,
        )


class FakeGraphOutboxRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add_projection_command(self, command: ProjectionCommand) -> object:
        self._store.graph_outbox_commands.append(command)
        return object()


class FakeSavepoint:
    """In-memory stand-in for a real SAVEPOINT.

    The fake store has no rollback, so this only records that the service
    scoped each source's writes in one; real per-source rollback isolation is
    proven against PostgreSQL in
    ``tests/integration/test_cognify_async_postgres_integration.py``.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def __aenter__(self) -> FakeSavepoint:
        self._store.savepoints += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None:
        return None


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.sources = FakeSourceRepository(store)
        self.documents = FakeDocumentRepository(store)
        self.chunks = FakeChunkRepository(store)
        self.entities = FakeEntityRepository(store)
        self.entity_mentions = FakeEntityMentionRepository(store)
        self.relations = FakeRelationRepository(store)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.summaries = FakeSummaryRepository(store)
        self.graph_outbox = FakeGraphOutboxRepository(store)
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

    def savepoint(self) -> FakeSavepoint:
        return FakeSavepoint(self._store)


def service_for(
    tmp_path: Path,
    store: FakeStore,
    embedding_client: FakeEmbeddingClient,
    knowledge_client: (
        FakeKnowledgeExtractionClient | FailingKnowledgeExtractionClient | None
    ) = None,
    document_summary_client: (
        FakeDocumentSummaryClient | FailingDocumentSummaryClient | None
    ) = None,
    store_for_persist: FakeStore | None = None,
) -> CognifyService:
    def create_uow() -> CognifyUnitOfWork:
        return cast(CognifyUnitOfWork, FakeUnitOfWork(store))

    service = CognifyService(
        make_settings(tmp_path),
        embedding_client=embedding_client,
        knowledge_extraction_client=knowledge_client or FakeKnowledgeExtractionClient(),
        document_summary_client=document_summary_client or FakeDocumentSummaryClient(),
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
    )
    _STORE_BY_SERVICE[id(service)] = store_for_persist or store
    return service


_STORE_BY_SERVICE: dict[int, FakeStore] = {}


async def run_cognify(
    service: CognifyService,
    dataset_id: UUID,
    *,
    source_ids: list[UUID] | None = None,
    rebuild: bool = False,
) -> CognifyProcessOutcome:
    """Drive the service exactly the way the ``process_sources`` step does.

    Two phases, never one: ``prepare_batch`` computes without writing, then
    ``persist_batch`` applies the batch inside a caller-supplied unit of work
    -- the engine's transaction in production, a ``FakeUnitOfWork`` here.
    """

    batch = await service.prepare_batch(
        dataset_id=dataset_id,
        source_ids=source_ids,
        rebuild=rebuild,
    )
    store = _STORE_BY_SERVICE[id(service)]
    uow = cast(CognifyUnitOfWork, FakeUnitOfWork(store))
    async with uow:
        return await service.persist_batch(uow, batch)


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


def mark_chunk_knowledge_complete(chunk: Chunk, summary: str = "Existing chunk summary.") -> None:
    chunk.metadata_ = {
        **chunk.metadata_,
        "knowledge_extraction": {
            "version": KNOWLEDGE_EXTRACTION_VERSION,
            "prompt_version": GRAPH_EXTRACTION_PROMPT_VERSION,
            "summary": summary,
        },
    }


def seed_document_summary(store: FakeStore, document: Document) -> Summary:
    summary_id = document_summary_id(document.id, generation=document.generation)
    summary = Summary(
        id=summary_id,
        dataset_id=document.dataset_id,
        generation=document.generation,
        target_type=SummaryTargetType.DOCUMENT,
        target_id=document.id,
        level=0,
        text="Existing document summary.",
        embedding=[0.2] * 3072,
        is_active=True,
    )
    store.summaries.append(summary)
    document.metadata_ = document_summary_metadata(
        document.metadata_,
        summary_id=summary_id,
        llm_model="gpt-5-mini",
        embedding_model="text-embedding-3-large",
        config_fingerprint="existing-fingerprint",
    )
    return summary


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

    result = await run_cognify(service_for(tmp_path, store, embedding_client), dataset.id)

    assert result.dataset_id == dataset.id
    assert result.target_generation == dataset.active_generation
    assert result.rebuild is False
    assert result.sources_processed == 1
    assert result.chunks == len(store.chunks)
    assert result.chunks > 0
    assert source.status == SourceStatus.ACTIVE
    assert document.token_count > 0
    assert all(chunk.source_id == source.id for chunk in store.chunks)
    assert all(len(chunk.embedding) == 3072 for chunk in store.chunks)
    assert all(chunk.lexical == "" for chunk in store.chunks)
    assert len(store.summaries) == 1
    assert store.summaries[0].target_type == SummaryTargetType.DOCUMENT
    assert store.summaries[0].target_id == document.id
    assert store.summaries[0].level == 0
    assert len(store.summaries[0].embedding) == 3072
    assert "document_summary" in document.metadata_
    # The service never creates or finalizes a PipelineRun any more (SM-510):
    # that lifecycle belongs entirely to the B5 engine.
    assert store.pipeline_runs == []


@pytest.mark.asyncio
async def test_cognify_rehydrates_memory_reset_document_from_source_storage(tmp_path: Path) -> None:
    store = FakeStore()
    original_text = "Fresh source bytes are normalized again after memory-only forget."
    forgotten_text = "Forgotten normalized text must never be reused."
    dataset, source, old_document = seed_pending_source(store, forgotten_text)
    storage_path = tmp_path / str(dataset.id) / str(source.id) / "original.txt"
    storage_path.parent.mkdir(parents=True)
    storage_path.write_text(original_text, encoding="utf-8")
    source.storage_uri = storage_path.as_uri()
    source.content_sha256 = sha256(original_text.encode("utf-8")).hexdigest()
    source.normalized_sha256 = source.content_sha256
    source.byte_size = len(original_text.encode("utf-8"))
    old_document.title = "Forgotten title"
    old_document.language = "pt-BR"
    old_document.metadata_ = {"document_summary": {"summary_id": str(uuid4())}}
    old_document.is_active = False
    reset_document = reset_document_for_recognify(old_document)
    store.documents.append(reset_document)

    result = await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id)

    assert result.sources_processed == 1
    assert source.status == SourceStatus.ACTIVE
    assert reset_document.normalized_text == original_text
    assert reset_document.normalized_text != forgotten_text
    assert reset_document.text_sha256 == sha256(original_text.encode("utf-8")).hexdigest()
    assert reset_document.token_count > 0
    assert reset_document.title == source.name
    assert reset_document.language == "und"
    assert "forget_memory_reset" not in reset_document.metadata_
    assert "document_summary" in reset_document.metadata_
    assert store.chunks
    assert all(forgotten_text not in chunk.text for chunk in store.chunks)
    assert all(original_text in chunk.text for chunk in store.chunks)


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

    result = await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset_id)

    assert result.dataset_id == dataset_id
    assert source.status == SourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_cognify_invalid_embedding_dimension_aborts_the_whole_batch(
    tmp_path: Path,
) -> None:
    """A provider fault is transient, not this source's fault.

    It propagates out of the computation phase, so ``persist`` never runs and
    nothing at all is committed -- including any source-status change. The
    source stays exactly as it was and is retried on the next attempt, instead
    of being left ``FAILED`` by a side effect the run never finished (which is
    what B4's incremental commits used to do).
    """

    store = FakeStore()
    dataset, source, _ = seed_pending_source(store, "Sofias Memory needs real embeddings.")
    service = service_for(tmp_path, store, FakeEmbeddingClient(dimensions=3))

    with pytest.raises(DependencyUnavailableError):
        await run_cognify(service, dataset.id)

    assert store.chunks == []
    assert store.commits == 0
    assert source.status == SourceStatus.PENDING
    # The failure propagates untouched: classifying it into a retryable or
    # permanent step outcome is the pipeline step's job, not the service's.
    assert store.pipeline_runs == []


@pytest.mark.asyncio
async def test_cognify_reexecution_does_not_duplicate_existing_chunks(tmp_path: Path) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Already chunked text.")
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
    mark_chunk_knowledge_complete(existing_chunk)
    store.chunks.append(existing_chunk)
    seed_document_summary(store, document)
    source.status = SourceStatus.ACTIVE
    embedding_client = FakeEmbeddingClient()
    knowledge_client = FakeKnowledgeExtractionClient()
    document_summary_client = FakeDocumentSummaryClient()

    result = await run_cognify(
        service_for(
            tmp_path,
            store,
            embedding_client,
            knowledge_client,
            document_summary_client,
        ),
        dataset.id,
    )

    assert result.sources_processed == 0
    assert result.chunks == 0
    assert store.chunks == [existing_chunk]
    assert embedding_client.calls == []
    assert knowledge_client.calls == []
    assert document_summary_client.calls == []


@pytest.mark.asyncio
async def test_cognify_enriches_existing_chunks_canonically_and_idempotently(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "PostgreSQL supports Sofias Memory.")
    source.status = SourceStatus.ACTIVE
    for ordinal in range(2):
        store.chunks.append(
            Chunk(
                id=uuid4(),
                dataset_id=source.dataset_id,
                document_id=document.id,
                source_id=source.id,
                generation=document.generation,
                ordinal=ordinal,
                text=document.normalized_text,
                content_sha256=f"{ordinal + 1:064x}",
                token_count=5,
                start_char=0,
                end_char=len(document.normalized_text),
                section_path=[],
                metadata_={"existing": "preserved"},
                embedding=[0.1] * 3072,
                lexical="",
                is_active=True,
            )
        )
    extraction = ChunkKnowledgeExtraction(
        summary="PostgreSQL replication is described.",
        entities=[
            ExtractedEntity(
                local_id="e1",
                name="PostgreSQL",
                type="Technology",
                description="A database technology.",
                aliases=[],
                confidence=0.9,
            ),
            ExtractedEntity(
                local_id="e2",
                name="Sofias Memory",
                type="System",
                description="A persistent memory system.",
                aliases=[],
                confidence=0.9,
            ),
        ],
        relations=[
            ExtractedRelation(
                source_local_id="e1",
                target_local_id="e2",
                predicate="supports",
                description="PostgreSQL supports Sofias Memory.",
                confidence=0.8,
                evidence="PostgreSQL supports Sofias Memory.",
            )
        ],
    )
    embedding_client = FakeEmbeddingClient()
    knowledge_client = FakeKnowledgeExtractionClient(extraction)
    document_summary_client = FakeDocumentSummaryClient()
    service = service_for(
        tmp_path,
        store,
        embedding_client,
        knowledge_client,
        document_summary_client,
    )

    result = await run_cognify(service, dataset.id)

    assert result.chunks == 0
    assert result.entities == 2
    assert result.relations == 1
    assert len(store.entities) == 2
    assert len(store.entity_mentions) == 4
    assert len(store.relations) == 1
    assert len(store.relation_evidence) == 2
    assert all(chunk.metadata_["existing"] == "preserved" for chunk in store.chunks)
    assert all("knowledge_extraction" in chunk.metadata_ for chunk in store.chunks)
    assert source.status == SourceStatus.ACTIVE
    assert embedding_client.calls == [[document_summary_client.summary]]
    assert len(store.summaries) == 1
    assert [command.aggregate_type for command in store.graph_outbox_commands] == [
        "entity",
        "entity",
        "chunk",
        "chunk",
        "entity_mention",
        "entity_mention",
        "entity_mention",
        "entity_mention",
        "relation",
        "chunk_next",
    ]
    assert all(
        "text" not in command.to_payload()["properties"] for command in store.graph_outbox_commands
    )

    second = await run_cognify(service, dataset.id)

    assert second.sources_processed == 0
    assert second.chunks == second.entities == second.relations == 0
    assert len(knowledge_client.calls) == 2
    assert len(store.entities) == 2
    assert len(store.entity_mentions) == 4
    assert len(store.relations) == 1
    assert len(store.relation_evidence) == 2
    assert len(store.summaries) == 1
    assert len(document_summary_client.calls) == 1


@pytest.mark.asyncio
async def test_cognify_never_projects_the_graph_itself(tmp_path: Path) -> None:
    """SM-510 boundary fix: Cognify writes ``graph_outbox`` rows and stops.

    Draining them is a Neo4j call, and neither phase of the step may make one
    -- ``execute`` because the rows do not exist yet (inserting them is an
    authoritative mutation), ``persist`` because it is PostgreSQL-only.
    Projection converges through SM-506's autonomous consumer instead.
    """

    store = FakeStore()
    dataset, source, _ = seed_pending_source(store, "Projection is somebody else's job.")
    service = service_for(tmp_path, store, FakeEmbeddingClient())

    result = await run_cognify(service, dataset.id)

    assert result.sources_processed == 1
    assert source.status == SourceStatus.ACTIVE
    assert store.graph_outbox_commands
    assert not hasattr(service, "_graph_projection_drain")


@pytest.mark.asyncio
async def test_cognify_writes_nothing_before_the_persist_phase(tmp_path: Path) -> None:
    """The whole point of the SM-510 boundary fix.

    ``prepare_batch`` runs every provider call and resolves every identity,
    yet PostgreSQL sees no chunk, entity, relation, summary or outbox row and
    no ``commit`` at all until ``persist_batch`` is handed a transaction.
    """

    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Nothing is durable before persist.")
    service = service_for(tmp_path, store, FakeEmbeddingClient())

    batch = await service.prepare_batch(dataset_id=dataset.id, source_ids=None, rebuild=False)

    assert batch.sources
    assert store.commits == 0
    assert store.chunks == []
    assert store.entities == []
    assert store.relations == []
    assert store.summaries == []
    assert store.graph_outbox_commands == []
    assert source.status == SourceStatus.PENDING
    assert document.token_count == -1

    uow = cast(CognifyUnitOfWork, FakeUnitOfWork(store))
    async with uow:
        outcome = await service.persist_batch(uow, batch)

    # persist_batch never commits either: the engine owns that boundary.
    assert store.commits == 0
    assert outcome.chunks == len(store.chunks) > 0
    assert source.status == SourceStatus.ACTIVE
    assert store.savepoints == 1


@pytest.mark.asyncio
async def test_one_entity_is_shared_by_every_source_in_the_same_batch(tmp_path: Path) -> None:
    """Cross-source canonical deduplication now happens in memory.

    Nothing commits between sources any more, so a second source can no
    longer discover the first source's entity in the database -- the batch
    itself has to resolve both mentions of one ``canonical_key`` to one
    provisional entity, never to two.
    """

    store = FakeStore()
    dataset, first, _ = seed_pending_source(store, "PostgreSQL supports Sofias Memory.")
    second = Source(
        id=uuid4(),
        dataset_id=dataset.id,
        kind=SourceKind.TEXT,
        name="second",
        mime_type="text/plain",
        original_uri=None,
        storage_uri=None,
        content_sha256="c" * 64,
        normalized_sha256="d" * 64,
        byte_size=42,
        metadata_={},
        status=SourceStatus.PENDING,
        version=1,
    )
    store.sources.append(second)
    store.documents.append(
        Document(
            id=uuid4(),
            dataset_id=dataset.id,
            source_id=second.id,
            generation=0,
            title="second",
            language="und",
            normalized_text="PostgreSQL supports Sofias Memory in another note.",
            text_sha256="d" * 64,
            token_count=-1,
            metadata_={},
            is_active=True,
        )
    )
    extraction = ChunkKnowledgeExtraction(
        summary="PostgreSQL supports Sofias Memory.",
        entities=[
            ExtractedEntity(
                local_id="e1",
                name="PostgreSQL",
                type="Technology",
                description="A database technology.",
                aliases=[],
                confidence=0.9,
            ),
            ExtractedEntity(
                local_id="e2",
                name="Sofias Memory",
                type="System",
                description="A persistent memory system.",
                aliases=[],
                confidence=0.9,
            ),
        ],
        relations=[
            ExtractedRelation(
                source_local_id="e1",
                target_local_id="e2",
                predicate="supports",
                description="PostgreSQL supports Sofias Memory.",
                confidence=0.8,
                evidence="PostgreSQL supports Sofias Memory.",
            )
        ],
    )

    result = await run_cognify(
        service_for(
            tmp_path,
            store,
            FakeEmbeddingClient(),
            FakeKnowledgeExtractionClient(extraction),
        ),
        dataset.id,
    )

    assert result.sources_processed == 2
    assert result.entities == 2
    assert result.relations == 1
    assert len(store.entities) == 2
    assert len({entity.canonical_key for entity in store.entities}) == 2
    assert len(store.relations) == 1
    assert first.status == second.status == SourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_cognify_invalid_enrichment_preserves_chunks_and_marks_failure(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Already embedded content.")
    source.status = SourceStatus.ACTIVE
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

    with pytest.raises(DependencyUnavailableError, match="Knowledge extraction"):
        await run_cognify(
            service_for(
                tmp_path,
                store,
                FakeEmbeddingClient(),
                FailingKnowledgeExtractionClient(),
            ),
            dataset.id,
        )

    assert store.chunks == [existing_chunk]
    assert store.entities == []
    assert store.entity_mentions == []
    assert store.relations == []
    assert store.relation_evidence == []
    assert store.commits == 0
    # Nothing was committed, so the source keeps the status it already had.
    assert source.status == SourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_cognify_adds_only_document_summary_to_sm404_source(tmp_path: Path) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Knowledge already extracted.")
    source.status = SourceStatus.ACTIVE
    chunk = Chunk(
        id=uuid4(),
        dataset_id=source.dataset_id,
        document_id=document.id,
        source_id=source.id,
        generation=document.generation,
        ordinal=0,
        text=document.normalized_text,
        content_sha256="d" * 64,
        token_count=3,
        start_char=0,
        end_char=len(document.normalized_text),
        section_path=[],
        metadata_={},
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )
    mark_chunk_knowledge_complete(chunk, "Knowledge is already extracted.")
    store.chunks.append(chunk)
    embedding_client = FakeEmbeddingClient()
    knowledge_client = FakeKnowledgeExtractionClient()
    document_summary_client = FakeDocumentSummaryClient()

    result = await run_cognify(
        service_for(
            tmp_path,
            store,
            embedding_client,
            knowledge_client,
            document_summary_client,
        ),
        dataset.id,
    )

    assert result.sources_processed == 1
    assert result.chunks == result.entities == result.relations == 0
    assert knowledge_client.calls == []
    assert document_summary_client.calls == [["Knowledge is already extracted."]]
    assert embedding_client.calls == [[document_summary_client.summary]]
    assert len(store.summaries) == 1
    assert store.summaries[0].id == document_summary_id(
        document.id,
        generation=document.generation,
    )
    assert source.status == SourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_document_summary_failure_does_not_persist_partial_knowledge(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Existing embedded chunk.")
    source.status = SourceStatus.ACTIVE
    chunk = Chunk(
        id=uuid4(),
        dataset_id=source.dataset_id,
        document_id=document.id,
        source_id=source.id,
        generation=document.generation,
        ordinal=0,
        text=document.normalized_text,
        content_sha256="e" * 64,
        token_count=3,
        start_char=0,
        end_char=len(document.normalized_text),
        section_path=[],
        metadata_={},
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )
    store.chunks.append(chunk)

    with pytest.raises(DependencyUnavailableError, match="Document summary"):
        await run_cognify(
            service_for(
                tmp_path,
                store,
                FakeEmbeddingClient(),
                FakeKnowledgeExtractionClient(),
                FailingDocumentSummaryClient(),
            ),
            dataset.id,
        )

    assert chunk.metadata_ == {}
    assert store.summaries == []
    assert "document_summary" not in document.metadata_
    assert store.commits == 0
    assert source.status == SourceStatus.ACTIVE


def test_knowledge_marker_does_not_depend_on_global_config_fingerprint() -> None:
    chunk = Chunk(
        id=uuid4(),
        dataset_id=uuid4(),
        document_id=uuid4(),
        source_id=uuid4(),
        generation=0,
        ordinal=0,
        text="Existing knowledge.",
        content_sha256="f" * 64,
        token_count=2,
        start_char=0,
        end_char=19,
        section_path=[],
        metadata_={
            "knowledge_extraction": {
                "version": KNOWLEDGE_EXTRACTION_VERSION,
                "prompt_version": GRAPH_EXTRACTION_PROMPT_VERSION,
                "config_fingerprint": "fingerprint-before-document-summary-prompt",
                "summary": "Existing summary.",
            }
        },
        embedding=[0.1] * 3072,
        lexical="",
        is_active=True,
    )

    assert is_chunk_knowledge_extracted(chunk) is True


@pytest.mark.asyncio
async def test_cognify_unknown_dataset_is_not_found(tmp_path: Path) -> None:
    store = FakeStore()
    service = service_for(tmp_path, store, FakeEmbeddingClient())

    with pytest.raises(SofiasMemoryError, match="Dataset does not exist") as raised:
        await run_cognify(service, uuid4())

    assert raised.value.status_code == 404


# --- rebuild / new generation (SM-510) ---------------------------------------


@pytest.mark.asyncio
async def test_rebuild_targets_the_next_generation_without_touching_the_current_one(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, source, document = seed_pending_source(store, "Rebuildable content for generation 1.")
    await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id)
    generation_zero_chunks = list(store.chunks)
    assert generation_zero_chunks

    result = await run_cognify(
        service_for(tmp_path, store, FakeEmbeddingClient()),
        dataset.id,
        rebuild=True,
    )

    assert result.rebuild is True
    assert result.target_generation == 1
    assert result.sources_processed == 1
    # The dataset itself is untouched here: only the activation step may ever
    # advance active_generation, and it does so in the engine's transaction.
    assert dataset.active_generation == 0
    assert all(chunk.generation == 0 for chunk in generation_zero_chunks)
    assert [chunk for chunk in store.chunks if chunk.generation == 1]
    assert document.generation == 0
    rebuilt_documents = [item for item in store.documents if item.generation == 1]
    assert len(rebuilt_documents) == 1
    assert rebuilt_documents[0].id == rebuild_document_id(source.id, generation=1)
    assert rebuilt_documents[0].normalized_text == document.normalized_text


@pytest.mark.asyncio
async def test_rebuild_covers_every_non_pending_source_not_only_pending_ones(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, first, _ = seed_pending_source(store, "First source stays active after cognify.")
    second = Source(
        id=uuid4(),
        dataset_id=dataset.id,
        kind=SourceKind.TEXT,
        name="second",
        mime_type="text/plain",
        original_uri=None,
        storage_uri=None,
        content_sha256="c" * 64,
        normalized_sha256="d" * 64,
        byte_size=42,
        metadata_={},
        status=SourceStatus.FAILED,
        version=1,
    )
    store.sources.append(second)
    store.documents.append(
        Document(
            id=uuid4(),
            dataset_id=dataset.id,
            source_id=second.id,
            generation=0,
            title="second",
            language="und",
            normalized_text="A previously failed source is rebuilt too.",
            text_sha256="d" * 64,
            token_count=-1,
            metadata_={},
            is_active=True,
        )
    )
    await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id)
    assert first.status == SourceStatus.ACTIVE

    result = await run_cognify(
        service_for(tmp_path, store, FakeEmbeddingClient()),
        dataset.id,
        rebuild=True,
    )

    assert result.sources_processed == 2
    rebuilt_source_ids = {chunk.source_id for chunk in store.chunks if chunk.generation == 1}
    assert rebuilt_source_ids == {first.id, second.id}


@pytest.mark.asyncio
async def test_rebuild_reexecution_does_not_duplicate_new_generation_artifacts(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    dataset, _, _ = seed_pending_source(store, "Interrupted rebuilds replay idempotently.")
    await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id)
    await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id, rebuild=True)
    first_pass = [chunk.id for chunk in store.chunks if chunk.generation == 1]
    documents_after_first = len(store.documents)

    result = await run_cognify(
        service_for(tmp_path, store, FakeEmbeddingClient()),
        dataset.id,
        rebuild=True,
    )

    assert result.sources_processed == 0
    assert result.chunks == 0
    assert [chunk.id for chunk in store.chunks if chunk.generation == 1] == first_pass
    assert len(store.documents) == documents_after_first


@pytest.mark.asyncio
async def test_rebuild_keeps_a_reset_document_reset_in_the_new_generation(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    original_text = "Rebuilt source bytes are normalized again."
    dataset, source, document = seed_pending_source(store, original_text)
    storage_path = tmp_path / str(dataset.id) / str(source.id) / "original.txt"
    storage_path.parent.mkdir(parents=True)
    storage_path.write_text(original_text, encoding="utf-8")
    source.storage_uri = storage_path.as_uri()
    source.content_sha256 = sha256(original_text.encode("utf-8")).hexdigest()
    source.byte_size = len(original_text.encode("utf-8"))
    document.is_active = False
    reset_document = reset_document_for_recognify(document)
    store.documents.append(reset_document)

    await run_cognify(service_for(tmp_path, store, FakeEmbeddingClient()), dataset.id, rebuild=True)

    rebuilt = next(item for item in store.documents if item.generation == 1)
    assert rebuilt.normalized_text == original_text
    assert RESET_DOCUMENT_METADATA_KEY not in rebuilt.metadata_
    assert [chunk for chunk in store.chunks if chunk.generation == 1]


def test_cognify_run_input_excludes_wait_from_the_work_identity() -> None:
    waiting = cognify_run_input(CognifyRequest(wait=True))
    not_waiting = cognify_run_input(CognifyRequest(wait=False))

    assert "wait" not in waiting
    assert waiting == not_waiting
    assert waiting == {"dataset": "main", "source_ids": None, "rebuild": False}


def test_cognify_run_input_serializes_source_ids_as_strings() -> None:
    source_id = uuid4()

    run_input = cognify_run_input(CognifyRequest(source_ids=[source_id], rebuild=False))

    assert run_input["source_ids"] == [str(source_id)]


def test_cognify_source_ids_round_trip_through_persisted_run_input() -> None:
    source_id = uuid4()
    run_input = cognify_run_input(CognifyRequest(source_ids=[source_id]))

    assert cognify_source_ids_from_run_input(run_input) == [source_id]
    assert cognify_source_ids_from_run_input({"source_ids": None}) is None


def test_cognify_source_ids_reject_uninterpretable_persisted_input() -> None:
    with pytest.raises(SofiasMemoryError, match="not interpretable"):
        cognify_source_ids_from_run_input({"source_ids": "not-a-list"})
    with pytest.raises(SofiasMemoryError, match="not interpretable"):
        cognify_source_ids_from_run_input({"source_ids": ["not-a-uuid"]})
