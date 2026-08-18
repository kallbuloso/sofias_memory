"""Read-only provenance service tracing knowledge back to PostgreSQL sources.

Neo4j is never consulted here: every endpoint this service backs
(``/provenance/source``, ``/provenance/relation``, ``/provenance/query``) is
answerable from PostgreSQL alone, so no artificial Neo4j dependency is
introduced.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, SourceStatus
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
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.provenance import (
    ProvenanceEntity,
    ProvenanceRelationSummary,
    QueryProvenanceReference,
    QueryProvenanceResult,
    RelationEvidenceItem,
    RelationProvenanceResult,
    SourceProvenanceChunk,
    SourceProvenanceDocument,
    SourceProvenanceResult,
)
from sofias_memory.schemas.recall import RecallFilters

QUOTE_MAX_CHARS = 500
SOURCE_NOT_FOUND_MESSAGE = "Source does not exist."
RELATION_NOT_FOUND_MESSAGE = "Relation does not exist."
QUERY_NOT_FOUND_MESSAGE = "Query does not exist."


class DatasetRepositoryForProvenance(Protocol):
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...


class SourceRepositoryForProvenance(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...


class DocumentRepositoryForProvenance(Protocol):
    async def list_for_source_generation(
        self, *, source_id: UUID, generation: int, active_only: bool = True
    ) -> list[Document]: ...


class ChunkRepositoryForProvenance(Protocol):
    async def list_for_source_generation(
        self, *, source_id: UUID, generation: int, active_only: bool = True
    ) -> list[Chunk]: ...

    async def get_active_current_reference(self, chunk_id: UUID) -> RetrievedChunk | None: ...


class EntityMentionRepositoryForProvenance(Protocol):
    async def list_active_entities_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Entity]: ...


class RelationRepositoryForProvenance(Protocol):
    async def get_by_id(self, relation_id: UUID) -> Relation | None: ...

    async def get_active_current_by_id(
        self, *, dataset_id: UUID, relation_id: UUID
    ) -> Relation | None: ...


class RelationEvidenceRepositoryForProvenance(Protocol):
    async def list_active_relations_for_chunks(
        self, *, dataset_id: UUID, chunk_ids: list[UUID]
    ) -> list[Relation]: ...

    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
        filters: RecallFilters,
    ) -> list[RecalledRelationEvidence]: ...


class QueryRepositoryForProvenance(Protocol):
    async def get_by_id(self, query_id: UUID) -> Query | None: ...


class ProvenanceUnitOfWork(Protocol):
    datasets: DatasetRepositoryForProvenance
    sources: SourceRepositoryForProvenance
    documents: DocumentRepositoryForProvenance
    chunks: ChunkRepositoryForProvenance
    entity_mentions: EntityMentionRepositoryForProvenance
    relations: RelationRepositoryForProvenance
    relation_evidence: RelationEvidenceRepositoryForProvenance
    queries: QueryRepositoryForProvenance

    async def __aenter__(self) -> ProvenanceUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


type UnitOfWorkFactory = Callable[[], ProvenanceUnitOfWork]


class ProvenanceService:
    """Trace stored knowledge back to authoritative PostgreSQL provenance."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def source(self, source_id: UUID) -> SourceProvenanceResult:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(source_id)
            if source is None:
                raise _not_found_error(SOURCE_NOT_FOUND_MESSAGE, {"source_id": str(source_id)})
            dataset = await uow.datasets.get_by_id(source.dataset_id)
            if (
                dataset is None
                or dataset.status != DatasetStatus.ACTIVE
                or source.status != SourceStatus.ACTIVE
            ):
                raise _not_found_error(SOURCE_NOT_FOUND_MESSAGE, {"source_id": str(source_id)})

            documents = await uow.documents.list_for_source_generation(
                source_id=source_id, generation=dataset.active_generation, active_only=True
            )
            chunks = await uow.chunks.list_for_source_generation(
                source_id=source_id, generation=dataset.active_generation, active_only=True
            )
            chunk_ids = [chunk.id for chunk in chunks]
            entities = await uow.entity_mentions.list_active_entities_for_chunks(
                dataset_id=dataset.id, chunk_ids=chunk_ids
            )
            relations = await uow.relation_evidence.list_active_relations_for_chunks(
                dataset_id=dataset.id, chunk_ids=chunk_ids
            )

            # Build the response while the session is still open: every ORM
            # attribute access below must happen before __aexit__ closes the
            # session, or SQLAlchemy raises DetachedInstanceError on expired
            # attributes.
            bounded_chunks = chunks[: self._settings.provenance_max_evidence]
            chunk_counts_by_document = _count_by_document(chunks)
            return SourceProvenanceResult(
                source_id=source.id,
                dataset_id=dataset.id,
                kind=source.kind,
                name=source.name,
                mime_type=source.mime_type,
                original_uri=source.original_uri,
                byte_size=source.byte_size,
                status=source.status,
                storage_available=source.storage_uri is not None,
                documents=[
                    SourceProvenanceDocument(
                        document_id=document.id,
                        title=document.title,
                        language=document.language,
                        chunk_count=chunk_counts_by_document.get(document.id, 0),
                    )
                    for document in documents
                ],
                chunks=[
                    SourceProvenanceChunk(
                        chunk_id=chunk.id,
                        document_id=chunk.document_id,
                        ordinal=chunk.ordinal,
                        quote=_quote(chunk.text),
                        start_char=chunk.start_char,
                        end_char=chunk.end_char,
                    )
                    for chunk in bounded_chunks
                ],
                entities=[
                    ProvenanceEntity(
                        entity_id=entity.id,
                        name=entity.name,
                        entity_type=entity.entity_type,
                        description=entity.description,
                    )
                    for entity in entities
                ],
                relations=[
                    ProvenanceRelationSummary(
                        relation_id=relation.id,
                        source_entity_id=relation.source_entity_id,
                        target_entity_id=relation.target_entity_id,
                        predicate=relation.predicate,
                        confidence=relation.confidence,
                    )
                    for relation in relations
                ],
            )

    async def relation(self, relation_id: UUID) -> RelationProvenanceResult:
        async with self._unit_of_work_factory() as uow:
            raw = await uow.relations.get_by_id(relation_id)
            if raw is None:
                raise _not_found_error(
                    RELATION_NOT_FOUND_MESSAGE, {"relation_id": str(relation_id)}
                )
            relation = await uow.relations.get_active_current_by_id(
                dataset_id=raw.dataset_id, relation_id=relation_id
            )
            if relation is None:
                raise _not_found_error(
                    RELATION_NOT_FOUND_MESSAGE, {"relation_id": str(relation_id)}
                )
            evidence = await uow.relation_evidence.list_active_for_recall(
                dataset_ids=[relation.dataset_id],
                relation_ids=[relation_id],
                filters=RecallFilters(),
            )

            bounded_evidence = evidence[: self._settings.provenance_max_evidence]
            return RelationProvenanceResult(
                relation_id=relation.id,
                dataset_id=relation.dataset_id,
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
                predicate=relation.predicate,
                description=relation.description,
                confidence=relation.confidence,
                evidence=[
                    RelationEvidenceItem(
                        source_id=item.source_id,
                        source_name=item.source_name,
                        document_id=item.document_id,
                        chunk_id=item.chunk_id,
                        chunk_ordinal=item.chunk_ordinal,
                        quote=_quote(item.quote),
                        start_char=item.start_char,
                        end_char=item.end_char,
                        confidence=item.confidence,
                        url=item.source_url,
                    )
                    for item in bounded_evidence
                ],
            )

    async def query(self, query_id: UUID) -> QueryProvenanceResult:
        async with self._unit_of_work_factory() as uow:
            query = await uow.queries.get_by_id(query_id)
            if query is None:
                raise _not_found_error(QUERY_NOT_FOUND_MESSAGE, {"query_id": str(query_id)})

            items = _reference_items(query.references)
            references: list[QueryProvenanceReference] = []
            for item in items:
                chunk_id = item.chunk_id
                hydrated = (
                    await uow.chunks.get_active_current_reference(chunk_id)
                    if chunk_id is not None
                    else None
                )
                references.append(_reference_from_item(item, hydrated))

            return QueryProvenanceResult(
                query_id=query.id,
                query_text=query.query_text,
                dataset_ids=list(query.dataset_ids),
                mode=query.mode,
                answer=query.answer,
                model=query.model,
                created_at=query.created_at,
                references=references,
            )


class _ReferenceItem:
    __slots__ = ("source_id", "document_id", "chunk_id", "chunk_ordinal", "score")

    def __init__(
        self,
        *,
        source_id: UUID | None,
        document_id: UUID | None,
        chunk_id: UUID | None,
        chunk_ordinal: int,
        score: float,
    ) -> None:
        self.source_id = source_id
        self.document_id = document_id
        self.chunk_id = chunk_id
        self.chunk_ordinal = chunk_ordinal
        self.score = score


def _reference_items(references: Mapping[str, object]) -> list[_ReferenceItem]:
    raw_items = references.get("items")
    if not isinstance(raw_items, Sequence):
        return []
    parsed: list[_ReferenceItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        parsed.append(
            _ReferenceItem(
                source_id=_optional_uuid(raw_item.get("source_id")),
                document_id=_optional_uuid(raw_item.get("document_id")),
                chunk_id=_optional_uuid(raw_item.get("chunk_id")),
                chunk_ordinal=_int_or_zero(raw_item.get("chunk_ordinal")),
                score=_float_or_zero(raw_item.get("score")),
            )
        )
    return parsed


def _reference_from_item(
    item: _ReferenceItem, hydrated: RetrievedChunk | None
) -> QueryProvenanceReference:
    zero = UUID(int=0)
    if hydrated is None:
        return QueryProvenanceReference(
            source_id=item.source_id or zero,
            document_id=item.document_id or zero,
            chunk_id=item.chunk_id or zero,
            chunk_ordinal=item.chunk_ordinal,
            score=item.score,
            available=False,
            quote=None,
            source_name=None,
        )
    return QueryProvenanceReference(
        source_id=hydrated.source_id,
        document_id=hydrated.document_id,
        chunk_id=hydrated.chunk_id,
        chunk_ordinal=hydrated.ordinal,
        score=item.score,
        available=True,
        quote=_quote(hydrated.text),
        source_name=hydrated.source_name,
    )


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) else 0


def _float_or_zero(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _count_by_document(chunks: Sequence[Chunk]) -> dict[UUID, int]:
    counts: dict[UUID, int] = {}
    for chunk in chunks:
        counts[chunk.document_id] = counts.get(chunk.document_id, 0) + 1
    return counts


def _quote(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= QUOTE_MAX_CHARS:
        return collapsed
    return f"{collapsed[: QUOTE_MAX_CHARS - 1].rstrip()}…"


def _not_found_error(message: str, details: dict[str, str]) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message=message,
        details=details,
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> ProvenanceUnitOfWork:
        return cast(ProvenanceUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
