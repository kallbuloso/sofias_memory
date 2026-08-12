"""Synchronous cognify service for chunks, embeddings, and structured knowledge."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4, uuid5

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import DEFAULT_PROMPT_VERSIONS, Settings
from sofias_memory.domain import (
    DatasetStatus,
    PipelineRunStatus,
    PipelineType,
    SourceStatus,
    SummaryTargetType,
)
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
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.chunking import (
    CHUNK_ALGORITHM_VERSION,
    TextChunk,
    TextTokenizer,
    chunk_document_text,
    document_token_count,
)
from sofias_memory.ports import (
    ProjectionCommand,
    chunk_next_upsert_command,
    chunk_upsert_command,
    entity_mention_upsert_command,
    entity_upsert_command,
    relation_upsert_command,
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
DOCUMENT_SUMMARY_VERSION = "v1"
DOCUMENT_SUMMARY_PROMPT_VERSION = DEFAULT_PROMPT_VERSIONS["document_summary"]


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class KnowledgeExtractionClient(Protocol):
    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction: ...


class DocumentSummaryClient(Protocol):
    async def summarize(self, chunk_summaries: Sequence[str]) -> str: ...


class DatasetRepositoryForCognify(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class SourceRepositoryForCognify(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def list_for_cognify(self, dataset_id: UUID) -> list[Source]: ...


class DocumentRepositoryForCognify(Protocol):
    async def get_by_id(self, document_id: UUID) -> Document | None: ...
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


class SummaryRepositoryForCognify(Protocol):
    async def add(self, summary: Summary) -> Summary: ...
    async def get_by_id(self, summary_id: UUID) -> Summary | None: ...
    async def get_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> Summary | None: ...


class GraphOutboxRepositoryForCognify(Protocol):
    async def add_projection_command(self, command: ProjectionCommand) -> object: ...


class GraphProjectionDrain(Protocol):
    async def process_dataset(self, dataset_id: UUID) -> object: ...


class CognifyUnitOfWork(Protocol):
    datasets: DatasetRepositoryForCognify
    sources: SourceRepositoryForCognify
    documents: DocumentRepositoryForCognify
    chunks: ChunkRepositoryForCognify
    entities: EntityRepositoryForCognify
    entity_mentions: EntityMentionRepositoryForCognify
    relations: RelationRepositoryForCognify
    relation_evidence: RelationEvidenceRepositoryForCognify
    summaries: SummaryRepositoryForCognify
    graph_outbox: GraphOutboxRepositoryForCognify
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
    existing_chunks: tuple[CognifyChunkSnapshot, ...]
    chunks_to_extract: tuple[CognifyChunkSnapshot, ...]


@dataclass(frozen=True)
class CognifyPreparedSource:
    work_item: CognifySourceWorkItem
    document_tokens: int | None
    new_chunks: tuple[TextChunk, ...]
    embeddings: tuple[list[float], ...]
    extractions: tuple[tuple[UUID, ChunkKnowledgeExtraction], ...]
    document_summary_id: UUID
    document_summary_text: str
    document_summary_embedding: list[float]


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
        document_summary_client: DocumentSummaryClient,
        graph_projection_drain: GraphProjectionDrain,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._knowledge_extraction_client = knowledge_extraction_client
        self._document_summary_client = document_summary_client
        self._graph_projection_drain = graph_projection_drain
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

        await self._graph_projection_drain.process_dataset(dataset.id)

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
                if source.status in {
                    SourceStatus.ACTIVE,
                    SourceStatus.PENDING,
                    SourceStatus.FAILED,
                }:
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
            document = await uow.documents.get_for_source_generation(
                source_id=source.id,
                generation=dataset.active_generation,
            )
            if document is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.BAD_REQUEST,
                    message="Source has no active normalized document for the dataset generation.",
                    details={"source_id": str(source.id)},
                )
            active_summary = await uow.summaries.get_by_id(
                document_summary_id(document.id, generation=document.generation)
            )
            if (
                chunks
                and all(is_chunk_knowledge_extracted(chunk) for chunk in chunks)
                and (is_document_summary_complete(document, active_summary))
            ):
                if source.status != SourceStatus.ACTIVE:
                    source.status = SourceStatus.ACTIVE
                    await uow.commit()
                return None
            chunks_to_extract = tuple(
                chunk_snapshot(chunk) for chunk in chunks if not is_chunk_knowledge_extracted(chunk)
            )
            if source.status == SourceStatus.FAILED and not allow_failed:
                return None
            if source.status not in {
                SourceStatus.PENDING,
                SourceStatus.ACTIVE,
                SourceStatus.FAILED,
            }:
                return None
            if not chunks and source.status == SourceStatus.ACTIVE:
                return None
            source.status = SourceStatus.PROCESSING
            work_item = CognifySourceWorkItem(
                dataset_id=dataset.id,
                source_id=source.id,
                document_id=document.id,
                generation=dataset.active_generation,
                normalized_text=document.normalized_text if not chunks else None,
                existing_chunks=tuple(chunk_snapshot(chunk) for chunk in chunks),
                chunks_to_extract=chunks_to_extract,
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
        if work_item.existing_chunks:
            all_chunk_snapshots = work_item.existing_chunks
            chunks_to_extract = work_item.chunks_to_extract
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
            all_chunk_snapshots = tuple(
                chunk_snapshot_from_text_chunk(chunk, work_item) for chunk in text_chunks
            )
            chunks_to_extract = all_chunk_snapshots

        extractions = await self._extract_chunk_knowledge(chunks_to_extract)
        extractions_by_chunk_id = {
            chunk.id: extraction
            for chunk, extraction in zip(chunks_to_extract, extractions, strict=True)
        }
        ordered_chunk_summaries = [
            extraction.summary
            if (extraction := extractions_by_chunk_id.get(chunk.id)) is not None
            else chunk_knowledge_summary(chunk)
            for chunk in sorted(all_chunk_snapshots, key=lambda item: (item.ordinal, item.id))
        ]
        document_summary_text = await self._summarize_document(ordered_chunk_summaries)
        document_summary_embedding = await self._embed_document_summary(document_summary_text)
        return CognifyPreparedSource(
            work_item=work_item,
            document_tokens=document_tokens,
            new_chunks=text_chunks,
            embeddings=embeddings,
            extractions=tuple(
                (chunk.id, extraction)
                for chunk, extraction in zip(chunks_to_extract, extractions, strict=True)
            ),
            document_summary_id=document_summary_id(
                work_item.document_id,
                generation=work_item.generation,
            ),
            document_summary_text=document_summary_text,
            document_summary_embedding=document_summary_embedding,
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

    async def _summarize_document(self, chunk_summaries: Sequence[str]) -> str:
        try:
            return await self._document_summary_client.summarize(chunk_summaries)
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Document summary generation failed.") from exc

    async def _embed_document_summary(self, summary_text: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([summary_text])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        self._validate_embeddings(embeddings, expected_count=1)
        return embeddings[0]

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
            document = await uow.documents.get_by_id(prepared.work_item.document_id)
            if source is None or document is None:
                raise _snapshot_persistence_error()

            if prepared.new_chunks:
                if prepared.document_tokens is None:
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
            entity_commands: dict[UUID, ProjectionCommand] = {}
            entity_mention_commands: dict[UUID, ProjectionCommand] = {}
            relation_commands: dict[UUID, ProjectionCommand] = {}
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
                    entity_commands[entity.id] = entity_upsert_command(
                        entity_id=entity.id,
                        dataset_id=entity.dataset_id,
                        name=entity.name,
                        entity_type=entity.entity_type,
                        description=entity.description,
                        importance_weight=float(entity.importance_weight),
                        generation=entity.generation,
                    )
                    if not await uow.entity_mentions.exists_for_entity_chunk(
                        entity_id=entity.id,
                        chunk_id=chunk.id,
                    ):
                        mention = await uow.entity_mentions.add(
                            entity_mention_from_extraction(extracted_entity, entity.id, chunk)
                        )
                        entity_mention_commands[mention.id] = entity_mention_upsert_command(
                            mention_id=mention.id,
                            dataset_id=prepared.work_item.dataset_id,
                            entity_id=mention.entity_id,
                            chunk_id=mention.chunk_id,
                            confidence=float(mention.confidence),
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
                    relation_commands[relation.id] = relation_upsert_command(
                        relation_id=relation.id,
                        dataset_id=relation.dataset_id,
                        source_entity_id=relation.source_entity_id,
                        target_entity_id=relation.target_entity_id,
                        predicate=relation.predicate,
                        description=relation.description,
                        confidence=float(relation.confidence),
                        importance_weight=float(relation.importance_weight),
                        generation=relation.generation,
                    )
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

            summary = await uow.summaries.get_by_id(prepared.document_summary_id)
            if summary is None:
                summary = Summary(
                    id=prepared.document_summary_id,
                    dataset_id=prepared.work_item.dataset_id,
                    generation=prepared.work_item.generation,
                    target_type=SummaryTargetType.DOCUMENT,
                    target_id=prepared.work_item.document_id,
                    level=0,
                    text=prepared.document_summary_text,
                    embedding=prepared.document_summary_embedding,
                    is_active=True,
                )
                await uow.summaries.add(summary)
            else:
                summary.text = prepared.document_summary_text
                summary.embedding = prepared.document_summary_embedding
                summary.is_active = True
            document.metadata_ = document_summary_metadata(
                document.metadata_,
                summary_id=summary.id,
                llm_model=self._settings.llm_model,
                embedding_model=self._settings.embedding_model,
                config_fingerprint=self._settings.config_fingerprint(),
            )

            active_chunks = await uow.chunks.list_for_source_generation(
                source_id=prepared.work_item.source_id,
                generation=prepared.work_item.generation,
            )
            chunk_commands = {
                chunk.id: chunk_upsert_command(
                    chunk_id=chunk.id,
                    dataset_id=chunk.dataset_id,
                    source_id=chunk.source_id,
                    document_id=chunk.document_id,
                    ordinal=chunk.ordinal,
                    generation=chunk.generation,
                )
                for chunk in active_chunks
            }
            next_commands = _chunk_next_projection_commands(active_chunks)
            for command in _ordered_projection_commands(
                entity_commands=tuple(entity_commands.values()),
                chunk_commands=tuple(chunk_commands.values()),
                entity_mention_commands=tuple(entity_mention_commands.values()),
                relation_commands=tuple(relation_commands.values()),
                next_commands=next_commands,
            ):
                await uow.graph_outbox.add_projection_command(command)

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


def document_summary_id(document_id: UUID, *, generation: int) -> UUID:
    return uuid5(
        document_id,
        f"document-summary:{generation}:{DOCUMENT_SUMMARY_PROMPT_VERSION}",
    )


def is_document_summary_complete(document: Document, summary: Summary | None) -> bool:
    marker = document.metadata_.get("document_summary")
    expected_id = document_summary_id(document.id, generation=document.generation)
    return (
        isinstance(marker, dict)
        and marker.get("version") == DOCUMENT_SUMMARY_VERSION
        and marker.get("prompt_version") == DOCUMENT_SUMMARY_PROMPT_VERSION
        and marker.get("summary_id") == str(expected_id)
        and summary is not None
        and summary.id == expected_id
        and summary.dataset_id == document.dataset_id
        and summary.generation == document.generation
        and summary.target_type == SummaryTargetType.DOCUMENT
        and summary.target_id == document.id
        and summary.level == 0
        and summary.is_active
    )


def chunk_knowledge_summary(chunk: CognifyChunkSnapshot) -> str:
    marker = chunk.metadata.get("knowledge_extraction")
    if not isinstance(marker, dict):
        raise _snapshot_persistence_error()
    summary = marker.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _snapshot_persistence_error()
    return summary.strip()


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


def document_summary_metadata(
    existing: dict[str, object],
    *,
    summary_id: UUID,
    llm_model: str,
    embedding_model: str,
    config_fingerprint: str,
) -> dict[str, object]:
    metadata = dict(existing)
    metadata["document_summary"] = {
        "version": DOCUMENT_SUMMARY_VERSION,
        "prompt_version": DOCUMENT_SUMMARY_PROMPT_VERSION,
        "summary_id": str(summary_id),
        "llm_model": llm_model,
        "embedding_model": embedding_model,
        "config_fingerprint": config_fingerprint,
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


def _chunk_next_projection_commands(chunks: Sequence[Chunk]) -> tuple[ProjectionCommand, ...]:
    groups: dict[tuple[UUID, UUID, int], list[Chunk]] = defaultdict(list)
    for chunk in chunks:
        if chunk.is_active:
            groups[(chunk.dataset_id, chunk.document_id, chunk.generation)].append(chunk)

    commands: list[ProjectionCommand] = []
    for group_chunks in groups.values():
        ordered_chunks = sorted(group_chunks, key=lambda chunk: (chunk.ordinal, chunk.id))
        for from_chunk, to_chunk in zip(ordered_chunks, ordered_chunks[1:], strict=False):
            if to_chunk.ordinal == from_chunk.ordinal + 1:
                commands.append(
                    chunk_next_upsert_command(
                        dataset_id=from_chunk.dataset_id,
                        from_chunk_id=from_chunk.id,
                        to_chunk_id=to_chunk.id,
                    )
                )
    return tuple(commands)


def _ordered_projection_commands(
    *,
    entity_commands: Sequence[ProjectionCommand],
    chunk_commands: Sequence[ProjectionCommand],
    entity_mention_commands: Sequence[ProjectionCommand],
    relation_commands: Sequence[ProjectionCommand],
    next_commands: Sequence[ProjectionCommand],
) -> tuple[ProjectionCommand, ...]:
    commands_by_identity: dict[tuple[str, str, str | None], ProjectionCommand] = {}
    for command in (
        *entity_commands,
        *chunk_commands,
        *entity_mention_commands,
        *relation_commands,
        *next_commands,
    ):
        commands_by_identity[_projection_command_identity(command)] = command
    return tuple(commands_by_identity.values())


def _projection_command_identity(command: ProjectionCommand) -> tuple[str, str, str | None]:
    if command.aggregate_type == "chunk_next":
        return (
            command.aggregate_type,
            command.identity["from_chunk_id"],
            command.identity["to_chunk_id"],
        )
    return command.aggregate_type, command.aggregate_id, None


def _snapshot_persistence_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Cognify source snapshot could not be persisted.",
    )
