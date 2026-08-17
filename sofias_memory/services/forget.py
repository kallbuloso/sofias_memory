"""Synchronous source forget service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.domain.document_reset import (
    RESET_DOCUMENT_LANGUAGE,
    RESET_DOCUMENT_METADATA_KEY,
    RESET_DOCUMENT_METADATA_VERSION,
    RESET_DOCUMENT_TEXT_SHA256,
    RESET_DOCUMENT_TITLE,
    RESET_DOCUMENT_TOKEN_COUNT,
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
from sofias_memory.ports import (
    ProjectionCommand,
    chunk_delete_command,
    chunk_next_delete_command,
    entity_delete_command,
    entity_mention_delete_command,
    relation_delete_command,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.forget import ForgetRequest, ForgetResult
from sofias_memory.services.remember import stable_payload_hash

FORGET_RESULT_METRIC_KEY = "forget_result"
FORGET_STEP = "forget_source"


class DatasetRepositoryForForget(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class SourceRepositoryForForget(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def get_by_id_for_update(self, source_id: UUID) -> Source | None: ...


class DocumentRepositoryForForget(Protocol):
    async def add(self, document: Document) -> Document: ...

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Document]: ...


class ChunkRepositoryForForget(Protocol):
    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]: ...


class EntityMentionRepositoryForForget(Protocol):
    async def list_for_chunks(self, *, chunk_ids: list[UUID]) -> list[EntityMention]: ...

    async def list_entity_ids_with_authoritative_mentions(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> set[UUID]: ...


class RelationEvidenceRepositoryForForget(Protocol):
    async def list_for_chunks(self, *, chunk_ids: list[UUID]) -> list[RelationEvidence]: ...

    async def list_relation_ids_with_authoritative_evidence(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> set[UUID]: ...


class EntityRepositoryForForget(Protocol):
    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> list[Entity]: ...


class RelationRepositoryForForget(Protocol):
    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> list[Relation]: ...

    async def list_active_current_incident_entity_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> set[UUID]: ...


class SummaryRepositoryForForget(Protocol):
    async def list_active_for_forget(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        document_ids: list[UUID],
        entity_ids: list[UUID],
        include_dataset_summary: bool,
    ) -> list[Summary]: ...


class GraphOutboxRepositoryForForget(Protocol):
    async def add_projection_command(self, command: ProjectionCommand) -> object: ...


class PipelineRunRepositoryForForget(Protocol):
    async def add(self, run: PipelineRun) -> PipelineRun: ...
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...

    async def has_running_forget_for_source_except(
        self,
        *,
        source_id: UUID,
        excluded_run_id: UUID,
    ) -> bool: ...


class ForgetUnitOfWork(Protocol):
    datasets: DatasetRepositoryForForget
    sources: SourceRepositoryForForget
    documents: DocumentRepositoryForForget
    chunks: ChunkRepositoryForForget
    entity_mentions: EntityMentionRepositoryForForget
    relation_evidence: RelationEvidenceRepositoryForForget
    entities: EntityRepositoryForForget
    relations: RelationRepositoryForForget
    summaries: SummaryRepositoryForForget
    graph_outbox: GraphOutboxRepositoryForForget
    pipeline_runs: PipelineRunRepositoryForForget

    async def __aenter__(self) -> ForgetUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def flush(self) -> None: ...
    async def commit(self) -> None: ...


class GraphProjectionDrain(Protocol):
    async def process_dataset(self, dataset_id: UUID) -> object: ...


type UnitOfWorkFactory = Callable[[], ForgetUnitOfWork]


@dataclass(frozen=True)
class ForgetMutation:
    dataset_id: UUID
    generation: int
    source_id: UUID
    source_status: SourceStatus
    storage_uri: str | None
    documents_deactivated: int
    chunks_deactivated: int
    summaries_deactivated: int
    entities_deactivated: int
    relations_deactivated: int
    entity_mentions_unprojected: int
    relation_evidence_unprojected: int
    graph_events_enqueued: int
    reentrant_in_progress: bool = False


class StorageDeleteStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    DELETED_NOW = "deleted_now"
    ALREADY_ABSENT = "already_absent"


@dataclass(frozen=True)
class StorageDeleteResult:
    status: StorageDeleteStatus

    @property
    def completed(self) -> bool:
        return self.status in {
            StorageDeleteStatus.DELETED_NOW,
            StorageDeleteStatus.ALREADY_ABSENT,
        }


class ForgetService:
    """Forget one source or only its derived memory using PostgreSQL as authority."""

    def __init__(
        self,
        settings: Settings,
        *,
        graph_projection_drain: GraphProjectionDrain,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._graph_projection_drain = graph_projection_drain
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def forget_source(self, request: ForgetRequest) -> ForgetResult:
        self._validate_request(request)
        await self._validate_target(request)
        run_id = await self._create_running_run(request)
        try:
            mutation = await self._apply_authoritative_forget(run_id, request)
            if mutation.reentrant_in_progress:
                return await self._complete_reentrant_run(run_id, mutation, request)
            drain_result = await self._graph_projection_drain.process_dataset(mutation.dataset_id)
            graph_events_processed = int(getattr(drain_result, "processed", 0))
            storage_delete = StorageDeleteResult(StorageDeleteStatus.NOT_REQUESTED)
            if not request.memory_only:
                storage_delete = delete_source_storage(
                    self._settings.data_directory,
                    dataset_id=mutation.dataset_id,
                    source_id=mutation.source_id,
                    storage_uri=mutation.storage_uri,
                )
            final_status = SourceStatus.PENDING if request.memory_only else SourceStatus.DELETED
            result = await self._finalize_source(
                run_id,
                mutation,
                request=request,
                final_status=final_status,
                storage_deleted=storage_delete.completed,
                graph_events_processed=graph_events_processed,
            )
            return result
        except Exception as exc:
            await self._mark_run_failed(run_id, exc)
            raise

    def _validate_request(self, request: ForgetRequest) -> None:
        if request.wait is not True:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Only wait=true is supported in this checkpoint.",
                details={"wait": request.wait},
            )

    async def _validate_target(self, request: ForgetRequest) -> None:
        """Confirm dataset/source exist before a PipelineRun references source_id.

        ``pipeline_runs.source_id`` has a NOT NULL foreign key to ``sources.id``, so
        creating the run first for a source_id that was never persisted would raise
        an unhandled IntegrityError instead of the documented 404. This is a plain
        read outside any lock; it does not replace the authoritative, locked
        revalidation performed under ``SourceRepository.get_by_id_for_update()``
        inside ``_apply_authoritative_forget``.
        """

        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset(uow, request.dataset)
            await self._require_source_in_dataset(uow, dataset, request.source_id)

    async def _create_running_run(self, request: ForgetRequest) -> UUID:
        now = utc_now()
        run_id = uuid4()
        run_input = forget_run_input(request)
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.FORGET,
            dataset_id=None,
            source_id=request.source_id,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=stable_payload_hash(run_input),
            input=run_input,
            progress=0.0,
            current_step=FORGET_STEP,
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

    async def _apply_authoritative_forget(
        self,
        run_id: UUID,
        request: ForgetRequest,
    ) -> ForgetMutation:
        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset(uow, request.dataset)
            source = await self._require_source_in_dataset_for_update(
                uow,
                dataset,
                request.source_id,
            )
            if source.status == SourceStatus.DELETED:
                mutation = empty_mutation(dataset=dataset, source=source)
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation
            if source.status == SourceStatus.DELETING:
                reentrant_in_progress = (
                    await uow.pipeline_runs.has_running_forget_for_source_except(
                        source_id=source.id,
                        excluded_run_id=run_id,
                    )
                )
                mutation = empty_mutation(
                    dataset=dataset,
                    source=source,
                    reentrant_in_progress=reentrant_in_progress,
                )
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation
            if source.status == SourceStatus.PENDING and request.memory_only:
                mutation = empty_mutation(dataset=dataset, source=source)
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation

            source.status = SourceStatus.DELETING
            documents = await uow.documents.list_for_source_generation(
                source_id=source.id,
                generation=dataset.active_generation,
                active_only=True,
            )
            chunks = await uow.chunks.list_for_source_generation(
                source_id=source.id,
                generation=dataset.active_generation,
                active_only=True,
            )
            mentions = await uow.entity_mentions.list_for_chunks(
                chunk_ids=[chunk.id for chunk in chunks]
            )
            evidence = await uow.relation_evidence.list_for_chunks(
                chunk_ids=[chunk.id for chunk in chunks]
            )
            for document in documents:
                document.is_active = False
            for chunk in chunks:
                chunk.is_active = False

            # The session deliberately has autoflush disabled. The authoritative
            # evidence and mention queries below must observe this forget mutation.
            await uow.flush()

            relation_candidates = await uow.relations.list_active_current_by_ids(
                dataset_id=dataset.id,
                relation_ids=sorted({item.relation_id for item in evidence}),
            )
            valid_relation_ids = (
                await uow.relation_evidence.list_relation_ids_with_authoritative_evidence(
                    dataset_id=dataset.id,
                    relation_ids=[relation.id for relation in relation_candidates],
                )
            )
            deactivated_relations = [
                relation
                for relation in relation_candidates
                if relation.id not in valid_relation_ids
            ]
            for relation in deactivated_relations:
                relation.is_active = False

            # Entity orphan detection must not count relations just deactivated above.
            await uow.flush()

            entity_candidates = await uow.entities.list_active_current_by_ids(
                dataset_id=dataset.id,
                entity_ids=sorted({mention.entity_id for mention in mentions}),
            )
            entity_ids = [entity.id for entity in entity_candidates]
            valid_entity_mentions = (
                await uow.entity_mentions.list_entity_ids_with_authoritative_mentions(
                    dataset_id=dataset.id,
                    entity_ids=entity_ids,
                )
            )
            incident_entity_ids = await uow.relations.list_active_current_incident_entity_ids(
                dataset_id=dataset.id,
                entity_ids=entity_ids,
            )
            deactivated_entities = [
                entity
                for entity in entity_candidates
                if entity.id not in valid_entity_mentions and entity.id not in incident_entity_ids
            ]
            for entity in deactivated_entities:
                entity.is_active = False

            summaries = await uow.summaries.list_active_for_forget(
                dataset_id=dataset.id,
                generation=dataset.active_generation,
                document_ids=[document.id for document in documents],
                entity_ids=[entity.id for entity in deactivated_entities],
                include_dataset_summary=bool(documents or chunks),
            )
            for summary in summaries:
                summary.is_active = False

            if request.memory_only and documents:
                await uow.documents.add(reset_document_for_recognify(documents[-1]))

            commands = forget_projection_commands(
                dataset_id=dataset.id,
                chunks=chunks,
                mentions=mentions,
                relations=deactivated_relations,
                entities=deactivated_entities,
            )
            for command in commands:
                await uow.graph_outbox.add_projection_command(command)

            mutation = ForgetMutation(
                dataset_id=dataset.id,
                generation=dataset.active_generation,
                source_id=source.id,
                source_status=SourceStatus.DELETING,
                storage_uri=source.storage_uri,
                documents_deactivated=len(documents),
                chunks_deactivated=len(chunks),
                summaries_deactivated=len(summaries),
                entities_deactivated=len(deactivated_entities),
                relations_deactivated=len(deactivated_relations),
                entity_mentions_unprojected=len(mentions),
                relation_evidence_unprojected=len(evidence),
                graph_events_enqueued=len(commands),
            )
            await self._store_partial_metrics(uow, run_id, mutation, request)
            await uow.commit()
            return mutation

    async def _require_active_dataset(self, uow: ForgetUnitOfWork, dataset_slug: str) -> Dataset:
        dataset = await uow.datasets.get_by_slug(dataset_slug)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.NOT_FOUND,
                message="Dataset does not exist.",
                details={"dataset": dataset_slug},
            )
        return dataset

    async def _require_source_in_dataset(
        self,
        uow: ForgetUnitOfWork,
        dataset: Dataset,
        source_id: UUID,
    ) -> Source:
        source = await uow.sources.get_by_id(source_id)
        if source is None or source.dataset_id != dataset.id:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.NOT_FOUND,
                message="Source does not exist.",
                details={"source_id": str(source_id)},
            )
        return source

    async def _require_source_in_dataset_for_update(
        self,
        uow: ForgetUnitOfWork,
        dataset: Dataset,
        source_id: UUID,
    ) -> Source:
        source = await uow.sources.get_by_id_for_update(source_id)
        if source is None or source.dataset_id != dataset.id:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.NOT_FOUND,
                message="Source does not exist.",
                details={"source_id": str(source_id)},
            )
        return source

    async def _store_partial_metrics(
        self,
        uow: ForgetUnitOfWork,
        run_id: UUID,
        mutation: ForgetMutation,
        request: ForgetRequest,
    ) -> None:
        run = await uow.pipeline_runs.get_by_id(run_id)
        if run is None:
            return
        run.dataset_id = mutation.dataset_id
        run.source_id = mutation.source_id
        run.metrics = {
            FORGET_RESULT_METRIC_KEY: forget_result_from_counts(
                run_id=run_id,
                mutation=mutation,
                request=request,
                source_status=mutation.source_status,
                graph_events_processed=0,
                storage_deleted=False,
            ).model_dump(mode="json")
        }

    async def _finalize_source(
        self,
        run_id: UUID,
        mutation: ForgetMutation,
        *,
        request: ForgetRequest,
        final_status: SourceStatus,
        storage_deleted: bool,
        graph_events_processed: int,
    ) -> ForgetResult:
        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(mutation.source_id)
            if source is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INTERNAL_ERROR,
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    message="Source could not be loaded for forget finalization.",
                )
            if source.status == SourceStatus.DELETING:
                source.status = final_status
            if (
                final_status == SourceStatus.DELETED
                and storage_deleted
                and source.status == SourceStatus.DELETED
            ):
                source.storage_uri = None
            result = forget_result_from_counts(
                run_id=run_id,
                mutation=mutation,
                request=request,
                source_status=source.status,
                graph_events_processed=graph_events_processed,
                storage_deleted=storage_deleted,
            )
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is not None:
                run.dataset_id = mutation.dataset_id
                run.source_id = mutation.source_id
                run.status = PipelineRunStatus.SUCCEEDED
                run.progress = 1.0
                run.current_step = None
                run.error_code = None
                run.error_message = None
                run.metrics = {FORGET_RESULT_METRIC_KEY: result.model_dump(mode="json")}
                run.finished_at = utc_now()
            await uow.commit()
            return result

    async def _complete_reentrant_run(
        self,
        run_id: UUID,
        mutation: ForgetMutation,
        request: ForgetRequest,
    ) -> ForgetResult:
        """Complete a duplicate request without competing for post-commit work."""

        async with self._unit_of_work_factory() as uow:
            source = await uow.sources.get_by_id(mutation.source_id)
            if source is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INTERNAL_ERROR,
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    message="Source could not be loaded for forget completion.",
                )
            result = forget_result_from_counts(
                run_id=run_id,
                mutation=mutation,
                request=request,
                source_status=source.status,
                graph_events_processed=0,
                storage_deleted=False,
            )
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is not None:
                run.dataset_id = mutation.dataset_id
                run.source_id = mutation.source_id
                run.status = PipelineRunStatus.SUCCEEDED
                run.progress = 1.0
                run.current_step = None
                run.error_code = None
                run.error_message = None
                run.metrics = {FORGET_RESULT_METRIC_KEY: result.model_dump(mode="json")}
                run.finished_at = utc_now()
            await uow.commit()
            return result

    async def _mark_run_failed(self, run_id: UUID, exc: Exception) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                return
            run.status = PipelineRunStatus.FAILED
            run.progress = 1.0
            run.current_step = None
            run.error_code = type(exc).__name__
            run.error_message = "Forget failed."
            run.finished_at = utc_now()
            await uow.commit()


def reset_document_for_recognify(document: Document) -> Document:
    """Create a content-free document placeholder for a future cognify run."""

    return Document(
        id=uuid4(),
        dataset_id=document.dataset_id,
        source_id=document.source_id,
        generation=document.generation,
        title=RESET_DOCUMENT_TITLE,
        language=RESET_DOCUMENT_LANGUAGE,
        normalized_text="",
        text_sha256=RESET_DOCUMENT_TEXT_SHA256,
        token_count=RESET_DOCUMENT_TOKEN_COUNT,
        metadata_={
            RESET_DOCUMENT_METADATA_KEY: {
                "version": RESET_DOCUMENT_METADATA_VERSION,
            }
        },
        is_active=True,
    )


def forget_projection_commands(
    *,
    dataset_id: UUID,
    chunks: list[Chunk],
    mentions: list[EntityMention],
    relations: list[Relation],
    entities: list[Entity],
) -> list[ProjectionCommand]:
    commands: dict[tuple[str, str, str | None], ProjectionCommand] = {}

    for chunk in chunks:
        _add_command(
            commands,
            chunk_delete_command(chunk_id=chunk.id, dataset_id=dataset_id),
        )
    for command in chunk_next_delete_commands(dataset_id=dataset_id, chunks=chunks):
        _add_command(commands, command)
    for mention in sorted(mentions, key=lambda item: item.id):
        _add_command(
            commands,
            entity_mention_delete_command(
                mention_id=mention.id,
                dataset_id=dataset_id,
                entity_id=mention.entity_id,
                chunk_id=mention.chunk_id,
            ),
        )
    for relation in sorted(relations, key=lambda item: item.id):
        _add_command(
            commands,
            relation_delete_command(
                relation_id=relation.id,
                dataset_id=dataset_id,
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
            ),
        )
    for entity in sorted(entities, key=lambda item: item.id):
        _add_command(
            commands,
            entity_delete_command(entity_id=entity.id, dataset_id=dataset_id),
        )

    return list(commands.values())


def chunk_next_delete_commands(*, dataset_id: UUID, chunks: list[Chunk]) -> list[ProjectionCommand]:
    chunks_by_document: dict[UUID, list[Chunk]] = {}
    for chunk in chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)

    commands: list[ProjectionCommand] = []
    for document_chunks in chunks_by_document.values():
        ordered = sorted(document_chunks, key=lambda item: (item.ordinal, item.id))
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.ordinal != left.ordinal + 1:
                continue
            commands.append(
                chunk_next_delete_command(
                    dataset_id=dataset_id,
                    from_chunk_id=left.id,
                    to_chunk_id=right.id,
                )
            )
    return commands


def _add_command(
    commands: dict[tuple[str, str, str | None], ProjectionCommand],
    command: ProjectionCommand,
) -> None:
    key: tuple[str, str, str | None]
    if command.aggregate_type == "chunk_next":
        key = (
            command.aggregate_type,
            command.identity["from_chunk_id"],
            command.identity["to_chunk_id"],
        )
    else:
        key = (command.aggregate_type, command.aggregate_id, None)
    commands.setdefault(key, command)


def delete_source_storage(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_uri: str | None,
) -> StorageDeleteResult:
    if storage_uri is None:
        return StorageDeleteResult(StorageDeleteStatus.NOT_REQUESTED)
    target_path = source_storage_path(
        data_directory,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=storage_uri,
    )
    if target_path is None:
        return StorageDeleteResult(StorageDeleteStatus.ALREADY_ABSENT)
    try:
        target_path.unlink()
    except OSError as exc:
        raise DependencyUnavailableError(message="Source storage could not be deleted.") from exc
    return StorageDeleteResult(StorageDeleteStatus.DELETED_NOW)


def source_storage_path(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_uri: str,
) -> Path | None:
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise invalid_storage_uri_error()

    storage_root = data_directory.resolve(strict=False)
    expected_directory = (storage_root / str(dataset_id) / str(source_id)).resolve(strict=False)
    raw_path = Path(url2pathname(parsed.path))
    nominal_path = raw_path.resolve(strict=False)
    if not expected_directory.is_relative_to(storage_root):
        raise invalid_storage_uri_error()
    if not nominal_path.is_relative_to(expected_directory):
        raise invalid_storage_uri_error()
    if not raw_path.exists():
        return None

    resolved_path = raw_path.resolve(strict=True)
    if not resolved_path.is_relative_to(expected_directory):
        raise invalid_storage_uri_error()
    if not resolved_path.is_file() or resolved_path.is_dir():
        raise invalid_storage_uri_error()
    return resolved_path


def invalid_storage_uri_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.BAD_REQUEST,
        message="Source storage location is invalid.",
    )


def forget_run_input(request: ForgetRequest) -> dict[str, JSONValue]:
    return {
        "dataset": request.dataset,
        "source_id": str(request.source_id),
        "memory_only": request.memory_only,
        "wait": request.wait,
    }


def empty_mutation(
    *,
    dataset: Dataset,
    source: Source,
    reentrant_in_progress: bool = False,
) -> ForgetMutation:
    return ForgetMutation(
        dataset_id=dataset.id,
        generation=dataset.active_generation,
        source_id=source.id,
        source_status=source.status,
        storage_uri=source.storage_uri,
        documents_deactivated=0,
        chunks_deactivated=0,
        summaries_deactivated=0,
        entities_deactivated=0,
        relations_deactivated=0,
        entity_mentions_unprojected=0,
        relation_evidence_unprojected=0,
        graph_events_enqueued=0,
        reentrant_in_progress=reentrant_in_progress,
    )


def forget_result_from_counts(
    *,
    run_id: UUID,
    mutation: ForgetMutation,
    request: ForgetRequest,
    source_status: SourceStatus,
    graph_events_processed: int,
    storage_deleted: bool,
) -> ForgetResult:
    return ForgetResult(
        run_id=run_id,
        status=PipelineRunStatus.SUCCEEDED.value,
        dataset_id=mutation.dataset_id,
        source_id=mutation.source_id,
        memory_only=request.memory_only,
        source_status=source_status,
        documents_deactivated=mutation.documents_deactivated,
        chunks_deactivated=mutation.chunks_deactivated,
        summaries_deactivated=mutation.summaries_deactivated,
        entities_deactivated=mutation.entities_deactivated,
        relations_deactivated=mutation.relations_deactivated,
        entity_mentions_unprojected=mutation.entity_mentions_unprojected,
        relation_evidence_unprojected=mutation.relation_evidence_unprojected,
        graph_events_enqueued=mutation.graph_events_enqueued,
        graph_events_processed=graph_events_processed,
        storage_deleted=storage_deleted,
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> ForgetUnitOfWork:
        return cast(ForgetUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
