from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    Query,
    Relation,
    Source,
)
from sofias_memory.infrastructure.postgres.repositories.chunks import RetrievedChunk
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RecalledRelationEvidence,
)
from sofias_memory.schemas.recall import RecallFilters
from sofias_memory.services.provenance import (
    ProvenanceService,
    ProvenanceUnitOfWork,
    UnitOfWorkFactory,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
HASH = "a" * 64
VECTOR = [0.0] * 3072


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "provenance_max_evidence": 10,
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


def make_source(
    dataset_id: UUID,
    *,
    source_id: UUID | None = None,
    status: SourceStatus = SourceStatus.ACTIVE,
    storage_uri: str | None = "file:///data/x/content.txt",
    original_uri: str | None = None,
) -> Source:
    return Source(
        id=source_id or uuid4(),
        dataset_id=dataset_id,
        kind=SourceKind.TEXT,
        name="source.txt",
        mime_type="text/plain",
        original_uri=original_uri,
        storage_uri=storage_uri,
        content_sha256=HASH,
        normalized_sha256=HASH,
        byte_size=100,
        metadata_={},
        status=status,
        version=1,
    )


def make_document(
    dataset_id: UUID, source_id: UUID, *, document_id: UUID | None = None
) -> Document:
    return Document(
        id=document_id or uuid4(),
        dataset_id=dataset_id,
        source_id=source_id,
        generation=0,
        title="Document title",
        language="en",
        normalized_text="content",
        text_sha256=HASH,
        token_count=10,
        metadata_={},
        is_active=True,
    )


def make_chunk(
    dataset_id: UUID,
    document_id: UUID,
    source_id: UUID,
    *,
    chunk_id: UUID | None = None,
    ordinal: int = 0,
    is_active: bool = True,
    generation: int = 0,
) -> Chunk:
    return Chunk(
        id=chunk_id or uuid4(),
        dataset_id=dataset_id,
        document_id=document_id,
        source_id=source_id,
        generation=generation,
        ordinal=ordinal,
        text="Some chunk text.",
        content_sha256=HASH,
        token_count=5,
        start_char=0,
        end_char=16,
        section_path=[],
        metadata_={},
        embedding=VECTOR,
        lexical="",
        is_active=is_active,
    )


def make_entity(dataset_id: UUID, *, entity_id: UUID | None = None, name: str = "Ada") -> Entity:
    return Entity(
        id=entity_id or uuid4(),
        dataset_id=dataset_id,
        generation=0,
        canonical_key=name.lower(),
        name=name,
        entity_type="person",
        description="A description.",
        aliases=[],
        properties={},
        confidence=0.9,
        importance_weight=0.5,
        embedding=None,
        is_active=True,
    )


def make_relation(
    dataset_id: UUID,
    *,
    relation_id: UUID | None = None,
    source_entity_id: UUID,
    target_entity_id: UUID,
    predicate: str = "knows",
    is_active: bool = True,
) -> Relation:
    return Relation(
        id=relation_id or uuid4(),
        dataset_id=dataset_id,
        generation=0,
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
        self.sources: list[Source] = []
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self.evidence: list[RecalledRelationEvidence] = []
        self.queries: list[Query] = []


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return next((d for d in self._store.datasets if d.id == dataset_id), None)


class FakeSourceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return next((s for s in self._store.sources if s.id == source_id), None)


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_source_generation(
        self, *, source_id: UUID, generation: int, active_only: bool = True
    ) -> list[Document]:
        return [
            doc
            for doc in self._store.documents
            if doc.source_id == source_id
            and doc.generation == generation
            and (not active_only or doc.is_active)
        ]


class FakeChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_source_generation(
        self, *, source_id: UUID, generation: int, active_only: bool = True
    ) -> list[Chunk]:
        return [
            chunk
            for chunk in self._store.chunks
            if chunk.source_id == source_id
            and chunk.generation == generation
            and (not active_only or chunk.is_active)
        ]

    async def get_active_current_reference(self, chunk_id: UUID) -> RetrievedChunk | None:
        chunk = next((c for c in self._store.chunks if c.id == chunk_id and c.is_active), None)
        if chunk is None:
            return None
        source = next((s for s in self._store.sources if s.id == chunk.source_id), None)
        if source is None or source.status != SourceStatus.ACTIVE:
            return None
        return RetrievedChunk(
            chunk_id=chunk.id,
            dataset_id=chunk.dataset_id,
            source_id=chunk.source_id,
            source_name=source.name,
            source_url=source.original_uri,
            document_id=chunk.document_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
        )


class FakeEntityMentionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.mentions: dict[UUID, list[UUID]] = {}

    async def list_active_entities_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Entity]:
        entity_ids: set[UUID] = set()
        for chunk_id in chunk_ids:
            entity_ids.update(self.mentions.get(chunk_id, []))
        return [entity for entity in self._store.entities if entity.id in entity_ids]


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, relation_id: UUID) -> Relation | None:
        return next((r for r in self._store.relations if r.id == relation_id), None)

    async def get_active_current_by_id(
        self, *, dataset_id: UUID, relation_id: UUID
    ) -> Relation | None:
        relation = await self.get_by_id(relation_id)
        if relation is None or relation.dataset_id != dataset_id or not relation.is_active:
            return None
        dataset = next((d for d in self._store.datasets if d.id == dataset_id), None)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            return None
        return relation


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.relations_for_chunks: list[Relation] = []

    async def list_active_relations_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Relation]:
        return self.relations_for_chunks

    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
        filters: RecallFilters,
    ) -> list[RecalledRelationEvidence]:
        ids = set(relation_ids)
        return [item for item in self._store.evidence if item.relation_id in ids]


class FakeQueryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, query_id: UUID) -> Query | None:
        return next((q for q in self._store.queries if q.id == query_id), None)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.datasets = FakeDatasetRepository(store)
        self.sources = FakeSourceRepository(store)
        self.documents = FakeDocumentRepository(store)
        self.chunks = FakeChunkRepository(store)
        self.entity_mentions = FakeEntityMentionRepository(store)
        self.relations = FakeRelationRepository(store)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.queries = FakeQueryRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def service_for(
    tmp_path: Path, store: FakeStore, *, settings: Settings | None = None
) -> ProvenanceService:
    def create_uow() -> ProvenanceUnitOfWork:
        return cast(ProvenanceUnitOfWork, FakeUnitOfWork(store))

    return ProvenanceService(
        settings or make_settings(tmp_path),
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
    )


# --- PROVENANCE SOURCE ---------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_source_active_returns_lineage(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id)
    store.sources.append(source)
    document = make_document(dataset.id, source.id)
    store.documents.append(document)
    chunk = make_chunk(dataset.id, document.id, source.id)
    store.chunks.append(chunk)
    entity = make_entity(dataset.id)
    store.entities.append(entity)
    relation = make_relation(dataset.id, source_entity_id=entity.id, target_entity_id=entity.id)
    store.relations.append(relation)

    uow_holder: FakeUnitOfWork | None = None

    def create_uow() -> ProvenanceUnitOfWork:
        nonlocal uow_holder
        uow = FakeUnitOfWork(store)
        uow_holder = uow
        cast(FakeEntityMentionRepository, uow.entity_mentions).mentions[chunk.id] = [entity.id]
        cast(FakeRelationEvidenceRepository, uow.relation_evidence).relations_for_chunks = [
            relation
        ]
        return cast(ProvenanceUnitOfWork, uow)

    service = ProvenanceService(
        make_settings(tmp_path), unit_of_work_factory=cast(UnitOfWorkFactory, create_uow)
    )

    result = await service.source(source.id)

    assert result.source_id == source.id
    assert result.dataset_id == dataset.id
    assert result.storage_available is True
    assert [d.document_id for d in result.documents] == [document.id]
    assert [c.chunk_id for c in result.chunks] == [chunk.id]
    assert [e.entity_id for e in result.entities] == [entity.id]
    assert [r.relation_id for r in result.relations] == [relation.id]
    del uow_holder


@pytest.mark.asyncio
async def test_provenance_source_deleted_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id, status=SourceStatus.DELETED)
    store.sources.append(source)
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.source(source.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_source_pending_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id, status=SourceStatus.PENDING)
    store.sources.append(source)
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.source(source.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_source_deleting_dataset_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset(status=DatasetStatus.DELETING)
    store.datasets.append(dataset)
    source = make_source(dataset.id)
    store.sources.append(source)
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.source(source.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_source_stale_chunk_is_not_returned(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id)
    store.sources.append(source)
    document = make_document(dataset.id, source.id)
    store.documents.append(document)
    inactive_chunk = make_chunk(dataset.id, document.id, source.id, is_active=False)
    store.chunks.append(inactive_chunk)
    service = service_for(tmp_path, store)

    result = await service.source(source.id)

    assert result.chunks == []


@pytest.mark.asyncio
async def test_provenance_source_does_not_expose_internal_storage_uri(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id, storage_uri="file:///C:/secret/internal/path/content.txt")
    store.sources.append(source)
    service = service_for(tmp_path, store)

    result = await service.source(source.id)

    dumped = result.model_dump_json()
    assert "secret" not in dumped
    assert "file://" not in dumped
    assert result.storage_available is True


@pytest.mark.asyncio
async def test_provenance_source_does_not_expose_full_normalized_text(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id)
    store.sources.append(source)
    document = make_document(dataset.id, source.id)
    store.documents.append(document)
    service = service_for(tmp_path, store)

    result = await service.source(source.id)

    dumped = result.model_dump()
    assert "normalized_text" not in dumped["documents"][0]


# --- PROVENANCE RELATION --------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_relation_active_returns_evidence(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    entity_a = make_entity(dataset.id)
    entity_b = make_entity(dataset.id)
    store.entities.extend([entity_a, entity_b])
    relation = make_relation(dataset.id, source_entity_id=entity_a.id, target_entity_id=entity_b.id)
    store.relations.append(relation)
    store.evidence.append(
        RecalledRelationEvidence(
            relation_id=relation.id,
            chunk_id=uuid4(),
            quote="Ada worked with Charles.",
            confidence=0.9,
            dataset_id=dataset.id,
            source_id=uuid4(),
            source_name="letters.txt",
            source_url=None,
            document_id=uuid4(),
            chunk_ordinal=0,
            start_char=0,
            end_char=25,
        )
    )
    service = service_for(tmp_path, store)

    result = await service.relation(relation.id)

    assert result.relation_id == relation.id
    assert len(result.evidence) == 1
    assert result.evidence[0].quote == "Ada worked with Charles."


@pytest.mark.asyncio
async def test_provenance_relation_multiple_evidences(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    entity_a = make_entity(dataset.id)
    entity_b = make_entity(dataset.id)
    store.entities.extend([entity_a, entity_b])
    relation = make_relation(dataset.id, source_entity_id=entity_a.id, target_entity_id=entity_b.id)
    store.relations.append(relation)
    for index in range(3):
        store.evidence.append(
            RecalledRelationEvidence(
                relation_id=relation.id,
                chunk_id=uuid4(),
                quote=f"Evidence {index}.",
                confidence=0.5 + index * 0.1,
                dataset_id=dataset.id,
                source_id=uuid4(),
                source_name="source.txt",
                source_url=None,
                document_id=uuid4(),
                chunk_ordinal=index,
                start_char=0,
                end_char=10,
            )
        )
    service = service_for(tmp_path, store)

    result = await service.relation(relation.id)

    assert len(result.evidence) == 3


@pytest.mark.asyncio
async def test_provenance_relation_inactive_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    entity_a = make_entity(dataset.id)
    entity_b = make_entity(dataset.id)
    store.entities.extend([entity_a, entity_b])
    relation = make_relation(
        dataset.id, source_entity_id=entity_a.id, target_entity_id=entity_b.id, is_active=False
    )
    store.relations.append(relation)
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.relation(relation.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_relation_missing_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.relation(uuid4())

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_relation_does_not_leak_other_dataset(tmp_path: Path) -> None:
    store = FakeStore()
    dataset_a = make_dataset("dataset-a")
    dataset_b = make_dataset("dataset-b", status=DatasetStatus.DELETING)
    store.datasets.extend([dataset_a, dataset_b])
    entity_a = make_entity(dataset_b.id)
    entity_b = make_entity(dataset_b.id)
    store.entities.extend([entity_a, entity_b])
    relation = make_relation(
        dataset_b.id, source_entity_id=entity_a.id, target_entity_id=entity_b.id
    )
    store.relations.append(relation)
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.relation(relation.id)

    assert excinfo.value.status_code == 404


# --- PROVENANCE QUERY -----------------------------------------------------------


def make_query(
    *,
    query_id: UUID | None = None,
    dataset_ids: list[UUID] | None = None,
    references_items: list[dict[str, object]] | None = None,
) -> Query:
    return Query(
        id=query_id or uuid4(),
        query_text="What is Sofias Memory?",
        dataset_ids=dataset_ids or [uuid4()],
        mode="rag",
        answer="Grounded answer.",
        references={"items": references_items or []},
        timings={"total": 10},
        model="gpt-5-mini",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_provenance_query_existing_returns_metadata(tmp_path: Path) -> None:
    store = FakeStore()
    query = make_query()
    store.queries.append(query)
    service = service_for(tmp_path, store)

    result = await service.query(query.id)

    assert result.query_id == query.id
    assert result.mode == "rag"
    assert result.answer == "Grounded answer."


@pytest.mark.asyncio
async def test_provenance_query_hydrates_authoritative_reference(tmp_path: Path) -> None:
    store = FakeStore()
    dataset = make_dataset()
    store.datasets.append(dataset)
    source = make_source(dataset.id)
    store.sources.append(source)
    document = make_document(dataset.id, source.id)
    store.documents.append(document)
    chunk = make_chunk(dataset.id, document.id, source.id)
    store.chunks.append(chunk)
    query = make_query(
        dataset_ids=[dataset.id],
        references_items=[
            {
                "source_id": str(source.id),
                "document_id": str(document.id),
                "chunk_id": str(chunk.id),
                "chunk_ordinal": 0,
                "score": 0.9,
            }
        ],
    )
    store.queries.append(query)
    service = service_for(tmp_path, store)

    result = await service.query(query.id)

    assert len(result.references) == 1
    assert result.references[0].available is True
    assert result.references[0].quote == "Some chunk text."


@pytest.mark.asyncio
async def test_provenance_query_forgotten_reference_does_not_expose_quote(tmp_path: Path) -> None:
    store = FakeStore()
    forgotten_chunk_id = uuid4()
    query = make_query(
        references_items=[
            {
                "source_id": str(uuid4()),
                "document_id": str(uuid4()),
                "chunk_id": str(forgotten_chunk_id),
                "chunk_ordinal": 0,
                "score": 0.9,
            }
        ]
    )
    store.queries.append(query)
    service = service_for(tmp_path, store)

    result = await service.query(query.id)

    assert len(result.references) == 1
    assert result.references[0].available is False
    assert result.references[0].quote is None


@pytest.mark.asyncio
async def test_provenance_query_missing_returns_404(tmp_path: Path) -> None:
    store = FakeStore()
    service = service_for(tmp_path, store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.query(uuid4())

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_provenance_query_does_not_depend_on_neo4j(tmp_path: Path) -> None:
    """ProvenanceService.__init__ never accepts a Neo4j client at all."""

    import inspect

    signature = inspect.signature(ProvenanceService.__init__)
    assert "graph_client" not in signature.parameters
    assert "neo4j" not in str(signature).lower()
