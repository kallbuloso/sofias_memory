"""Synchronous remember text ingest service."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    PipelineRunStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres.models import Dataset, Document, PipelineRun, Source
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.loaders.text import PreparedText, PreparedTextFile, prepare_text_content
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.remember import RememberTextRequest, RememberTextResult

DEFAULT_DATASET_SLUG = "main"
INGEST_MODE = "ingest"
REMEMBER_RESULT_METRIC_KEY = "remember_result"
TEXT_MIME_TYPE = "text/plain"
TEXT_STORAGE_EXTENSION = ".txt"
UNTOKENIZED_SENTINEL = -1
UNDETERMINED_LANGUAGE = "und"


class DatasetRepositoryForRemember(Protocol):
    async def add(self, dataset: Dataset) -> Dataset: ...
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class SourceRepositoryForRemember(Protocol):
    async def add(self, source: Source) -> Source: ...

    async def get_latest_by_content_hash(
        self,
        *,
        dataset_id: UUID,
        content_sha256: str,
    ) -> Source | None: ...


class DocumentRepositoryForRemember(Protocol):
    async def add(self, document: Document) -> Document: ...
    async def list_for_source(self, source_id: UUID) -> list[Document]: ...


class PipelineRunRepositoryForRemember(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...
    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None: ...


class RememberUnitOfWork(Protocol):
    datasets: DatasetRepositoryForRemember
    sources: SourceRepositoryForRemember
    documents: DocumentRepositoryForRemember
    pipeline_runs: PipelineRunRepositoryForRemember

    async def __aenter__(self) -> RememberUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], RememberUnitOfWork]


@dataclass(frozen=True)
class RememberPreparedInput:
    dataset: str
    name: str | None
    metadata: dict[str, JSONValue]
    session_id: str | None
    mode: str
    wait: bool
    force: bool
    source_kind: SourceKind
    mime_type: str
    storage_extension: str
    prepared_text: PreparedText
    run_input: dict[str, JSONValue]


class RememberService:
    """Text-only remember ingest adapted from Cognee's add/ingest core."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def remember_text(
        self,
        request: RememberTextRequest,
        *,
        idempotency_key: str | None = None,
    ) -> RememberTextResult:
        prepared_text = prepare_text_content(request.content)
        prepared_input = RememberPreparedInput(
            dataset=request.dataset,
            name=request.name,
            metadata=dict(request.metadata),
            session_id=request.session_id,
            mode=request.mode,
            wait=request.wait,
            force=request.force,
            source_kind=SourceKind.TEXT,
            mime_type=TEXT_MIME_TYPE,
            storage_extension=TEXT_STORAGE_EXTENSION,
            prepared_text=prepared_text,
            run_input=remember_text_run_input(
                request,
                content_sha256=prepared_text.content_sha256,
            ),
        )
        return await self._remember_prepared(prepared_input, idempotency_key=idempotency_key)

    async def remember_file(
        self,
        *,
        dataset: str,
        prepared_file: PreparedTextFile,
        metadata: dict[str, JSONValue],
        session_id: str | None,
        mode: str,
        wait: bool,
        force: bool,
        idempotency_key: str | None = None,
    ) -> RememberTextResult:
        prepared_input = RememberPreparedInput(
            dataset=dataset,
            name=prepared_file.original_filename,
            metadata=dict(metadata),
            session_id=session_id,
            mode=mode,
            wait=wait,
            force=force,
            source_kind=SourceKind.FILE,
            mime_type=prepared_file.mime_type,
            storage_extension=prepared_file.storage_extension,
            prepared_text=prepared_file.text,
            run_input=remember_file_run_input(
                dataset=dataset,
                content_sha256=prepared_file.text.content_sha256,
                filename=prepared_file.original_filename,
                metadata=metadata,
                session_id=session_id,
                mode=mode,
                wait=wait,
                force=force,
                mime_type=prepared_file.mime_type,
            ),
        )
        return await self._remember_prepared(prepared_input, idempotency_key=idempotency_key)

    async def _remember_prepared(
        self,
        prepared_input: RememberPreparedInput,
        *,
        idempotency_key: str | None,
    ) -> RememberTextResult:
        self._validate_supported_mode(prepared_input.mode, prepared_input.wait)
        payload_hash = stable_payload_hash(prepared_input.run_input)

        existing_result = await self._idempotent_existing_result(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if existing_result is not None:
            return existing_result

        run = await self._create_running_run(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            run_input=prepared_input.run_input,
        )
        try:
            return await self._ingest_prepared(run.id, prepared_input)
        except Exception as exc:
            await self._mark_run_failed(run.id, exc)
            raise

    def _validate_supported_mode(self, mode: str, wait: bool) -> None:
        if mode != INGEST_MODE:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Only remember mode=ingest is supported in this checkpoint.",
                details={"mode": mode},
            )
        if wait is not True:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Only wait=true is supported in this checkpoint.",
                details={"wait": wait},
            )

    async def _idempotent_existing_result(
        self,
        *,
        idempotency_key: str | None,
        payload_hash: str,
    ) -> RememberTextResult | None:
        if idempotency_key is None:
            return None

        async with self._unit_of_work_factory() as uow:
            existing = await uow.pipeline_runs.get_by_idempotency_key(idempotency_key)
            if existing is None:
                return None
            if existing.payload_hash != payload_hash:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.CONFLICT,
                    message="Idempotency key was already used with a different payload.",
                )
            if existing.status != PipelineRunStatus.SUCCEEDED:
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.CONFLICT,
                    message="Idempotency key is already associated with an unfinished run.",
                )
            return result_from_run(existing)

    async def _create_running_run(
        self,
        *,
        idempotency_key: str | None,
        payload_hash: str,
        run_input: dict[str, JSONValue],
    ) -> PipelineRun:
        now = utc_now()
        run = PipelineRun(
            id=uuid4(),
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=None,
            source_id=None,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            input=dict(run_input),
            progress=0.0,
            current_step="ingest",
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
        return run

    async def _ingest_prepared(
        self,
        run_id: UUID,
        prepared_input: RememberPreparedInput,
    ) -> RememberTextResult:
        async with self._unit_of_work_factory() as uow:
            run = await _require_run(uow, run_id)
            dataset = await self._resolve_dataset(uow, prepared_input.dataset)
            source = await uow.sources.get_latest_by_content_hash(
                dataset_id=dataset.id,
                content_sha256=prepared_input.prepared_text.content_sha256,
            )
            deduplicated = source is not None and not prepared_input.force

            if deduplicated:
                document = await _existing_document_for_source(uow, cast(Source, source))
            else:
                source = await self._create_source(
                    uow,
                    dataset=dataset,
                    prepared_input=prepared_input,
                    previous_source=source,
                )
                document = await self._create_document(
                    uow,
                    dataset=dataset,
                    source=source,
                    prepared_input=prepared_input,
                )

            result = RememberTextResult(
                run_id=run.id,
                status=PipelineRunStatus.SUCCEEDED.value,
                dataset_id=dataset.id,
                source_id=cast(Source, source).id,
                document_id=document.id,
                content_hash=prepared_input.prepared_text.content_sha256,
                chunks=0,
                entities=0,
                relations=0,
                deduplicated=deduplicated,
            )
            _mark_run_succeeded(
                run,
                result,
                dataset_id=dataset.id,
                source_id=cast(Source, source).id,
            )
            await uow.commit()
            return result

    async def _resolve_dataset(
        self,
        uow: RememberUnitOfWork,
        dataset_slug: str,
    ) -> Dataset:
        dataset = await uow.datasets.get_by_slug(dataset_slug)
        if dataset is not None:
            return dataset
        if dataset_slug != DEFAULT_DATASET_SLUG:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.NOT_FOUND,
                message="Dataset does not exist.",
                details={"dataset": dataset_slug},
            )

        dataset = Dataset(
            id=uuid4(),
            name=DEFAULT_DATASET_SLUG,
            slug=DEFAULT_DATASET_SLUG,
            description=None,
            status=DatasetStatus.ACTIVE,
            active_generation=0,
        )
        return await uow.datasets.add(dataset)

    async def _create_source(
        self,
        uow: RememberUnitOfWork,
        *,
        dataset: Dataset,
        prepared_input: RememberPreparedInput,
        previous_source: Source | None,
    ) -> Source:
        source_id = uuid4()
        storage_uri = write_original_bytes(
            self._settings.data_directory,
            dataset_id=dataset.id,
            source_id=source_id,
            storage_extension=prepared_input.storage_extension,
            original_bytes=prepared_input.prepared_text.original_bytes,
        )
        source = Source(
            id=source_id,
            dataset_id=dataset.id,
            kind=prepared_input.source_kind,
            name=source_name(prepared_input),
            mime_type=prepared_input.mime_type,
            original_uri=None,
            storage_uri=storage_uri,
            content_sha256=prepared_input.prepared_text.content_sha256,
            normalized_sha256=prepared_input.prepared_text.normalized_sha256,
            byte_size=prepared_input.prepared_text.byte_size,
            metadata_=dict(prepared_input.metadata),
            status=SourceStatus.PENDING,
            version=1 if previous_source is None else previous_source.version + 1,
        )
        return await uow.sources.add(source)

    async def _create_document(
        self,
        uow: RememberUnitOfWork,
        *,
        dataset: Dataset,
        source: Source,
        prepared_input: RememberPreparedInput,
    ) -> Document:
        document = Document(
            id=uuid4(),
            dataset_id=dataset.id,
            source_id=source.id,
            generation=dataset.active_generation,
            title=source_name(prepared_input),
            language=UNDETERMINED_LANGUAGE,
            normalized_text=prepared_input.prepared_text.normalized_text,
            text_sha256=prepared_input.prepared_text.normalized_sha256,
            token_count=UNTOKENIZED_SENTINEL,
            metadata_=document_metadata(prepared_input),
            is_active=True,
        )
        return await uow.documents.add(document)

    async def _mark_run_failed(self, run_id: UUID, exc: Exception) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            now = utc_now()
            run.status = PipelineRunStatus.FAILED
            run.progress = 1.0
            run.current_step = None
            run.finished_at = now
            run.error_code = type(exc).__name__
            run.error_message = "Remember ingest failed."
            await uow.commit()


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> RememberUnitOfWork:
        return cast(RememberUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work


def remember_text_run_input(
    request: RememberTextRequest,
    *,
    content_sha256: str,
) -> dict[str, JSONValue]:
    return {
        "dataset": request.dataset,
        "content_sha256": content_sha256,
        "name": request.name,
        "metadata": request.metadata,
        "session_id": request.session_id,
        "mode": request.mode,
        "force": request.force,
    }


def remember_file_run_input(
    *,
    dataset: str,
    content_sha256: str,
    filename: str,
    metadata: dict[str, JSONValue],
    session_id: str | None,
    mode: str,
    wait: bool,
    force: bool,
    mime_type: str,
) -> dict[str, JSONValue]:
    return {
        "dataset": dataset,
        "content_sha256": content_sha256,
        "filename": filename,
        "metadata": metadata,
        "session_id": session_id,
        "mode": mode,
        "wait": wait,
        "force": force,
        "source_kind": SourceKind.FILE.value,
        "mime_type": mime_type,
    }


def stable_payload_hash(payload: Mapping[str, JSONValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def write_original_bytes(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_extension: str,
    original_bytes: bytes,
) -> str:
    storage_root = data_directory.resolve()
    target_directory = (storage_root / str(dataset_id) / str(source_id)).resolve()
    if not target_directory.is_relative_to(storage_root):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Source storage path is invalid.",
        )
    target_directory.mkdir(parents=True, exist_ok=True)
    target_path = target_directory / f"original{storage_extension}"
    temporary_path = target_directory / f"original{storage_extension}.tmp"
    temporary_path.write_bytes(original_bytes)
    temporary_path.replace(target_path)
    return target_path.resolve().as_uri()


def write_original_text(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    original_bytes: bytes,
) -> str:
    return write_original_bytes(
        data_directory,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=TEXT_STORAGE_EXTENSION,
        original_bytes=original_bytes,
    )


def source_name(prepared_input: RememberPreparedInput) -> str:
    return prepared_input.name or f"text-{prepared_input.prepared_text.content_sha256[:12]}"


def document_metadata(prepared_input: RememberPreparedInput) -> dict[str, JSONValue]:
    metadata = dict(prepared_input.metadata)
    if prepared_input.session_id is not None:
        metadata["session_id"] = prepared_input.session_id
    return metadata


def result_from_run(run: PipelineRun) -> RememberTextResult:
    result = run.metrics.get(REMEMBER_RESULT_METRIC_KEY)
    if not isinstance(result, Mapping):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Stored idempotent remember result is unavailable.",
        )
    return RememberTextResult.model_validate(result)


async def _require_run(uow: RememberUnitOfWork, run_id: UUID) -> PipelineRun:
    run = await uow.pipeline_runs.get_by_id(run_id)
    if run is None:
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Remember run could not be loaded.",
        )
    return run


async def _existing_document_for_source(
    uow: RememberUnitOfWork,
    source: Source,
) -> Document:
    documents = await uow.documents.list_for_source(source.id)
    if not documents:
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Stored source has no normalized document.",
        )
    return documents[-1]


def _mark_run_succeeded(
    run: PipelineRun,
    result: RememberTextResult,
    *,
    dataset_id: UUID,
    source_id: UUID,
) -> None:
    run.dataset_id = dataset_id
    run.source_id = source_id
    run.status = PipelineRunStatus.SUCCEEDED
    run.progress = 1.0
    run.current_step = None
    run.error_code = None
    run.error_message = None
    run.finished_at = utc_now()
    run.metrics = {REMEMBER_RESULT_METRIC_KEY: result.model_dump(mode="json")}
