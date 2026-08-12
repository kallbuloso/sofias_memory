"""Synchronous cognify service for chunks, embeddings, and structured knowledge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import DEFAULT_PROMPT_VERSIONS, Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
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
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.chunking import (
    CHUNK_ALGORITHM_VERSION,
    TextChunk,
    TextTokenizer,
    chunk_document_text,
    document_token_count,
)
from sofias_memory.schemas.cognify import CognifyRequest, CognifyResult
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    ExtractedEntity,
    canonical_entity_key,
    normalize_relation_predicate,
)
from sofias_memory.services.remember import stable_payload_hash

COGNIFY_CHUNK_STEP = "chunk_embeddings"
COGNIFY_RESULT_METRIC_KEY = "cognify_result"
KNOWLEDGE_EXTRACTION_VERSION = "v1"
GRAPH_EXTRACTION_PROMPT_VERSION = DEFAULT_PROMPT_VERSIONS["graph_extraction"]


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class KnowledgeExtractionClient(Protocol):
    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction: ...


class DatasetRepositoryForCognify(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class SourceRepositoryForCognify(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def list_for_cognify(self, dataset_id: UUID) -> list[Source]: ...


class DocumentRepositoryForCognify(Protocol):
    async def get_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
    ) -> Document | None: ...


class ChunkRepositoryForCognify(Protocol):
    async def add_many(self, chunks: Sequence[Chunk]) -> list[Chunk]: ...
    async def get_by_id(self, chunk_id: UUID) -> Chunk | None: ...
    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]: ...


class EntityRepositoryForCognify(Protocol):
    async def add(self, entity: Entity) -> Entity: ...
    async def get_active_by_canonical_key(
        self,
        *,
        dataset_id: UUID,
        canonical_key: str,
    ) -> Entity | None: ...


class EntityMentionRepositoryForCognify(Protocol):
    async def add(self, mention: EntityMention) -> EntityMention: ...
    async def exists_for_entity_chunk(self, *, entity_id: UUID, chunk_id: UUID) -> bool: ...


class RelationRepositoryForCognify(Protocol):
    async def add(self, relation: Relation) -> Relation: ...
    async def get_active_by_identity(
        self,
        *,
        source_entity_id: UUID,
        target_entity_id: UUID,
        predicate: str,
        generation: int,
    ) -> Relation | None: ...


class RelationEvidenceRepositoryForCognify(Protocol):
    async def add(self, evidence: RelationEvidence) -> RelationEvidence: ...
    async def exists_for_relation_chunk(self, *, relation_id: UUID, chunk_id: UUID) -> bool: ...


class PipelineRunRepositoryForCognify(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...


class CognifyUnitOfWork(Protocol):
    datasets: DatasetRepositoryForCognify
    sources: SourceRepositoryForCognify
    documents: DocumentRepositoryForCognify
    chunks: ChunkRepositoryForCognify
    entities: EntityRepositoryForCognify
    entity_mentions: EntityMentionRepositoryForCognify
    relations: RelationRepositoryForCognify
    relation_evidence: RelationEvidenceRepositoryForCognify
    pipeline_runs: PipelineRunRepositoryForCognify

    async def __aenter__(self) -> CognifyUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], CognifyUnitOfWork]


@dataclass(frozen=True)
class CognifyDatasetSnapshot:
    id: UUID
    slug: str
    active_generation: int


@dataclass(frozen=True)
class CognifyChunkSnapshot:
    id: UUID
    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    generation: int
    ordinal: int
    text: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class CognifySourceWorkItem:
    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    generation: int
    normalized_text: str | None
    chunks: tuple[CognifyChunkSnapshot, ...]


@dataclass(frozen=True)
class CognifyPreparedSource:
    work_item: CognifySourceWorkItem
    document_tokens: int | None
    new_chunks: tuple[TextChunk, ...]
    embeddings: tuple[list[float], ...]
    extractions: tuple[tuple[UUID, ChunkKnowledgeExtraction], ...]


@dataclass(frozen=True)
class CognifyPersistedCounts:
    chunks: int
    entities: int
    relations: int


class CognifyService:
    """Cognify remembered documents without holding database transactions over network I/O."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        knowledge_extraction_client: KnowledgeExtractionClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._knowledge_extraction_client = knowledge_extraction_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )
        self._tokenizer = TextTokenizer(settings.embedding_model)

    async def cognify(self, request: CognifyRequest) -> CognifyResult:
        self._validate_supported_request(request)
        run_id = await self._create_running_run(request)
        try:
            return await self._cognify(run_id, request)
        except Exception as exc:
            await self._mark_run_failed(run_id, exc)
            raise

    def _validate_supported_request(self, request: CognifyRequest) -> None:
        if request.rebuild is True:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Cognify rebuild is not available in this checkpoint.",
                details={"rebuild": request.rebuild},
            )
        if request.wait is not True:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Only wait=true is supported until the worker is implemented.",
                details={"wait": request.wait},
            )

    async def _create_running_run(self, request: CognifyRequest) -> UUID:
        run_input = cognify_run_input(request)
        now = utc_now()
        run_id = uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.COGNIFY,
            dataset_id=None,
            source_id=None,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=stable_payload_hash(run_input),
            input=run_input,
            progress=0.0,
            current_step=COGNIFY_CHUNK_STEP,
            attempt=1,
            worker_id=None,
            heartbeat_at=None,
            config_fingerprint=self._settings.config_fingerprint(),
            error_code=None,
            error_message=None,
            metrics={},
            started_at=now,
            finished_at=None,
        )
        async with self._unit_of_work_factory() as uow:
            await uow.pipeline_runs.add(run)
            await uow.commit()
        return run_id

    async def _cognify(self, run_id: UUID, request: CognifyRequest) -> CognifyResult:
        dataset = await self._load_dataset_snapshot(request.dataset)
        source_ids = await self._select_source_ids(dataset, request.source_ids)
        totals = CognifyPersistedCounts(chunks=0, entities=0, relations=0)
        sources_processed = 0

        for source_id in source_ids:
            work_item = await self._claim_source_work_item(
                dataset,
                source_id,
                allow_failed=request.source_ids is not None,
            )
            if work_item is None:
                continue
            try:
                prepared = await self._prepare_source(work_item)
                persisted = await self._persist_prepared_source(prepared)
            except Exception:
                await self._mark_source_failed(work_item.source_id)
                raise
            totals = CognifyPersistedCounts(
                chunks=totals.chunks + persisted.chunks,
                entities=totals.entities + persisted.entities,
                relations=totals.relations + persisted.relations,
            )
            sources_processed += 1

        result = CognifyResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED.value,
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            sources_processed=sources_processed,
            chunks=totals.chunks,
            entities=totals.entities,
            relations=totals.relations,
        )
        await self._mark_run_succeeded(run_id, result, dataset_id=dataset.id)
        return result

    async def _load_dataset_snapshot(self, dataset_slug: str) -> CognifyDatasetSnapshot:
        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_slug(dataset_slug)
            if dataset is None or dataset.status != DatasetStatus.ACTIVE:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Dataset does not exist.",
                    details={"dataset": dataset_slug},
                )
            return CognifyDatasetSnapshot(
                id=dataset.id,
                slug=dataset.slug,
                active_generation=dataset.active_generation,
            )

    async def _select_source_ids(
        self,
        dataset: CognifyDatasetSnapshot,
        source_ids: list[UUID] | None,
    ) -> list[UUID]:
        async with self._unit_of_work_factory() as uow:
            sources = (
                await self._load_requested_sources(uow, dataset=dataset, source_ids=source_ids)
                if source_ids is not None
                else await uow.sources.list_for_cognify(dataset.id)
            )
            selected: list[UUID] = []
            for source in sources:
                chunks = await uow.chunks.list_for_source_generation(
                    source_id=source.id,
                    generation=dataset.active_generation,
                )
                if chunks and all(is_chunk_knowledge_extracted(chunk) for chunk in chunks):
                    continue
                if (
                    chunks
                    and source.status
                    in {SourceStatus.ACTIVE, SourceStatus.PENDING, SourceStatus.FAILED}
                ) or (not chunks and source.status == SourceStatus.PENDING):
                    selected.append(source.id)
            return selected

    async def _claim_source_work_item(
        self,
        dataset: CognifyDatasetSnapshot,
        source_id: UUID,
        *,
        allow_failed: bool,
    ) -> CognifySourceWorkItem | None:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(source_id)
            if source is None or source.dataset_id != dataset.id:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Source does not exist.",
                    details={"source_id": str(source_id)},
                )
            chunks = await uow.chunks.list_for_source_generation(
                source_id=source.id,
                generation=dataset.active_generation,
            )
            chunks_to_extract = tuple(
                chunk_snapshot(chunk) for chunk in chunks if not is_chunk_knowledge_extracted(chunk)
            )
            if chunks and not chunks_to_extract:
                source.status = SourceStatus.ACTIVE
                await uow.commit()
                return None
            if source.status == SourceStatus.FAILED and not allow_failed:
                return None
            if source.status not in {
                SourceStatus.PENDING,
                SourceStatus.ACTIVE,
                SourceStatus.FAILED,
            }:
                return None
            if chunks:
                document_id = chunks[0].document_id
                normalized_text: str | None = None
            else:
                if source.status != SourceStatus.PENDING:
                    return None
                document = await uow.documents.get_for_source_generation(
                    source_id=source.id,
                    generation=dataset.active_generation,
                )
                if document is None:
                    raise SofiasMemoryError(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.BAD_REQUEST,
                        message=(
                            "Source has no active normalized document for the dataset generation."
                        ),
                        details={"source_id": str(source.id)},
                    )
                document_id = document.id
                normalized_text = document.normalized_text
            source.status = SourceStatus.PROCESSING
            work_item = CognifySourceWorkItem(
                dataset_id=dataset.id,
                source_id=source.id,
                document_id=document_id,
                generation=dataset.active_generation,
                normalized_text=normalized_text,
                chunks=chunks_to_extract,
            )
            await uow.commit()
            return work_item

    async def _load_requested_sources(
        self,
        uow: CognifyUnitOfWork,
        *,
        dataset: CognifyDatasetSnapshot,
        source_ids: list[UUID],
    ) -> list[Source]:
        sources: list[Source] = []
        seen: set[UUID] = set()
        for source_id in source_ids:
            if source_id in seen:
                continue
            seen.add(source_id)
            source = await uow.sources.get_by_id(source_id)
            if source is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Source does not exist.",
                    details={"source_id": str(source_id)},
                )
            if source.dataset_id != dataset.id:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.BAD_REQUEST,
                    message="Source does not belong to the requested dataset.",
                    details={"source_id": str(source_id), "dataset": dataset.slug},
                )
            sources.append(source)
        return sources

    async def _prepare_source(self, work_item: CognifySourceWorkItem) -> CognifyPreparedSource:
        if work_item.chunks:
            chunk_snapshots = work_item.chunks
            document_tokens = None
            text_chunks: tuple[TextChunk, ...] = ()
            embeddings: tuple[list[float], ...] = ()
        else:
            normalized_text = work_item.normalized_text
            if normalized_text is None:
                raise RuntimeError("new chunk preparation requires normalized text")
            document_tokens = document_token_count(normalized_text, self._tokenizer)
            text_chunks = tuple(
                chunk_document_text(
                    normalized_text,
                    document_id=work_item.document_id,
                    generation=work_item.generation,
                    tokenizer=self._tokenizer,
                    max_tokens=self._settings.chunk_max_tokens,
                    overlap_tokens=self._settings.chunk_overlap_tokens,
                    min_tokens=self._settings.chunk_min_tokens,
                )
            )
            embeddings = tuple(await self._embed_chunks(text_chunks))
            chunk_snapshots = tuple(
                chunk_snapshot_from_text_chunk(chunk, work_item) for chunk in text_chunks
            )

        extractions = await self._extract_chunk_knowledge(chunk_snapshots)
        return CognifyPreparedSource(
            work_item=work_item,
            document_tokens=document_tokens,
            new_chunks=text_chunks,
            embeddings=embeddings,
            extractions=tuple(
                (chunk.id, extraction)
                for chunk, extraction in zip(chunk_snapshots, extractions, strict=True)
            ),
        )

    async def _embed_chunks(self, chunks: Sequence[TextChunk]) -> list[list[float]]:
        try:
            embeddings = await self._embedding_client.embed_texts([chunk.text for chunk in chunks])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        self._validate_embeddings(embeddings, expected_count=len(chunks))
        return embeddings

    async def _extract_chunk_knowledge(
        self,
        chunks: Sequence[CognifyChunkSnapshot],
    ) -> list[ChunkKnowledgeExtraction]:
        try:
            return list(
                await asyncio.gather(
                    *(self._knowledge_extraction_client.extract(chunk.text) for chunk in chunks)
                )
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Knowledge extraction failed.") from exc

    def _validate_embeddings(self, embeddings: list[list[float]], *, expected_count: int) -> None:
        if len(embeddings) != expected_count:
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected number of vectors."
            )
        if any(len(embedding) != self._settings.embedding_dimensions for embedding in embeddings):
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected vector dimension."
            )

    async def _persist_prepared_source(
        self,
        prepared: CognifyPreparedSource,
    ) -> CognifyPersistedCounts:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(prepared.work_item.source_id)
            if source is None:
                raise _snapshot_persistence_error()

            if prepared.new_chunks:
                document = await uow.documents.get_for_source_generation(
                    source_id=prepared.work_item.source_id,
                    generation=prepared.work_item.generation,
                )
                if document is None or prepared.document_tokens is None:
                    raise _snapshot_persistence_error()
                chunks = [
                    chunk_model_from_text_chunk(
                        text_chunk,
                        dataset_id=prepared.work_item.dataset_id,
                        document_id=prepared.work_item.document_id,
                        source_id=prepared.work_item.source_id,
                        generation=prepared.work_item.generation,
                        embedding=embedding,
                        embedding_model=self._settings.embedding_model,
                        config_fingerprint=self._settings.config_fingerprint(),
                    )
                    for text_chunk, embedding in zip(
                        prepared.new_chunks,
                        prepared.embeddings,
                        strict=True,
                    )
                ]
                await uow.chunks.add_many(chunks)
                document.token_count = prepared.document_tokens
                chunks_created = len(chunks)
            else:
                chunks_created = 0

            entities_created = 0
            relations_created = 0
            for chunk_id, extraction in prepared.extractions:
                chunk = await uow.chunks.get_by_id(chunk_id)
                if chunk is None:
                    raise _snapshot_persistence_error()
                chunk_entities: dict[str, Entity] = {}
                for extracted_entity in extraction.entities:
                    entity, created = await self._resolve_entity(
                        uow,
                        extracted_entity,
                        dataset_id=prepared.work_item.dataset_id,
                        generation=prepared.work_item.generation,
                    )
                    entities_created += int(created)
                    chunk_entities[extracted_entity.local_id] = entity
                    if not await uow.entity_mentions.exists_for_entity_chunk(
                        entity_id=entity.id,
                        chunk_id=chunk.id,
                    ):
                        await uow.entity_mentions.add(
                            entity_mention_from_extraction(extracted_entity, entity.id, chunk)
                        )

                for extracted_relation in extraction.relations:
                    source_entity = chunk_entities[extracted_relation.source_local_id]
                    target_entity = chunk_entities[extracted_relation.target_local_id]
                    predicate = normalize_relation_predicate(extracted_relation.predicate)
                    relation = await uow.relations.get_active_by_identity(
                        source_entity_id=source_entity.id,
                        target_entity_id=target_entity.id,
                        predicate=predicate,
                        generation=prepared.work_item.generation,
                    )
                    if relation is None:
                        relation = Relation(
                            id=uuid4(),
                            dataset_id=prepared.work_item.dataset_id,
                            generation=prepared.work_item.generation,
                            source_entity_id=source_entity.id,
                            target_entity_id=target_entity.id,
                            predicate=predicate,
                            description=extracted_relation.description,
                            properties={},
                            confidence=extracted_relation.confidence,
                            importance_weight=1.0,
                            embedding=None,
                            is_active=True,
                        )
                        await uow.relations.add(relation)
                        relations_created += 1
                    else:
                        relation.confidence = max(
                            float(relation.confidence), extracted_relation.confidence
                        )
                        if not relation.description:
                            relation.description = extracted_relation.description
                    if not await uow.relation_evidence.exists_for_relation_chunk(
                        relation_id=relation.id,
                        chunk_id=chunk.id,
                    ):
                        await uow.relation_evidence.add(
                            RelationEvidence(
                                relation_id=relation.id,
                                chunk_id=chunk.id,
                                quote=extracted_relation.evidence,
                                confidence=extracted_relation.confidence,
                            )
                        )
                chunk.metadata_ = knowledge_extraction_metadata(
                    chunk.metadata_,
                    extraction.summary,
                    llm_model=self._settings.llm_model,
                    config_fingerprint=self._settings.config_fingerprint(),
                )

            source.status = SourceStatus.ACTIVE
            await uow.commit()
            return CognifyPersistedCounts(
                chunks=chunks_created,
                entities=entities_created,
                relations=relations_created,
            )

    async def _resolve_entity(
        self,
        uow: CognifyUnitOfWork,
        extracted: ExtractedEntity,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> tuple[Entity, bool]:
        canonical_key = canonical_entity_key(extracted.entity_type, extracted.name)
        entity = await uow.entities.get_active_by_canonical_key(
            dataset_id=dataset_id,
            canonical_key=canonical_key,
        )
        if entity is not None:
            entity.aliases = list(dict.fromkeys((*entity.aliases, *extracted.aliases)))
            entity.confidence = max(float(entity.confidence), extracted.confidence)
            return entity, False
        entity = Entity(
            id=uuid4(),
            dataset_id=dataset_id,
            generation=generation,
            canonical_key=canonical_key,
            name=extracted.name,
            entity_type=extracted.entity_type,
            description=extracted.description,
            aliases=extracted.aliases,
            properties={},
            confidence=extracted.confidence,
            importance_weight=1.0,
            embedding=None,
            is_active=True,
        )
        await uow.entities.add(entity)
        return entity, True

    async def _mark_source_failed(self, source_id: UUID) -> None:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(source_id)
            if source is not None:
                source.status = SourceStatus.FAILED
            await uow.commit()

    async def _mark_run_succeeded(
        self,
        run_id: UUID,
        result: CognifyResult,
        *,
        dataset_id: UUID,
    ) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            run.dataset_id = dataset_id
            run.status = PipelineRunStatus.SUCCEEDED
            run.progress = 1.0
            run.current_step = None
            run.error_code = None
            run.error_message = None
            run.metrics = {COGNIFY_RESULT_METRIC_KEY: result.model_dump(mode="json")}
            run.finished_at = utc_now()
            await uow.commit()

    async def _mark_run_failed(self, run_id: UUID, exc: Exception) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            run.status = PipelineRunStatus.FAILED
            run.progress = 1.0
            run.current_step = None
            run.error_code = type(exc).__name__
            run.error_message = "Cognify failed."
            run.finished_at = utc_now()
            await uow.commit()


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> CognifyUnitOfWork:
        return cast(CognifyUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work


def cognify_run_input(request: CognifyRequest) -> dict[str, JSONValue]:
    return {
        "dataset": request.dataset,
        "source_ids": [str(source_id) for source_id in request.source_ids]
        if request.source_ids is not None
        else None,
        "rebuild": request.rebuild,
        "wait": request.wait,
    }


def chunk_model_from_text_chunk(
    chunk: TextChunk,
    *,
    dataset_id: UUID,
    document_id: UUID,
    source_id: UUID,
    generation: int,
    embedding: list[float],
    embedding_model: str,
    config_fingerprint: str,
) -> Chunk:
    return Chunk(
        id=chunk.id,
        dataset_id=dataset_id,
        document_id=document_id,
        source_id=source_id,
        generation=generation,
        ordinal=chunk.ordinal,
        text=chunk.text,
        content_sha256=chunk.content_sha256,
        token_count=chunk.token_count,
        start_char=chunk.start_char,
        end_char=chunk.end_char,
        section_path=list(chunk.section_path),
        metadata_={
            "chunk_algorithm_version": CHUNK_ALGORITHM_VERSION,
            "embedding_model": embedding_model,
            "config_fingerprint": config_fingerprint,
        },
        embedding=embedding,
        lexical="",
        is_active=True,
    )


def chunk_snapshot(chunk: Chunk) -> CognifyChunkSnapshot:
    return CognifyChunkSnapshot(
        id=chunk.id,
        dataset_id=chunk.dataset_id,
        source_id=chunk.source_id,
        document_id=chunk.document_id,
        generation=chunk.generation,
        ordinal=chunk.ordinal,
        text=chunk.text,
        metadata=dict(chunk.metadata_),
    )


def chunk_snapshot_from_text_chunk(
    chunk: TextChunk,
    work_item: CognifySourceWorkItem,
) -> CognifyChunkSnapshot:
    return CognifyChunkSnapshot(
        id=chunk.id,
        dataset_id=work_item.dataset_id,
        source_id=work_item.source_id,
        document_id=work_item.document_id,
        generation=work_item.generation,
        ordinal=chunk.ordinal,
        text=chunk.text,
        metadata={},
    )


def is_chunk_knowledge_extracted(chunk: Chunk | CognifyChunkSnapshot) -> bool:
    marker = (
        chunk.metadata.get("knowledge_extraction")
        if isinstance(chunk, CognifyChunkSnapshot)
        else chunk.metadata_.get("knowledge_extraction")
    )
    return (
        isinstance(marker, dict)
        and marker.get("version") == KNOWLEDGE_EXTRACTION_VERSION
        and marker.get("prompt_version") == GRAPH_EXTRACTION_PROMPT_VERSION
    )


def knowledge_extraction_metadata(
    existing: dict[str, object],
    summary: str,
    *,
    llm_model: str,
    config_fingerprint: str,
) -> dict[str, object]:
    metadata = dict(existing)
    metadata["knowledge_extraction"] = {
        "version": KNOWLEDGE_EXTRACTION_VERSION,
        "prompt_version": GRAPH_EXTRACTION_PROMPT_VERSION,
        "llm_model": llm_model,
        "config_fingerprint": config_fingerprint,
        "summary": summary,
    }
    return metadata


def entity_mention_from_extraction(
    extracted: ExtractedEntity,
    entity_id: UUID,
    chunk: Chunk,
) -> EntityMention:
    surface_text = extracted.name
    start_char: int | None = None
    end_char: int | None = None
    for candidate in (extracted.name, *extracted.aliases):
        candidate_start = chunk.text.find(candidate)
        if candidate_start >= 0:
            surface_text = candidate
            start_char = candidate_start
            end_char = candidate_start + len(candidate)
            break
    return EntityMention(
        id=uuid4(),
        entity_id=entity_id,
        chunk_id=chunk.id,
        surface_text=surface_text,
        start_char=start_char,
        end_char=end_char,
        confidence=extracted.confidence,
    )


def _snapshot_persistence_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Cognify source snapshot could not be persisted.",
    )
