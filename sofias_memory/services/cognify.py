"""Synchronous cognify service for chunking and embeddings."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    PipelineRun,
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
from sofias_memory.services.remember import stable_payload_hash

COGNIFY_RUN_STEP = "chunk_embeddings"
COGNIFY_RESULT_METRIC_KEY = "cognify_result"
ZERO_DERIVED_ENTITY_COUNT = 0
ZERO_DERIVED_RELATION_COUNT = 0


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class DatasetRepositoryForCognify(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class SourceRepositoryForCognify(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def list_pending_for_dataset(self, dataset_id: UUID) -> list[Source]: ...


class DocumentRepositoryForCognify(Protocol):
    async def get_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
    ) -> Document | None: ...


class ChunkRepositoryForCognify(Protocol):
    async def add_many(self, chunks: Sequence[Chunk]) -> list[Chunk]: ...

    async def exists_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> bool: ...


class PipelineRunRepositoryForCognify(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...


class CognifyUnitOfWork(Protocol):
    datasets: DatasetRepositoryForCognify
    sources: SourceRepositoryForCognify
    documents: DocumentRepositoryForCognify
    chunks: ChunkRepositoryForCognify
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
class CognifySourceSnapshot:
    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    generation: int
    normalized_text: str


@dataclass(frozen=True)
class CognifyPreparedSource:
    snapshot: CognifySourceSnapshot
    document_tokens: int
    chunks: list[TextChunk]
    embeddings: list[list[float]]


class CognifyService:
    """Chunk and embed pending remembered documents."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )
        self._tokenizer = TextTokenizer(settings.embedding_model)

    async def cognify(self, request: CognifyRequest) -> CognifyResult:
        self._validate_supported_request(request)
        run_id = await self._create_running_run(request)
        try:
            result = await self._cognify(run_id, request)
        except Exception as exc:
            await self._mark_run_failed(run_id, exc)
            raise
        return result

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
            current_step=COGNIFY_RUN_STEP,
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
        chunks_processed = 0
        sources_processed = 0

        for source_id in source_ids:
            snapshot = await self._claim_source_snapshot(dataset, source_id)
            if snapshot is None:
                continue
            try:
                prepared = await self._prepare_source(snapshot)
                await self._persist_prepared_source(prepared)
            except Exception:
                await self._mark_source_failed(snapshot.source_id)
                raise
            chunks_processed += len(prepared.chunks)
            sources_processed += 1

        result = CognifyResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED.value,
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            sources_processed=sources_processed,
            chunks=chunks_processed,
            entities=ZERO_DERIVED_ENTITY_COUNT,
            relations=ZERO_DERIVED_RELATION_COUNT,
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
                else await uow.sources.list_pending_for_dataset(dataset.id)
            )
            selected: list[UUID] = []
            for source in sources:
                if await uow.chunks.exists_for_source_generation(
                    source_id=source.id,
                    generation=dataset.active_generation,
                    active_only=True,
                ):
                    continue
                if source.status != SourceStatus.PENDING:
                    continue
                selected.append(source.id)
            return selected

    async def _claim_source_snapshot(
        self,
        dataset: CognifyDatasetSnapshot,
        source_id: UUID,
    ) -> CognifySourceSnapshot | None:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(source_id)
            if source is None or source.dataset_id != dataset.id:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Source does not exist.",
                    details={"source_id": str(source_id)},
                )
            if await uow.chunks.exists_for_source_generation(
                source_id=source.id,
                generation=dataset.active_generation,
                active_only=True,
            ):
                source.status = SourceStatus.ACTIVE
                await uow.commit()
                return None
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
                    message="Source has no active normalized document for the dataset generation.",
                    details={"source_id": str(source.id)},
                )
            source.status = SourceStatus.PROCESSING
            snapshot = CognifySourceSnapshot(
                dataset_id=dataset.id,
                source_id=source.id,
                document_id=document.id,
                generation=dataset.active_generation,
                normalized_text=document.normalized_text,
            )
            await uow.commit()
            return snapshot

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

    async def _prepare_source(self, snapshot: CognifySourceSnapshot) -> CognifyPreparedSource:
        document_tokens = document_token_count(snapshot.normalized_text, self._tokenizer)
        text_chunks = chunk_document_text(
            snapshot.normalized_text,
            document_id=snapshot.document_id,
            generation=snapshot.generation,
            tokenizer=self._tokenizer,
            max_tokens=self._settings.chunk_max_tokens,
            overlap_tokens=self._settings.chunk_overlap_tokens,
            min_tokens=self._settings.chunk_min_tokens,
        )
        try:
            embeddings = await self._embedding_client.embed_texts(
                [chunk.text for chunk in text_chunks]
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        self._validate_embeddings(embeddings, expected_count=len(text_chunks))
        return CognifyPreparedSource(
            snapshot=snapshot,
            document_tokens=document_tokens,
            chunks=text_chunks,
            embeddings=embeddings,
        )

    def _validate_embeddings(self, embeddings: list[list[float]], *, expected_count: int) -> None:
        if len(embeddings) != expected_count:
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected number of vectors."
            )
        for embedding in embeddings:
            if len(embedding) != self._settings.embedding_dimensions:
                raise DependencyUnavailableError(
                    "Embedding provider returned an unexpected vector dimension."
                )

    async def _persist_prepared_source(self, prepared: CognifyPreparedSource) -> None:
        async with self._unit_of_work_factory() as uow:
            document = await uow.documents.get_for_source_generation(
                source_id=prepared.snapshot.source_id,
                generation=prepared.snapshot.generation,
            )
            source = await uow.sources.get_by_id(prepared.snapshot.source_id)
            if document is None or source is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INTERNAL_ERROR,
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    message="Cognify source snapshot could not be persisted.",
                )
            if await uow.chunks.exists_for_source_generation(
                source_id=prepared.snapshot.source_id,
                generation=prepared.snapshot.generation,
                active_only=True,
            ):
                source.status = SourceStatus.ACTIVE
                document.token_count = prepared.document_tokens
                await uow.commit()
                return

            chunks = [
                chunk_model_from_text_chunk(
                    text_chunk,
                    dataset_id=prepared.snapshot.dataset_id,
                    document_id=prepared.snapshot.document_id,
                    source_id=prepared.snapshot.source_id,
                    generation=prepared.snapshot.generation,
                    embedding=embedding,
                    embedding_model=self._settings.embedding_model,
                    config_fingerprint=self._settings.config_fingerprint(),
                )
                for text_chunk, embedding in zip(prepared.chunks, prepared.embeddings, strict=True)
            ]
            await uow.chunks.add_many(chunks)
            document.token_count = prepared.document_tokens
            source.status = SourceStatus.ACTIVE
            await uow.commit()

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
