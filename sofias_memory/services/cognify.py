"""Cognify chunk/embedding/knowledge processing (B4 algorithms, B5 runtime).

The B4 per-source algorithms (chunking, embeddings, knowledge extraction,
document summaries, graph outbox commands, per-source idempotency markers)
are unchanged. What SM-510 removed is this module's own *orchestration*:
there is no ``cognify()`` entry point, no self-created ``PipelineRun``, and
no synchronous run finalization any more. The durable run/step lifecycle
belongs entirely to the B5 engine, and :class:`CognifyService` is now the
business helper the ``cognify.process_sources.v1`` step calls (see
``sofias_memory.pipelines.steps.cognify``).

The service is deliberately split into the two phases ADR-0009 SS O
prescribes for a pipeline step, and never blurs them:

* :meth:`CognifyService.prepare_batch` -- the *external/computation* phase.
  Reads PostgreSQL (never mutates it, never commits), calls the embedding,
  knowledge-extraction and summary providers, and returns a fully-resolved,
  in-memory :class:`CognifyPreparedBatch`: every row that will be written,
  every canonical entity/relation identity, and every per-source outcome,
  already decided.
* :meth:`CognifyService.persist_batch` -- the *PostgreSQL-only* phase. Given
  the engine's already-open transaction, it applies that batch and nothing
  else: no provider call, no HTTP, no Neo4j, and no ``commit()`` of its own.
  The engine commits it together with the step's ``succeeded`` transition,
  so authoritative Cognify state can never be observable while its
  ``PipelineStep`` row is still ``running``.

Every method here is generation-parameterized: a normal cognify targets the
dataset's current ``active_generation``; a ``rebuild=true`` run targets
``active_generation + 1`` and stays invisible to readers until the
``cognify.activate_generation.v1`` step flips the dataset atomically.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import UUID, uuid4, uuid5

from sqlalchemy.exc import SQLAlchemyError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import DEFAULT_PROMPT_VERSIONS, Settings
from sofias_memory.domain import (
    DatasetStatus,
    SourceStatus,
    SummaryTargetType,
)
from sofias_memory.domain.document_reset import (
    RESET_DOCUMENT_METADATA_KEY,
    RESET_DOCUMENT_METADATA_VERSION,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    Relation,
    RelationEvidence,
    Source,
    Summary,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.loaders.text import (
    CSV_FILE_MIME_TYPE,
    DOCX_FILE_MIME_TYPE,
    HTML_FILE_MIME_TYPE,
    JSON_FILE_MIME_TYPE,
    MARKDOWN_FILE_MIME_TYPE,
    PDF_FILE_MIME_TYPE,
    TEXT_FILE_MIME_TYPE,
    PreparedText,
    TextFileLoadError,
    prepare_text_file_content,
)
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
from sofias_memory.schemas.cognify import CognifyRequest
from sofias_memory.schemas.common import ErrorCode, JSONValue
from sofias_memory.schemas.knowledge import (
    ChunkKnowledgeExtraction,
    ExtractedEntity,
    ExtractedRelation,
    canonical_entity_key,
    normalize_relation_predicate,
)

COGNIFY_RESULT_METRIC_KEY = "cognify_result"
"""``PipelineRun.metrics`` key the ``cognify.activate_generation.v1`` step
writes the business result under, in the same transaction as the run's own
``succeeded`` transition -- the single durable source the HTTP route rebuilds
a ``CognifyResult`` from (never in-process memory)."""

REBUILD_DOCUMENT_NAMESPACE = "cognify-rebuild-document"
KNOWLEDGE_EXTRACTION_VERSION = "v1"
GRAPH_EXTRACTION_PROMPT_VERSION = DEFAULT_PROMPT_VERSIONS["graph_extraction"]
DOCUMENT_SUMMARY_VERSION = "v1"
DOCUMENT_SUMMARY_PROMPT_VERSION = DEFAULT_PROMPT_VERSIONS["document_summary"]
UNDETERMINED_LANGUAGE = "und"
SOURCE_EXTENSION_BY_MIME_TYPE = {
    TEXT_FILE_MIME_TYPE: ".txt",
    MARKDOWN_FILE_MIME_TYPE: ".md",
    JSON_FILE_MIME_TYPE: ".json",
    CSV_FILE_MIME_TYPE: ".csv",
    HTML_FILE_MIME_TYPE: ".html",
    PDF_FILE_MIME_TYPE: ".pdf",
    DOCX_FILE_MIME_TYPE: ".docx",
}


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class KnowledgeExtractionClient(Protocol):
    async def extract(self, chunk_text: str) -> ChunkKnowledgeExtraction: ...


class DocumentSummaryClient(Protocol):
    async def summarize(self, chunk_summaries: Sequence[str]) -> str: ...


class DatasetRepositoryForCognify(Protocol):
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...


class SourceRepositoryForCognify(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def list_for_cognify(self, dataset_id: UUID) -> list[Source]: ...
    async def list_for_dataset_not_deleted(self, dataset_id: UUID) -> list[Source]: ...


class DocumentRepositoryForCognify(Protocol):
    async def add(self, document: Document) -> Document: ...
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


class SavepointContext(Protocol):
    async def __aenter__(self) -> object: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None: ...


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

    async def __aenter__(self) -> CognifyUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...
    def savepoint(self) -> SavepointContext: ...


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
    source_name: str
    source_mime_type: str
    source_storage_uri: str | None
    source_content_sha256: str
    source_byte_size: int
    rehydrate_document: bool
    existing_chunks: tuple[CognifyChunkSnapshot, ...]
    chunks_to_extract: tuple[CognifyChunkSnapshot, ...]
    document_to_create: Document | None = None
    """A ``rebuild``'s brand-new target-generation ``Document``, *constructed*
    but deliberately never added to a session during the external phase --
    inserting it is an authoritative mutation and belongs to
    :meth:`CognifyService.persist_batch` alone."""


@dataclass(frozen=True)
class _SourceDecision:
    """Read-only verdict for one candidate source. Carries no side effect: a
    source that only needs its ``status`` corrected to ``ACTIVE`` says so as
    data, so the correction lands in the engine's transaction."""

    work_item: CognifySourceWorkItem | None = None
    activate: bool = False


@dataclass(frozen=True)
class CognifyPreparedSource:
    work_item: CognifySourceWorkItem
    document_tokens: int | None
    refreshed_document: PreparedText | None
    new_chunks: tuple[TextChunk, ...]
    embeddings: tuple[list[float], ...]
    extractions: tuple[tuple[UUID, ChunkKnowledgeExtraction], ...]
    document_summary_id: UUID
    document_summary_text: str
    document_summary_embedding: list[float]


@dataclass
class CognifyPlannedEntity:
    """One canonical entity identity resolved during the external phase.

    Resolved against already-durable PostgreSQL state (a read-only
    ``get_active_by_canonical_key``) *plus* in-memory deduplication across
    every source in the same batch: two sources extracting the same
    ``canonical_key`` in one ``prepare_batch`` resolve to this one plan, never
    to two provisional entities. Mutable on purpose -- aliases and confidence
    accumulate as more chunks mention the same entity.
    """

    canonical_key: str
    entity_id: UUID
    dataset_id: UUID
    generation: int
    name: str
    entity_type: str
    description: str
    aliases: tuple[str, ...]
    confidence: float
    is_new: bool


type RelationIdentity = tuple[str, str, str]
"""``(source_canonical_key, target_canonical_key, normalized_predicate)``."""


@dataclass
class CognifyPlannedRelation:
    """One relation identity resolved during the external phase, deduplicated
    across the whole batch exactly like :class:`CognifyPlannedEntity`."""

    identity: RelationIdentity
    relation_id: UUID
    dataset_id: UUID
    generation: int
    source_key: str
    target_key: str
    predicate: str
    description: str
    confidence: float
    is_new: bool


@dataclass(frozen=True)
class CognifyChunkKnowledgePlan:
    """What one chunk contributes, with every identity already resolved."""

    chunk_id: UUID
    summary: str
    entities: tuple[tuple[str, ExtractedEntity], ...]
    relations: tuple[tuple[RelationIdentity, ExtractedRelation], ...]


@dataclass(frozen=True)
class CognifyPreparedSourcePlan:
    prepared: CognifyPreparedSource
    chunk_plans: tuple[CognifyChunkKnowledgePlan, ...]
    chunks_created: int
    entities_created: int
    relations_created: int


@dataclass(frozen=True)
class CognifyPreparedBatch:
    """Everything one ``process_sources`` execution decided, still in memory.

    Nothing here has touched PostgreSQL authoritatively yet. Losing this
    object (crash, cancellation, a failed ``persist``) loses exactly nothing
    durable -- which is the whole point of moving the writes out of the
    external phase.
    """

    dataset_id: UUID
    target_generation: int
    rebuild: bool
    sources: tuple[CognifyPreparedSourcePlan, ...]
    sources_to_activate: tuple[UUID, ...]
    failed_source_ids: tuple[UUID, ...]
    entity_plans: Mapping[str, CognifyPlannedEntity]
    relation_plans: Mapping[RelationIdentity, CognifyPlannedRelation]

    def planned_outcome(self) -> CognifyProcessOutcome:
        return CognifyProcessOutcome(
            dataset_id=self.dataset_id,
            target_generation=self.target_generation,
            rebuild=self.rebuild,
            sources_processed=len(self.sources),
            chunks=sum(plan.chunks_created for plan in self.sources),
            entities=sum(plan.entities_created for plan in self.sources),
            relations=sum(plan.relations_created for plan in self.sources),
        )


@dataclass
class _KnowledgeResolver:
    """Batch-scoped canonical identity accumulator (see the class docstrings
    above). Lives only for the duration of one ``prepare_batch``."""

    entities: dict[str, CognifyPlannedEntity]
    relations: dict[RelationIdentity, CognifyPlannedRelation]


@dataclass(frozen=True)
class CognifyPersistedCounts:
    chunks: int
    entities: int
    relations: int


@dataclass(frozen=True)
class CognifyProcessOutcome:
    """JSON-safe business result of one ``process_sources`` execution.

    Deliberately counts and identities only -- never document text, chunk
    text, an embedding, a prompt, or a provider payload (ADR-0009 SS 10):
    this is what ends up in ``PipelineStep.output`` and, via the activation
    step, in ``PipelineRun.metrics``.
    """

    dataset_id: UUID
    target_generation: int
    rebuild: bool
    sources_processed: int
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
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._knowledge_extraction_client = knowledge_extraction_client
        self._document_summary_client = document_summary_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )
        self._tokenizer = TextTokenizer(settings.embedding_model)

    # -- external/computation phase -------------------------------------

    async def prepare_batch(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID] | None,
        rebuild: bool,
    ) -> CognifyPreparedBatch:
        """Decide everything one cognify would write, writing nothing.

        Called only by the ``cognify.process_sources.v1`` step's ``execute``
        phase. It reads PostgreSQL through short, read-only units of work
        (never held across a provider call, never committed), calls the
        embedding/extraction/summary providers, and resolves every canonical
        entity and relation identity -- against durable state *and* against
        the rest of this same batch. Nothing here mutates a row, and nothing
        here touches ``PipelineRun``/``PipelineStep`` state: that lifecycle is
        the engine's alone.
        """

        dataset = await self._load_dataset_snapshot(dataset_id)
        target_generation = dataset.active_generation + 1 if rebuild else dataset.active_generation
        selected = await self._select_source_ids(dataset, source_ids, rebuild=rebuild)

        resolver = _KnowledgeResolver(entities={}, relations={})
        prepared_plans: list[CognifyPreparedSourcePlan] = []
        to_activate: list[UUID] = []
        failed: list[UUID] = []

        for source_id in selected:
            decision = await self._decide_source_work_item(
                dataset,
                source_id,
                target_generation=target_generation,
                # A dataset-wide rebuild deliberately reprocesses every
                # non-deleted source, including one left FAILED by an earlier
                # attempt; a normal cognify only retries a FAILED source when
                # it was named explicitly (unchanged B4 rule).
                allow_failed=rebuild or source_ids is not None,
                rebuild=rebuild,
            )
            if decision.activate:
                to_activate.append(source_id)
            work_item = decision.work_item
            if work_item is None:
                continue
            try:
                prepared = await self._prepare_source(work_item)
                plan = await self._resolve_source_knowledge(
                    prepared,
                    resolver=resolver,
                    dataset_id=dataset.id,
                    target_generation=target_generation,
                )
            except DependencyUnavailableError:
                # Provider/storage unavailability is not this source's fault
                # and is not durable evidence of anything: let it propagate so
                # the step is classified retryable and the whole batch is
                # discarded, leaving the source's status untouched for the
                # next attempt. (B4 marked it FAILED here only because it had
                # already committed the surrounding work incrementally.)
                raise
            except SofiasMemoryError:
                # Permanent and attributable to this source's own content:
                # record it as data so persist() can mark exactly this source
                # FAILED, and keep going with the rest of the batch.
                failed.append(work_item.source_id)
                continue
            prepared_plans.append(plan)

        return CognifyPreparedBatch(
            dataset_id=dataset.id,
            target_generation=target_generation,
            rebuild=rebuild,
            sources=tuple(prepared_plans),
            sources_to_activate=tuple(to_activate),
            failed_source_ids=tuple(failed),
            entity_plans=dict(resolver.entities),
            relation_plans=dict(resolver.relations),
        )

    async def _load_dataset_snapshot(self, dataset_id: UUID) -> CognifyDatasetSnapshot:
        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_id(dataset_id)
            if dataset is None or dataset.status != DatasetStatus.ACTIVE:
                raise dataset_not_found_error(str(dataset_id))
            return CognifyDatasetSnapshot(
                id=dataset.id,
                slug=dataset.slug,
                active_generation=dataset.active_generation,
            )

    async def _select_source_ids(
        self,
        dataset: CognifyDatasetSnapshot,
        source_ids: list[UUID] | None,
        *,
        rebuild: bool,
    ) -> list[UUID]:
        async with self._unit_of_work_factory() as uow:
            if source_ids is not None:
                sources = await self._load_requested_sources(
                    uow, dataset=dataset, source_ids=source_ids
                )
            elif rebuild:
                # Dataset-wide rebuild: every source that still carries
                # authoritative content, not only the pending ones. Already
                # ``deleted`` sources are excluded -- they have nothing left
                # to reprocess into the new generation.
                sources = await uow.sources.list_for_dataset_not_deleted(dataset.id)
            else:
                sources = await uow.sources.list_for_cognify(dataset.id)
            selected: list[UUID] = []
            for source in sources:
                if source.status in {
                    SourceStatus.ACTIVE,
                    SourceStatus.PENDING,
                    SourceStatus.FAILED,
                }:
                    selected.append(source.id)
            return selected

    async def _decide_source_work_item(
        self,
        dataset: CognifyDatasetSnapshot,
        source_id: UUID,
        *,
        target_generation: int,
        allow_failed: bool,
        rebuild: bool = False,
    ) -> _SourceDecision:
        """B4's ``_claim_source_work_item`` logic, with every write removed.

        Same selection/idempotency rules; the two mutations it used to commit
        here (``PROCESSING`` while in flight, and ``ACTIVE`` for an
        already-complete source) become plain data on the returned decision.
        ``PROCESSING`` disappears entirely: with the batch committed once, it
        could never be observed by anyone anyway.
        """

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
                generation=target_generation,
            )
            document, document_to_create = await self._resolve_target_generation_document(
                uow,
                dataset=dataset,
                source=source,
                target_generation=target_generation,
                rebuild=rebuild,
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
                return _SourceDecision(activate=source.status != SourceStatus.ACTIVE)
            chunks_to_extract = tuple(
                chunk_snapshot(chunk) for chunk in chunks if not is_chunk_knowledge_extracted(chunk)
            )
            if source.status == SourceStatus.FAILED and not allow_failed:
                return _SourceDecision()
            if source.status not in {
                SourceStatus.PENDING,
                SourceStatus.ACTIVE,
                SourceStatus.FAILED,
            }:
                return _SourceDecision()
            if not chunks and source.status == SourceStatus.ACTIVE and not rebuild:
                # B4 guard against re-chunking an active source whose chunks
                # were deliberately forgotten. A rebuild always starts from
                # zero chunks in the new generation, so it must bypass it.
                return _SourceDecision()
            return _SourceDecision(
                work_item=CognifySourceWorkItem(
                    dataset_id=dataset.id,
                    source_id=source.id,
                    document_id=document.id,
                    generation=target_generation,
                    normalized_text=document.normalized_text if not chunks else None,
                    source_name=source.name,
                    source_mime_type=source.mime_type,
                    source_storage_uri=source.storage_uri,
                    source_content_sha256=source.content_sha256,
                    source_byte_size=source.byte_size,
                    rehydrate_document=is_reset_document(document),
                    existing_chunks=tuple(chunk_snapshot(chunk) for chunk in chunks),
                    chunks_to_extract=chunks_to_extract,
                    document_to_create=document_to_create,
                )
            )

    async def _resolve_target_generation_document(
        self,
        uow: CognifyUnitOfWork,
        *,
        dataset: CognifyDatasetSnapshot,
        source: Source,
        target_generation: int,
        rebuild: bool,
    ) -> tuple[Document | None, Document | None]:
        """The active ``Document`` this source must be processed into, plus
        the one that must still be *inserted* for it (``None`` when it already
        exists durably).

        For a normal cognify this is simply the existing active document of
        the dataset's current generation. A rebuild targets a generation that
        has no document yet, so one is *constructed* by copying the current
        generation's normalized text under a deterministic id derived from
        ``(source_id, target_generation)`` -- re-running the same rebuild
        finds that row instead of building a second one, and the source bytes
        are never re-read or re-normalized (that is Remember's job, not
        Cognify's).

        Deliberately never ``add``-ed to a session here: constructing an ORM
        instance is inert, inserting it is an authoritative mutation that
        belongs to :meth:`CognifyService.persist_batch`.
        """

        document = await uow.documents.get_for_source_generation(
            source_id=source.id,
            generation=target_generation,
        )
        if document is not None or not rebuild:
            return document, None

        base = await uow.documents.get_for_source_generation(
            source_id=source.id,
            generation=dataset.active_generation,
        )
        if base is None:
            return None, None
        # A content-free "reset" document must stay reset in the new
        # generation so cognify rehydrates it from stored source bytes
        # exactly as it would in the current generation.
        metadata: dict[str, object] = (
            {RESET_DOCUMENT_METADATA_KEY: base.metadata_[RESET_DOCUMENT_METADATA_KEY]}
            if is_reset_document(base)
            else {}
        )
        planned = Document(
            id=rebuild_document_id(source.id, generation=target_generation),
            dataset_id=dataset.id,
            source_id=source.id,
            generation=target_generation,
            title=base.title,
            language=base.language,
            normalized_text=base.normalized_text,
            text_sha256=base.text_sha256,
            token_count=base.token_count,
            metadata_=metadata,
            is_active=True,
        )
        return planned, planned

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
        if work_item.rehydrate_document and work_item.existing_chunks:
            raise DependencyUnavailableError("Source reset state is inconsistent.")
        if work_item.existing_chunks:
            all_chunk_snapshots = work_item.existing_chunks
            chunks_to_extract = work_item.chunks_to_extract
            document_tokens = None
            refreshed_document = None
            text_chunks: tuple[TextChunk, ...] = ()
            embeddings: tuple[list[float], ...] = ()
        else:
            refreshed_document = (
                await asyncio.to_thread(self._read_reset_document, work_item)
                if work_item.rehydrate_document
                else None
            )
            normalized_text = (
                refreshed_document.normalized_text
                if refreshed_document is not None
                else work_item.normalized_text
            )
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
            refreshed_document=refreshed_document,
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

    async def _resolve_source_knowledge(
        self,
        prepared: CognifyPreparedSource,
        *,
        resolver: _KnowledgeResolver,
        dataset_id: UUID,
        target_generation: int,
    ) -> CognifyPreparedSourcePlan:
        """Turn one source's raw extractions into resolved identities.

        Read-only: it only ever ``SELECT``s (canonical key, relation
        identity), and it deduplicates within the batch through ``resolver``
        so two sources extracting the same entity in one execution share one
        identity instead of racing to create two.

        This is also where the business counters come from -- they must be
        known before ``execute`` returns, because the engine writes
        ``StepResult.output`` durably.
        """

        chunk_plans: list[CognifyChunkKnowledgePlan] = []
        entities_created = 0
        relations_created = 0

        async with self._unit_of_work_factory() as uow:
            for chunk_id, extraction in prepared.extractions:
                keys_by_local_id: dict[str, str] = {}
                entity_refs: list[tuple[str, ExtractedEntity]] = []
                for extracted in extraction.entities:
                    key = canonical_entity_key(extracted.entity_type, extracted.name)
                    plan = resolver.entities.get(key)
                    if plan is None:
                        plan, created = await self._plan_entity(
                            uow,
                            extracted,
                            canonical_key=key,
                            dataset_id=dataset_id,
                            target_generation=target_generation,
                        )
                        entities_created += int(created)
                        resolver.entities[key] = plan
                    plan.aliases = tuple(dict.fromkeys((*plan.aliases, *extracted.aliases)))
                    plan.confidence = max(plan.confidence, extracted.confidence)
                    keys_by_local_id[extracted.local_id] = key
                    entity_refs.append((key, extracted))

                relation_refs: list[tuple[RelationIdentity, ExtractedRelation]] = []
                for extracted_relation in extraction.relations:
                    try:
                        source_key = keys_by_local_id[extracted_relation.source_local_id]
                        target_key = keys_by_local_id[extracted_relation.target_local_id]
                    except KeyError as exc:
                        # The model referenced an entity it never declared:
                        # permanent and specific to this source's extraction.
                        raise _snapshot_persistence_error() from exc
                    predicate = normalize_relation_predicate(extracted_relation.predicate)
                    identity: RelationIdentity = (source_key, target_key, predicate)
                    plan_relation = resolver.relations.get(identity)
                    if plan_relation is None:
                        plan_relation, created = await self._plan_relation(
                            uow,
                            extracted_relation,
                            identity=identity,
                            resolver=resolver,
                            dataset_id=dataset_id,
                            target_generation=target_generation,
                        )
                        relations_created += int(created)
                        resolver.relations[identity] = plan_relation
                    plan_relation.confidence = max(
                        plan_relation.confidence, extracted_relation.confidence
                    )
                    if not plan_relation.description:
                        plan_relation.description = extracted_relation.description
                    relation_refs.append((identity, extracted_relation))

                chunk_plans.append(
                    CognifyChunkKnowledgePlan(
                        chunk_id=chunk_id,
                        summary=extraction.summary,
                        entities=tuple(entity_refs),
                        relations=tuple(relation_refs),
                    )
                )

        return CognifyPreparedSourcePlan(
            prepared=prepared,
            chunk_plans=tuple(chunk_plans),
            chunks_created=len(prepared.new_chunks),
            entities_created=entities_created,
            relations_created=relations_created,
        )

    async def _plan_entity(
        self,
        uow: CognifyUnitOfWork,
        extracted: ExtractedEntity,
        *,
        canonical_key: str,
        dataset_id: UUID,
        target_generation: int,
    ) -> tuple[CognifyPlannedEntity, bool]:
        existing = await uow.entities.get_active_by_canonical_key(
            dataset_id=dataset_id,
            canonical_key=canonical_key,
        )
        if existing is not None:
            return (
                CognifyPlannedEntity(
                    canonical_key=canonical_key,
                    entity_id=existing.id,
                    dataset_id=dataset_id,
                    generation=target_generation,
                    name=existing.name,
                    entity_type=existing.entity_type,
                    description=existing.description,
                    aliases=tuple(existing.aliases),
                    confidence=float(existing.confidence),
                    is_new=False,
                ),
                False,
            )
        return (
            CognifyPlannedEntity(
                canonical_key=canonical_key,
                entity_id=uuid4(),
                dataset_id=dataset_id,
                generation=target_generation,
                name=extracted.name,
                entity_type=extracted.entity_type,
                description=extracted.description,
                aliases=(),
                confidence=extracted.confidence,
                is_new=True,
            ),
            True,
        )

    async def _plan_relation(
        self,
        uow: CognifyUnitOfWork,
        extracted: ExtractedRelation,
        *,
        identity: RelationIdentity,
        resolver: _KnowledgeResolver,
        dataset_id: UUID,
        target_generation: int,
    ) -> tuple[CognifyPlannedRelation, bool]:
        source_key, target_key, predicate = identity
        source_plan = resolver.entities[source_key]
        target_plan = resolver.entities[target_key]
        existing = None
        if not source_plan.is_new and not target_plan.is_new:
            # Only durable endpoints can have a durable relation between
            # them; a brand-new endpoint makes the lookup pointless.
            existing = await uow.relations.get_active_by_identity(
                source_entity_id=source_plan.entity_id,
                target_entity_id=target_plan.entity_id,
                predicate=predicate,
                generation=target_generation,
            )
        if existing is not None:
            return (
                CognifyPlannedRelation(
                    identity=identity,
                    relation_id=existing.id,
                    dataset_id=dataset_id,
                    generation=target_generation,
                    source_key=source_key,
                    target_key=target_key,
                    predicate=predicate,
                    description=existing.description,
                    confidence=float(existing.confidence),
                    is_new=False,
                ),
                False,
            )
        return (
            CognifyPlannedRelation(
                identity=identity,
                relation_id=uuid4(),
                dataset_id=dataset_id,
                generation=target_generation,
                source_key=source_key,
                target_key=target_key,
                predicate=predicate,
                description=extracted.description,
                confidence=extracted.confidence,
                is_new=True,
            ),
            True,
        )

    # -- PostgreSQL-only phase ------------------------------------------

    async def persist_batch(
        self,
        uow: CognifyUnitOfWork,
        batch: CognifyPreparedBatch,
    ) -> CognifyProcessOutcome:
        """Apply a prepared batch inside the engine's own transaction.

        PostgreSQL-only by construction: no embedding, LLM, HTTP, Neo4j or
        filesystem call is reachable from here, and it never calls
        ``commit()`` -- the engine commits this together with the step's own
        ``succeeded`` transition, which is what makes a partially-written
        cognify unobservable.

        Per-source isolation is a real SAVEPOINT: a failure attributable to
        one source rolls that source's rows back, marks *only* that source
        ``FAILED``, and lets the rest of the batch land. Anything outside the
        two classes below (a genuine bug, an invariant violation) propagates
        and fails the whole step, exactly as the engine's fail-safe
        classification expects.
        """

        totals = CognifyPersistedCounts(chunks=0, entities=0, relations=0)
        sources_processed = 0
        entity_ids: dict[str, UUID] = {}

        for plan in batch.sources:
            source_id = plan.prepared.work_item.source_id
            try:
                async with uow.savepoint():
                    persisted = await self._persist_source_plan(
                        uow,
                        plan,
                        batch=batch,
                        entity_ids=entity_ids,
                    )
            except (SofiasMemoryError, SQLAlchemyError):
                await self._mark_source_failed(uow, source_id)
                continue
            totals = CognifyPersistedCounts(
                chunks=totals.chunks + persisted.chunks,
                entities=totals.entities + persisted.entities,
                relations=totals.relations + persisted.relations,
            )
            sources_processed += 1

        for source_id in batch.sources_to_activate:
            source = await uow.sources.get_by_id(source_id)
            if source is not None:
                source.status = SourceStatus.ACTIVE
        for source_id in batch.failed_source_ids:
            await self._mark_source_failed(uow, source_id)

        return CognifyProcessOutcome(
            dataset_id=batch.dataset_id,
            target_generation=batch.target_generation,
            rebuild=batch.rebuild,
            sources_processed=sources_processed,
            chunks=totals.chunks,
            entities=totals.entities,
            relations=totals.relations,
        )

    async def _persist_source_plan(
        self,
        uow: CognifyUnitOfWork,
        plan: CognifyPreparedSourcePlan,
        *,
        batch: CognifyPreparedBatch,
        entity_ids: dict[str, UUID],
    ) -> CognifyPersistedCounts:
        """B4's ``_persist_prepared_source`` writes, verbatim in behavior, now
        applied to the engine's transaction instead of one of this service's
        own -- and with entity/relation *identity* already decided by
        ``prepare_batch`` instead of being resolved mid-write."""

        prepared = plan.prepared
        work_item = prepared.work_item
        source = await uow.sources.get_by_id(work_item.source_id)
        if source is None:
            raise _snapshot_persistence_error()
        document: Document | None
        if work_item.document_to_create is not None:
            # A rebuild's target-generation Document: constructed during the
            # external phase, inserted only here.
            document = await uow.documents.add(work_item.document_to_create)
        else:
            document = await uow.documents.get_by_id(work_item.document_id)
        if document is None:
            raise _snapshot_persistence_error()

        if prepared.refreshed_document is not None:
            document.title = work_item.source_name
            document.language = UNDETERMINED_LANGUAGE
            document.normalized_text = prepared.refreshed_document.normalized_text
            document.text_sha256 = prepared.refreshed_document.normalized_sha256
            document.metadata_ = {}
        if prepared.new_chunks:
            if prepared.document_tokens is None:
                raise _snapshot_persistence_error()
            chunks = [
                chunk_model_from_text_chunk(
                    text_chunk,
                    dataset_id=work_item.dataset_id,
                    document_id=work_item.document_id,
                    source_id=work_item.source_id,
                    generation=work_item.generation,
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
        for chunk_plan in plan.chunk_plans:
            chunk = await uow.chunks.get_by_id(chunk_plan.chunk_id)
            if chunk is None:
                raise _snapshot_persistence_error()
            chunk_entities: dict[str, Entity] = {}
            for canonical_key, extracted_entity in chunk_plan.entities:
                entity, created = await self._apply_entity_plan(
                    uow,
                    batch.entity_plans[canonical_key],
                    entity_ids=entity_ids,
                )
                entities_created += int(created)
                chunk_entities[canonical_key] = entity
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
                        dataset_id=work_item.dataset_id,
                        entity_id=mention.entity_id,
                        chunk_id=mention.chunk_id,
                        confidence=float(mention.confidence),
                    )

            for identity, extracted_relation in chunk_plan.relations:
                relation_plan = batch.relation_plans[identity]
                relation, created = await self._apply_relation_plan(
                    uow,
                    relation_plan,
                    source_entity=chunk_entities[relation_plan.source_key],
                    target_entity=chunk_entities[relation_plan.target_key],
                )
                relations_created += int(created)
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
                chunk_plan.summary,
                llm_model=self._settings.llm_model,
                config_fingerprint=self._settings.config_fingerprint(),
            )

        summary = await uow.summaries.get_by_id(prepared.document_summary_id)
        if summary is None:
            summary = Summary(
                id=prepared.document_summary_id,
                dataset_id=work_item.dataset_id,
                generation=work_item.generation,
                target_type=SummaryTargetType.DOCUMENT,
                target_id=work_item.document_id,
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
            source_id=work_item.source_id,
            generation=work_item.generation,
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
        return CognifyPersistedCounts(
            chunks=chunks_created,
            entities=entities_created,
            relations=relations_created,
        )

    async def _apply_entity_plan(
        self,
        uow: CognifyUnitOfWork,
        plan: CognifyPlannedEntity,
        *,
        entity_ids: dict[str, UUID],
    ) -> tuple[Entity, bool]:
        """Materialize one planned canonical entity idempotently.

        Re-checks the canonical key against the transaction's own state
        rather than trusting ``plan.is_new``: within this single transaction
        the key may already have been created for an earlier source, and a
        source rolled back to its SAVEPOINT must not leave a later source
        pointing at an entity that no longer exists.
        """

        entity = await uow.entities.get_active_by_canonical_key(
            dataset_id=plan.dataset_id,
            canonical_key=plan.canonical_key,
        )
        if entity is not None:
            entity.aliases = list(dict.fromkeys((*entity.aliases, *plan.aliases)))
            entity.confidence = max(float(entity.confidence), plan.confidence)
            entity_ids[plan.canonical_key] = entity.id
            return entity, False
        entity = Entity(
            id=plan.entity_id,
            dataset_id=plan.dataset_id,
            generation=plan.generation,
            canonical_key=plan.canonical_key,
            name=plan.name,
            entity_type=plan.entity_type,
            description=plan.description,
            aliases=list(plan.aliases),
            properties={},
            confidence=plan.confidence,
            importance_weight=1.0,
            embedding=None,
            is_active=True,
        )
        await uow.entities.add(entity)
        entity_ids[plan.canonical_key] = entity.id
        return entity, True

    async def _apply_relation_plan(
        self,
        uow: CognifyUnitOfWork,
        plan: CognifyPlannedRelation,
        *,
        source_entity: Entity,
        target_entity: Entity,
    ) -> tuple[Relation, bool]:
        relation = await uow.relations.get_active_by_identity(
            source_entity_id=source_entity.id,
            target_entity_id=target_entity.id,
            predicate=plan.predicate,
            generation=plan.generation,
        )
        if relation is not None:
            relation.confidence = max(float(relation.confidence), plan.confidence)
            if not relation.description:
                relation.description = plan.description
            return relation, False
        relation = Relation(
            id=plan.relation_id,
            dataset_id=plan.dataset_id,
            generation=plan.generation,
            source_entity_id=source_entity.id,
            target_entity_id=target_entity.id,
            predicate=plan.predicate,
            description=plan.description,
            properties={},
            confidence=plan.confidence,
            importance_weight=1.0,
            embedding=None,
            is_active=True,
        )
        await uow.relations.add(relation)
        return relation, True

    def _read_reset_document(self, work_item: CognifySourceWorkItem) -> PreparedText:
        """Reload and validate source bytes for a content-free reset document."""

        storage_path = source_storage_path_for_cognify(
            self._settings.data_directory,
            dataset_id=work_item.dataset_id,
            source_id=work_item.source_id,
            storage_uri=work_item.source_storage_uri,
        )
        max_bytes = self._settings.max_source_size_mb * 1024 * 1024
        try:
            actual_size = storage_path.stat().st_size
        except OSError as exc:
            raise DependencyUnavailableError("Source storage is unavailable.") from exc
        if actual_size > max_bytes or actual_size != work_item.source_byte_size:
            raise DependencyUnavailableError("Source storage is unavailable.")
        try:
            original_bytes = storage_path.read_bytes()
        except OSError as exc:
            raise DependencyUnavailableError("Source storage is unavailable.") from exc
        if len(original_bytes) != work_item.source_byte_size:
            raise DependencyUnavailableError("Source storage is unavailable.")
        if sha256(original_bytes).hexdigest() != work_item.source_content_sha256:
            raise DependencyUnavailableError("Source storage is unavailable.")
        extension = SOURCE_EXTENSION_BY_MIME_TYPE.get(work_item.source_mime_type)
        if extension is None:
            raise DependencyUnavailableError("Source storage is unavailable.")
        try:
            return prepare_text_file_content(f"source{extension}", original_bytes).text
        except TextFileLoadError as exc:
            raise DependencyUnavailableError("Source storage is unavailable.") from exc

    async def _mark_source_failed(self, uow: CognifyUnitOfWork, source_id: UUID) -> None:
        """PostgreSQL-only, inside the engine's transaction (never its own)."""

        source = await uow.sources.get_by_id(source_id)
        if source is not None:
            source.status = SourceStatus.FAILED


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> CognifyUnitOfWork:
        return cast(CognifyUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work


def cognify_run_input(request: CognifyRequest) -> dict[str, JSONValue]:
    """The canonical Cognify work identity (ADR-0009 SS S).

    ``wait`` is deliberately absent: it is a response-shape preference, not
    part of what work is being requested, so ``wait=true`` and ``wait=false``
    for otherwise-identical input resolve to the very same run under the same
    ``Idempotency-Key``.
    """

    return {
        "dataset": request.dataset,
        "source_ids": [str(source_id) for source_id in request.source_ids]
        if request.source_ids is not None
        else None,
        "rebuild": request.rebuild,
    }


def cognify_source_ids_from_run_input(run_input: Mapping[str, object]) -> list[UUID] | None:
    """Rehydrate ``source_ids`` from a persisted ``PipelineRun.input``.

    Raises a permanent, typed error rather than silently dropping an
    unparsable value: a run whose durable input cannot be interpreted must
    fail loudly, never be reinterpreted as "no source_ids" (which would
    silently widen the requested scope).
    """

    raw = run_input.get("source_ids")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _invalid_run_input_error("source_ids")
    try:
        return [UUID(str(value)) for value in raw]
    except ValueError as exc:
        raise _invalid_run_input_error("source_ids") from exc


def rebuild_document_id(source_id: UUID, *, generation: int) -> UUID:
    """Deterministic id of the document a rebuild materializes for a source."""

    return uuid5(source_id, f"{REBUILD_DOCUMENT_NAMESPACE}:{generation}")


def dataset_not_found_error(dataset: str) -> SofiasMemoryError:
    """The single not-found shape Cognify uses for an unresolvable dataset,
    identical to the B4 response so the public contract is unchanged."""

    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Dataset does not exist.",
        details={"dataset": dataset},
    )


def _invalid_run_input_error(field: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Cognify run input is not interpretable.",
        details={"field": field},
    )


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


def is_reset_document(document: Document) -> bool:
    marker = document.metadata_.get(RESET_DOCUMENT_METADATA_KEY)
    return (
        isinstance(marker, Mapping)
        and marker.get("version") == RESET_DOCUMENT_METADATA_VERSION
        and document.normalized_text == ""
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


def source_storage_path_for_cognify(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_uri: str | None,
) -> Path:
    """Resolve a stored source file without accepting paths outside its source directory."""

    if storage_uri is None:
        raise DependencyUnavailableError("Source storage is unavailable.")
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise DependencyUnavailableError("Source storage is unavailable.")
    storage_root = data_directory.resolve(strict=False)
    expected_directory = (storage_root / str(dataset_id) / str(source_id)).resolve(strict=False)
    raw_path = Path(url2pathname(parsed.path))
    nominal_path = raw_path.resolve(strict=False)
    if (
        not expected_directory.is_relative_to(storage_root)
        or not nominal_path.is_relative_to(expected_directory)
        or not raw_path.exists()
    ):
        raise DependencyUnavailableError("Source storage is unavailable.")
    try:
        resolved_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise DependencyUnavailableError("Source storage is unavailable.") from exc
    if (
        not resolved_path.is_relative_to(expected_directory)
        or not resolved_path.is_file()
        or resolved_path.is_dir()
    ):
        raise DependencyUnavailableError("Source storage is unavailable.")
    return resolved_path


def _snapshot_persistence_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Cognify source snapshot could not be persisted.",
    )
