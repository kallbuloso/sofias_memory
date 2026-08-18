"""Synchronous forget service for source, dataset, and everything scopes."""

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
from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import (
    FORGET_TARGET_CONFLICT_ERROR_CODE,
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
from sofias_memory.schemas.forget import (
    ForgetDatasetResult,
    ForgetEverythingResult,
    ForgetRequest,
    ForgetResponseData,
    ForgetResult,
)
from sofias_memory.services.remember import stable_payload_hash

FORGET_RESULT_METRIC_KEY = "forget_result"
FORGET_DATASET_RESULT_METRIC_KEY = "forget_dataset_result"
FORGET_EVERYTHING_RESULT_METRIC_KEY = "forget_everything_result"
FORGET_STEP = "forget_source"
FORGET_DATASET_STEP = "forget_dataset"
FORGET_EVERYTHING_STEP = "forget_everything"
EVERYTHING_CONFIRM_PHRASE = "DELETE EVERYTHING"


class ForgetScope(StrEnum):
    """The three FR-090 forget scopes, derived from the request, not sent by it."""

    SOURCE = "source"
    DATASET = "dataset"
    EVERYTHING = "everything"


class _AlreadyFinalizedForgetConflict(SofiasMemoryError):
    """A conflict whose owning PipelineRun was already finalized as failed.

    Raised only after the run's ``status``/``error_code`` have been committed
    with :data:`FORGET_TARGET_CONFLICT_ERROR_CODE`, so the generic
    ``except Exception: _mark_run_failed(...)`` handler must not overwrite
    that marker with the default ``type(exc).__name__`` — doing so would make
    this rejected, never-mutated attempt look like a real prior intent and
    incorrectly block a later, correctly-intentioned retry.
    """


class DatasetRepositoryForForget(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...
    async def get_by_slug_for_update(self, slug: str) -> Dataset | None: ...
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...
    async def get_by_id_for_update(self, dataset_id: UUID) -> Dataset | None: ...
    async def list_ids_for_everything_forget(self) -> list[UUID]: ...


class SourceRepositoryForForget(Protocol):
    async def get_by_id(self, source_id: UUID) -> Source | None: ...
    async def get_by_id_for_update(self, source_id: UUID) -> Source | None: ...
    async def list_for_dataset_not_deleted(self, dataset_id: UUID) -> list[Source]: ...
    async def list_for_dataset_for_update(self, dataset_id: UUID) -> list[Source]: ...


class DocumentRepositoryForForget(Protocol):
    async def add(self, document: Document) -> Document: ...

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Document]: ...

    async def list_active_current_for_dataset(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> list[Document]: ...


class ChunkRepositoryForForget(Protocol):
    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]: ...

    async def list_active_current_for_dataset(
        self,
        *,
        dataset_id: UUID,
        generation: int,
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

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Entity]: ...


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

    async def list_active_current_for_dataset(self, *, dataset_id: UUID) -> list[Relation]: ...


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

    async def find_latest_forget_for_source_except(
        self,
        *,
        source_id: UUID,
        excluded_run_id: UUID,
    ) -> PipelineRun | None: ...

    async def find_running_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None: ...

    async def find_latest_forget_for_dataset_except(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID],
        excluded_run_id: UUID,
    ) -> PipelineRun | None: ...


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


class DatasetAttemptOutcome(StrEnum):
    """Result of one locked attempt to progress a dataset-scoped forget target."""

    MUTATED = "mutated"
    RESUMED = "resumed"
    REENTRANT = "reentrant"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DatasetMutationPart:
    """Authoritative counts from one dataset's content mutation.

    All-zero when the attempt did not itself mutate content — either because
    a compatible attempt owns the target (``REENTRANT``/``RESUMED``) or
    because it was rejected (``BLOCKED``); see :class:`DatasetAttempt`.
    """

    dataset_id: UUID
    generation: int
    sources_touched: int
    documents_deactivated: int
    chunks_deactivated: int
    summaries_deactivated: int
    entities_deactivated: int
    relations_deactivated: int
    entity_mentions_unprojected: int
    relation_evidence_unprojected: int
    graph_events_enqueued: int


@dataclass(frozen=True)
class DatasetAttempt:
    """Outcome of one locked attempt to progress a dataset/everything target.

    ``MUTATED``: dataset was ``active``; this attempt applied the fresh
    authoritative mutation and the caller should proceed to drain/storage/finalize.

    ``RESUMED``: dataset was already ``deleting``, no other FORGET is
    currently RUNNING against it, and the persisted intent of whatever last
    touched it matches this request; the caller resumes drain/storage/finalize
    without reapplying the mutation.

    ``REENTRANT``: a compatible FORGET is currently RUNNING against this
    target; the caller must not dispute drain/storage/finalization with it.

    ``BLOCKED``: an incompatible FORGET (RUNNING, or the most recent finished
    attempt) targeted this dataset; the caller must reject with a stable,
    retryable conflict and must not mutate/drain/touch storage.
    """

    outcome: DatasetAttemptOutcome
    part: DatasetMutationPart


@dataclass(frozen=True)
class DatasetFinalizeCounts:
    sources_affected: int
    sources_pending: int
    sources_deleted: int


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


def determine_forget_scope(request: ForgetRequest) -> ForgetScope:
    """Validate the request and derive its scope (FR-090 section 5).

    Scope is not a wire field: it is inferred from which fields the caller
    actually set (``request.model_fields_set``), so the SOURCE contract from
    SM-422 (``dataset`` defaulting to ``"main"``) keeps working unchanged
    while DATASET forget still requires an explicit target.
    """

    fields_set = request.model_fields_set
    if request.wait is not True:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Only wait=true is supported in this checkpoint.",
            details={"wait": request.wait},
        )
    if request.everything:
        if request.source_id is not None:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="everything cannot be combined with source_id.",
            )
        if "dataset" in fields_set:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="everything cannot be combined with an explicit dataset.",
            )
        if request.memory_only:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="everything does not support memory_only=true.",
            )
        if request.confirm != EVERYTHING_CONFIRM_PHRASE:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message='everything requires confirm="DELETE EVERYTHING".',
            )
        return ForgetScope.EVERYTHING
    if request.confirm is not None:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="confirm is only accepted when everything=true.",
        )
    if request.source_id is not None:
        return ForgetScope.SOURCE
    if "dataset" not in fields_set:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="dataset forget requires an explicit dataset.",
        )
    return ForgetScope.DATASET


class ForgetService:
    """Forget source, dataset, or everything using PostgreSQL as authority."""

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

    async def forget(self, request: ForgetRequest) -> ForgetResponseData:
        """Dispatch to the scope derived from the request (used by the API route)."""

        scope = determine_forget_scope(request)
        if scope is ForgetScope.SOURCE:
            return await self.forget_source(request)
        if scope is ForgetScope.DATASET:
            return await self.forget_dataset(request)
        return await self.forget_everything(request)

    # ------------------------------------------------------------------
    # SOURCE scope (SM-422, extended with dataset-level locking and
    # payload-aware reentrancy per SM-423 section 20/23).
    # ------------------------------------------------------------------

    async def forget_source(self, request: ForgetRequest) -> ForgetResult:
        self._validate_request(request)
        source_id = _require_forget_source_id(request)
        await self._validate_target(request, source_id)
        run_id = await self._create_running_run(request, source_id)
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
            if not isinstance(exc, _AlreadyFinalizedForgetConflict):
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

    async def _validate_target(self, request: ForgetRequest, source_id: UUID) -> None:
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
            await self._require_source_in_dataset(uow, dataset, source_id)

    async def _create_running_run(self, request: ForgetRequest, source_id: UUID) -> UUID:
        now = utc_now()
        run_id = uuid4()
        run_input = forget_run_input(request)
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.FORGET,
            dataset_id=None,
            source_id=source_id,
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
        source_id = _require_forget_source_id(request)
        payload_hash = stable_payload_hash(forget_run_input(request))
        async with self._unit_of_work_factory() as uow:
            dataset = await self._require_active_dataset_for_update(uow, request.dataset)
            source = await self._require_source_in_dataset_for_update(
                uow,
                dataset,
                source_id,
            )
            # Terminal/no-op statuses never mutate, drain, or touch storage, so
            # they are always safe regardless of any other concurrent forget.
            if source.status == SourceStatus.DELETED:
                mutation = empty_mutation(dataset=dataset, source=source)
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation
            if source.status == SourceStatus.PENDING and request.memory_only:
                mutation = empty_mutation(dataset=dataset, source=source)
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation

            # Every remaining branch either resumes or performs real work
            # (mutation, drain, storage), so it must first prove no other
            # FORGET (source-scoped on this exact source, dataset-scoped, or
            # everything-scoped) is currently RUNNING against this dataset —
            # including while this dataset is still `active` and a
            # dataset/everything PipelineRun has been created but has not
            # locked/mutated it yet (FR-090 concurrency, closes the race
            # class that produced GraphOutboxAlreadyProcessingError in SM-422).
            running = await uow.pipeline_runs.find_running_forget_for_dataset_except(
                dataset_id=dataset.id,
                source_ids=[source.id],
                excluded_run_id=run_id,
            )
            if running is not None:
                if running.payload_hash != payload_hash:
                    await self._finalize_run_as_conflict(uow, run_id)
                    await uow.commit()
                    raise _AlreadyFinalizedForgetConflict(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.CONFLICT,
                        message=(
                            "A conflicting forget operation is already running for this source."
                        ),
                        details={"source_id": str(source.id)},
                    )
                mutation = empty_mutation(
                    dataset=dataset, source=source, reentrant_in_progress=True
                )
                await self._store_partial_metrics(uow, run_id, mutation, request)
                await uow.commit()
                return mutation

            if source.status == SourceStatus.DELETING:
                # No RUNNING owner: either a genuine crash retry, or this
                # request diverges from whatever attempt last touched this
                # source. Recover that attempt's persisted intent instead of
                # assuming "no RUNNING" means "safe to resume".
                prior = await uow.pipeline_runs.find_latest_forget_for_source_except(
                    source_id=source.id,
                    excluded_run_id=run_id,
                )
                if prior is not None and prior.payload_hash != payload_hash:
                    await self._finalize_run_as_conflict(uow, run_id)
                    await uow.commit()
                    raise _AlreadyFinalizedForgetConflict(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.CONFLICT,
                        message=(
                            "A prior forget attempt on this source used different options; "
                            "retry with the same options to resume it."
                        ),
                        details={"source_id": str(source.id)},
                    )
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
        """Source forget only ever targets an ACTIVE dataset (FR-090 rule 3).

        Unlike dataset/everything forget, a source forget request is a *new*
        query against the dataset: while the dataset is ``deleting`` (a
        dataset/everything forget owns it), it must be rejected, not
        silently allowed to proceed or retried under this scope.
        """

        dataset = await uow.datasets.get_by_slug(dataset_slug)
        return _require_active_dataset(dataset, dataset_slug)

    async def _require_active_dataset_for_update(
        self,
        uow: ForgetUnitOfWork,
        dataset_slug: str,
    ) -> Dataset:
        """Lock the dataset row before locking its source (FR-090 lock ordering).

        Serializes this source mutation behind any concurrent dataset-scoped
        forget mutation on the same dataset, so the two can never interleave
        their authoritative writes.
        """

        dataset = await uow.datasets.get_by_slug_for_update(dataset_slug)
        return _require_active_dataset(dataset, dataset_slug)

    async def _require_source_in_dataset(
        self,
        uow: ForgetUnitOfWork,
        dataset: Dataset,
        source_id: UUID,
    ) -> Source:
        source = await uow.sources.get_by_id(source_id)
        return _require_source_of_dataset(source, dataset, source_id)

    async def _require_source_in_dataset_for_update(
        self,
        uow: ForgetUnitOfWork,
        dataset: Dataset,
        source_id: UUID,
    ) -> Source:
        source = await uow.sources.get_by_id_for_update(source_id)
        return _require_source_of_dataset(source, dataset, source_id)

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

    async def _finalize_run_as_conflict(self, uow: ForgetUnitOfWork, run_id: UUID) -> None:
        """Mark a run FAILED with a stable, filterable conflict code, in-transaction.

        Must be called (and its containing transaction committed) *before*
        raising :class:`_AlreadyFinalizedForgetConflict`, since this run was
        rejected before mutating anything: the containing transaction must
        not roll back the run's own bookkeeping along with the (nonexistent)
        content mutation.
        """

        run = await uow.pipeline_runs.get_by_id(run_id)
        if run is None:
            return
        run.status = PipelineRunStatus.FAILED
        run.progress = 1.0
        run.current_step = None
        run.error_code = FORGET_TARGET_CONFLICT_ERROR_CODE
        run.error_message = "Forget target is owned by a conflicting operation."
        run.finished_at = utc_now()

    # ------------------------------------------------------------------
    # DATASET scope
    # ------------------------------------------------------------------

    async def forget_dataset(self, request: ForgetRequest) -> ForgetDatasetResult:
        determine_forget_scope(request)
        dataset_id = await self._resolve_forget_dataset_id(request.dataset)
        payload_hash = stable_payload_hash(forget_dataset_run_input(request))
        run_id = await self._create_running_run_for_dataset(request, dataset_id, payload_hash)
        try:
            attempt = await self._apply_authoritative_dataset_forget(
                run_id,
                dataset_id,
                request=request,
                payload_hash=payload_hash,
            )
            if attempt.outcome is DatasetAttemptOutcome.REENTRANT:
                return await self._complete_reentrant_dataset_run(run_id, attempt.part, request)
            if attempt.outcome is DatasetAttemptOutcome.BLOCKED:
                raise _AlreadyFinalizedForgetConflict(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.CONFLICT,
                    message="A conflicting forget operation is already running for this dataset.",
                    details={"dataset_id": str(dataset_id)},
                )
            drain_result = await self._graph_projection_drain.process_dataset(dataset_id)
            graph_events_processed = int(getattr(drain_result, "processed", 0))
            storage_deleted = 0
            storage_already_absent = 0
            if not request.memory_only:
                storage_deleted, storage_already_absent = await self._delete_dataset_storage(
                    dataset_id
                )
            finalize_counts = await self._finalize_dataset_content(
                dataset_id,
                memory_only=request.memory_only,
            )
            result = await self._finalize_dataset_run(
                run_id,
                attempt.part,
                finalize_counts,
                request=request,
                graph_events_processed=graph_events_processed,
                storage_deleted=storage_deleted,
                storage_already_absent=storage_already_absent,
            )
            return result
        except Exception as exc:
            if not isinstance(exc, _AlreadyFinalizedForgetConflict):
                await self._mark_run_failed(run_id, exc)
            raise

    async def _resolve_forget_dataset_id(self, dataset_slug: str) -> UUID:
        """Unlocked pre-validation so the PipelineRun FK is never violated.

        Mirrors the SM-422 fix for source forget: the dataset must be proven
        to exist before a PipelineRun is created referencing it, and this
        never creates the ``main`` dataset (that stays exclusive to Remember).
        """

        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_slug(dataset_slug)
            resolved = _require_active_or_deleting_dataset(dataset, dataset_slug)
            return resolved.id

    async def _create_running_run_for_dataset(
        self,
        request: ForgetRequest,
        dataset_id: UUID,
        payload_hash: str,
    ) -> UUID:
        now = utc_now()
        run_id = uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.FORGET,
            dataset_id=dataset_id,
            source_id=None,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=payload_hash,
            input=forget_dataset_run_input(request),
            progress=0.0,
            current_step=FORGET_DATASET_STEP,
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

    async def _apply_authoritative_dataset_forget(
        self,
        run_id: UUID,
        dataset_id: UUID,
        *,
        request: ForgetRequest,
        payload_hash: str,
    ) -> DatasetAttempt:
        """Lock the dataset, prove no incompatible FORGET owns it, then act.

        The conflict check runs unconditionally, even when the dataset is
        still ``active``: a source-scoped forget for one of this dataset's
        sources may already have committed its mutation and still be mid
        drain/storage/finalize without ever setting ``dataset.status`` —
        that PipelineRun stays RUNNING through that whole window. Skipping
        this check while ``active`` is exactly the gap that produced
        ``GraphOutboxAlreadyProcessingError``-style double-drain races.
        """

        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_id_for_update(dataset_id)
            if dataset is None or dataset.status not in (
                DatasetStatus.ACTIVE,
                DatasetStatus.DELETING,
            ):
                raise SofiasMemoryError(
                    code=ErrorCode.INVALID_REQUEST,
                    status_code=HTTPStatus.NOT_FOUND,
                    message="Dataset does not exist.",
                    details={"dataset_id": str(dataset_id)},
                )
            sources = await uow.sources.list_for_dataset_for_update(dataset.id)
            source_ids = [source.id for source in sources]

            running = await uow.pipeline_runs.find_running_forget_for_dataset_except(
                dataset_id=dataset.id,
                source_ids=source_ids,
                excluded_run_id=run_id,
            )
            if running is not None:
                part = _empty_dataset_mutation_part(dataset)
                await self._store_dataset_partial_metrics(uow, run_id, part)
                if running.payload_hash == payload_hash:
                    await uow.commit()
                    return DatasetAttempt(DatasetAttemptOutcome.REENTRANT, part)
                await self._finalize_run_as_conflict(uow, run_id)
                await uow.commit()
                return DatasetAttempt(DatasetAttemptOutcome.BLOCKED, part)

            if dataset.status == DatasetStatus.DELETING:
                # No RUNNING owner: either a genuine crash retry, or this
                # request diverges from whatever attempt last left the
                # dataset `deleting`. Recover that attempt's persisted
                # intent instead of assuming "no RUNNING" means "resumable".
                prior = await uow.pipeline_runs.find_latest_forget_for_dataset_except(
                    dataset_id=dataset.id,
                    source_ids=source_ids,
                    excluded_run_id=run_id,
                )
                part = _empty_dataset_mutation_part(dataset)
                await self._store_dataset_partial_metrics(uow, run_id, part)
                if prior is not None and prior.payload_hash != payload_hash:
                    await self._finalize_run_as_conflict(uow, run_id)
                    await uow.commit()
                    return DatasetAttempt(DatasetAttemptOutcome.BLOCKED, part)
                await uow.commit()
                return DatasetAttempt(DatasetAttemptOutcome.RESUMED, part)

            part = await self._mutate_dataset_content(
                uow,
                dataset,
                sources,
                memory_only=request.memory_only,
            )
            await self._store_dataset_partial_metrics(uow, run_id, part)
            await uow.commit()
            return DatasetAttempt(DatasetAttemptOutcome.MUTATED, part)

    async def _mutate_dataset_content(
        self,
        uow: ForgetUnitOfWork,
        dataset: Dataset,
        sources: list[Source],
        *,
        memory_only: bool,
    ) -> DatasetMutationPart:
        dataset.status = DatasetStatus.DELETING
        for source in sources:
            source.status = SourceStatus.DELETING

        documents = await uow.documents.list_active_current_for_dataset(
            dataset_id=dataset.id,
            generation=dataset.active_generation,
        )
        chunks = await uow.chunks.list_active_current_for_dataset(
            dataset_id=dataset.id,
            generation=dataset.active_generation,
        )
        chunk_ids = [chunk.id for chunk in chunks]
        mentions = await uow.entity_mentions.list_for_chunks(chunk_ids=chunk_ids)
        evidence = await uow.relation_evidence.list_for_chunks(chunk_ids=chunk_ids)

        for document in documents:
            document.is_active = False
        for chunk in chunks:
            chunk.is_active = False

        # The whole dataset is in scope, so unlike source-scoped forget there
        # is no orphan detection to perform: every active/current entity and
        # relation of this dataset is deactivated unconditionally.
        entities = await uow.entities.list_active_current_for_dataset(dataset_id=dataset.id)
        relations = await uow.relations.list_active_current_for_dataset(dataset_id=dataset.id)
        for entity in entities:
            entity.is_active = False
        for relation in relations:
            relation.is_active = False

        summaries = await uow.summaries.list_active_for_forget(
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            document_ids=[document.id for document in documents],
            entity_ids=[entity.id for entity in entities],
            include_dataset_summary=True,
        )
        for summary in summaries:
            summary.is_active = False

        if memory_only:
            documents_by_source: dict[UUID, list[Document]] = {}
            for document in documents:
                documents_by_source.setdefault(document.source_id, []).append(document)
            for source in sources:
                source_documents = documents_by_source.get(source.id)
                if source_documents:
                    await uow.documents.add(reset_document_for_recognify(source_documents[-1]))

        commands = forget_projection_commands(
            dataset_id=dataset.id,
            chunks=chunks,
            mentions=mentions,
            relations=relations,
            entities=entities,
        )
        for command in commands:
            await uow.graph_outbox.add_projection_command(command)

        return DatasetMutationPart(
            dataset_id=dataset.id,
            generation=dataset.active_generation,
            sources_touched=len(sources),
            documents_deactivated=len(documents),
            chunks_deactivated=len(chunks),
            summaries_deactivated=len(summaries),
            entities_deactivated=len(entities),
            relations_deactivated=len(relations),
            entity_mentions_unprojected=len(mentions),
            relation_evidence_unprojected=len(evidence),
            graph_events_enqueued=len(commands),
        )

    async def _store_dataset_partial_metrics(
        self,
        uow: ForgetUnitOfWork,
        run_id: UUID,
        part: DatasetMutationPart,
    ) -> None:
        run = await uow.pipeline_runs.get_by_id(run_id)
        if run is None:
            return
        run.dataset_id = part.dataset_id
        run.metrics = {"forget_dataset_mutation": _dataset_mutation_metrics(part)}

    async def _delete_dataset_storage(self, dataset_id: UUID) -> tuple[int, int]:
        # Extract plain (id, status, storage_uri) tuples while the session is
        # still open: the ORM `Source` rows themselves must not be read after
        # the unit of work exits (rollback on a read-only UoW expires their
        # attributes, and the session is then closed — accessing `.status`
        # afterward raises `DetachedInstanceError` on a real PostgreSQL
        # session, even though in-memory fakes never show this behavior).
        async with self._unit_of_work_factory() as uow:
            sources = await uow.sources.list_for_dataset_not_deleted(dataset_id)
            source_snapshots = [
                (source.id, source.status, source.storage_uri) for source in sources
            ]
        deleted = 0
        already_absent = 0
        for source_id, status, storage_uri in source_snapshots:
            if status != SourceStatus.DELETING:
                continue
            storage_delete = delete_source_storage(
                self._settings.data_directory,
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=storage_uri,
            )
            if storage_delete.status == StorageDeleteStatus.DELETED_NOW:
                deleted += 1
            elif storage_delete.status == StorageDeleteStatus.ALREADY_ABSENT:
                already_absent += 1
        return deleted, already_absent

    async def _finalize_dataset_content(
        self,
        dataset_id: UUID,
        *,
        memory_only: bool,
    ) -> DatasetFinalizeCounts:
        async with self._unit_of_work_factory() as uow:
            dataset = await uow.datasets.get_by_id_for_update(dataset_id)
            if dataset is None:
                raise SofiasMemoryError(
                    code=ErrorCode.INTERNAL_ERROR,
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    message="Dataset could not be loaded for forget finalization.",
                )
            sources = await uow.sources.list_for_dataset_for_update(dataset.id)
            final_status = SourceStatus.PENDING if memory_only else SourceStatus.DELETED
            sources_pending = 0
            sources_deleted = 0
            for source in sources:
                if source.status != SourceStatus.DELETING:
                    continue
                source.status = final_status
                if final_status == SourceStatus.DELETED:
                    source.storage_uri = None
                    sources_deleted += 1
                else:
                    sources_pending += 1
            if dataset.status == DatasetStatus.DELETING:
                dataset.status = DatasetStatus.ACTIVE
            await uow.commit()
            return DatasetFinalizeCounts(
                sources_affected=len(sources),
                sources_pending=sources_pending,
                sources_deleted=sources_deleted,
            )

    async def _finalize_dataset_run(
        self,
        run_id: UUID,
        part: DatasetMutationPart,
        finalize_counts: DatasetFinalizeCounts,
        *,
        request: ForgetRequest,
        graph_events_processed: int,
        storage_deleted: int,
        storage_already_absent: int,
    ) -> ForgetDatasetResult:
        result = ForgetDatasetResult(
            run_id=run_id,
            status="succeeded",
            dataset_id=part.dataset_id,
            memory_only=request.memory_only,
            sources_affected=finalize_counts.sources_affected,
            sources_pending=finalize_counts.sources_pending,
            sources_deleted=finalize_counts.sources_deleted,
            documents_deactivated=part.documents_deactivated,
            chunks_deactivated=part.chunks_deactivated,
            summaries_deactivated=part.summaries_deactivated,
            entities_deactivated=part.entities_deactivated,
            relations_deactivated=part.relations_deactivated,
            entity_mentions_unprojected=part.entity_mentions_unprojected,
            relation_evidence_unprojected=part.relation_evidence_unprojected,
            graph_events_enqueued=part.graph_events_enqueued,
            graph_events_processed=graph_events_processed,
            storage_deleted=storage_deleted,
            storage_already_absent=storage_already_absent,
        )
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is not None:
                run.dataset_id = part.dataset_id
                run.status = PipelineRunStatus.SUCCEEDED
                run.progress = 1.0
                run.current_step = None
                run.error_code = None
                run.error_message = None
                run.metrics = {FORGET_DATASET_RESULT_METRIC_KEY: result.model_dump(mode="json")}
                run.finished_at = utc_now()
            await uow.commit()
        return result

    async def _complete_reentrant_dataset_run(
        self,
        run_id: UUID,
        part: DatasetMutationPart,
        request: ForgetRequest,
    ) -> ForgetDatasetResult:
        """Complete a duplicate dataset-forget request without disputing post-commit work."""

        async with self._unit_of_work_factory() as uow:
            sources = await uow.sources.list_for_dataset_not_deleted(part.dataset_id)
            result = ForgetDatasetResult(
                run_id=run_id,
                status="succeeded",
                dataset_id=part.dataset_id,
                memory_only=request.memory_only,
                sources_affected=len(sources),
                sources_pending=0,
                sources_deleted=0,
                documents_deactivated=part.documents_deactivated,
                chunks_deactivated=part.chunks_deactivated,
                summaries_deactivated=part.summaries_deactivated,
                entities_deactivated=part.entities_deactivated,
                relations_deactivated=part.relations_deactivated,
                entity_mentions_unprojected=part.entity_mentions_unprojected,
                relation_evidence_unprojected=part.relation_evidence_unprojected,
                graph_events_enqueued=part.graph_events_enqueued,
                graph_events_processed=0,
                storage_deleted=0,
                storage_already_absent=0,
            )
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is not None:
                run.dataset_id = part.dataset_id
                run.status = PipelineRunStatus.SUCCEEDED
                run.progress = 1.0
                run.current_step = None
                run.error_code = None
                run.error_message = None
                run.metrics = {FORGET_DATASET_RESULT_METRIC_KEY: result.model_dump(mode="json")}
                run.finished_at = utc_now()
            await uow.commit()
            return result

    # ------------------------------------------------------------------
    # EVERYTHING scope
    # ------------------------------------------------------------------

    async def forget_everything(self, request: ForgetRequest) -> ForgetEverythingResult:
        determine_forget_scope(request)
        payload_hash = stable_payload_hash(forget_everything_run_input(request))
        run_id = await self._create_running_run_for_everything(request, payload_hash)
        try:
            dataset_ids = await self._list_everything_targets()
            documents_deactivated = 0
            chunks_deactivated = 0
            summaries_deactivated = 0
            entities_deactivated = 0
            relations_deactivated = 0
            entity_mentions_unprojected = 0
            relation_evidence_unprojected = 0
            graph_events_enqueued = 0
            graph_events_processed = 0
            storage_deleted = 0
            storage_already_absent = 0
            sources_affected = 0
            sources_deleted = 0

            for dataset_id in dataset_ids:
                attempt = await self._apply_authoritative_dataset_forget(
                    run_id,
                    dataset_id,
                    request=request,
                    payload_hash=payload_hash,
                )
                if attempt.outcome in (
                    DatasetAttemptOutcome.BLOCKED,
                    DatasetAttemptOutcome.REENTRANT,
                ):
                    # There is no partial-success contract for everything: this
                    # run cannot claim SUCCEEDED unless it personally drove every
                    # target dataset to its final state. Abort instead of
                    # skipping — datasets already finalized earlier in this loop
                    # keep their committed state (no rollback attempted); this
                    # one and any later ones stay untouched/`deleting` and are
                    # recoverable by a retryable failure, not a silent partial
                    # success.
                    error_cls = (
                        _AlreadyFinalizedForgetConflict
                        if attempt.outcome is DatasetAttemptOutcome.BLOCKED
                        else SofiasMemoryError
                    )
                    raise error_cls(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.CONFLICT,
                        message=(
                            "A conflicting forget operation is already running for one of "
                            "the target datasets; everything forget is retryable."
                        ),
                        details={"dataset_id": str(dataset_id)},
                    )

                part = attempt.part
                documents_deactivated += part.documents_deactivated
                chunks_deactivated += part.chunks_deactivated
                summaries_deactivated += part.summaries_deactivated
                entities_deactivated += part.entities_deactivated
                relations_deactivated += part.relations_deactivated
                entity_mentions_unprojected += part.entity_mentions_unprojected
                relation_evidence_unprojected += part.relation_evidence_unprojected
                graph_events_enqueued += part.graph_events_enqueued

                drain_result = await self._graph_projection_drain.process_dataset(dataset_id)
                graph_events_processed += int(getattr(drain_result, "processed", 0))
                deleted, already_absent = await self._delete_dataset_storage(dataset_id)
                storage_deleted += deleted
                storage_already_absent += already_absent
                finalize_counts = await self._finalize_dataset_content(
                    dataset_id,
                    memory_only=False,
                )
                sources_affected += finalize_counts.sources_affected
                sources_deleted += finalize_counts.sources_deleted

            result = ForgetEverythingResult(
                run_id=run_id,
                status="succeeded",
                datasets_affected=len(dataset_ids),
                sources_affected=sources_affected,
                sources_pending=0,
                sources_deleted=sources_deleted,
                documents_deactivated=documents_deactivated,
                chunks_deactivated=chunks_deactivated,
                summaries_deactivated=summaries_deactivated,
                entities_deactivated=entities_deactivated,
                relations_deactivated=relations_deactivated,
                entity_mentions_unprojected=entity_mentions_unprojected,
                relation_evidence_unprojected=relation_evidence_unprojected,
                graph_events_enqueued=graph_events_enqueued,
                graph_events_processed=graph_events_processed,
                storage_deleted=storage_deleted,
                storage_already_absent=storage_already_absent,
            )
            await self._finalize_everything_run(run_id, result)
            return result
        except Exception as exc:
            if not isinstance(exc, _AlreadyFinalizedForgetConflict):
                await self._mark_run_failed(run_id, exc)
            raise

    async def _list_everything_targets(self) -> list[UUID]:
        async with self._unit_of_work_factory() as uow:
            return await uow.datasets.list_ids_for_everything_forget()

    async def _create_running_run_for_everything(
        self,
        request: ForgetRequest,
        payload_hash: str,
    ) -> UUID:
        now = utc_now()
        run_id = uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.FORGET,
            dataset_id=None,
            source_id=None,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=payload_hash,
            input=forget_everything_run_input(request),
            progress=0.0,
            current_step=FORGET_EVERYTHING_STEP,
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

    async def _finalize_everything_run(self, run_id: UUID, result: ForgetEverythingResult) -> None:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is not None:
                run.status = PipelineRunStatus.SUCCEEDED
                run.progress = 1.0
                run.current_step = None
                run.error_code = None
                run.error_message = None
                run.metrics = {FORGET_EVERYTHING_RESULT_METRIC_KEY: result.model_dump(mode="json")}
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
        "scope": ForgetScope.SOURCE.value,
        "dataset": request.dataset,
        "source_id": str(request.source_id),
        "memory_only": request.memory_only,
        "wait": request.wait,
    }


def forget_dataset_run_input(request: ForgetRequest) -> dict[str, JSONValue]:
    return {
        "scope": ForgetScope.DATASET.value,
        "dataset": request.dataset,
        "memory_only": request.memory_only,
        "wait": request.wait,
    }


def forget_everything_run_input(request: ForgetRequest) -> dict[str, JSONValue]:
    return {
        "scope": ForgetScope.EVERYTHING.value,
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


def _empty_dataset_mutation_part(dataset: Dataset) -> DatasetMutationPart:
    return DatasetMutationPart(
        dataset_id=dataset.id,
        generation=dataset.active_generation,
        sources_touched=0,
        documents_deactivated=0,
        chunks_deactivated=0,
        summaries_deactivated=0,
        entities_deactivated=0,
        relations_deactivated=0,
        entity_mentions_unprojected=0,
        relation_evidence_unprojected=0,
        graph_events_enqueued=0,
    )


def _dataset_mutation_metrics(part: DatasetMutationPart) -> dict[str, JSONValue]:
    return {
        "dataset_id": str(part.dataset_id),
        "sources_touched": part.sources_touched,
        "documents_deactivated": part.documents_deactivated,
        "chunks_deactivated": part.chunks_deactivated,
        "summaries_deactivated": part.summaries_deactivated,
        "entities_deactivated": part.entities_deactivated,
        "relations_deactivated": part.relations_deactivated,
        "entity_mentions_unprojected": part.entity_mentions_unprojected,
        "relation_evidence_unprojected": part.relation_evidence_unprojected,
        "graph_events_enqueued": part.graph_events_enqueued,
    }


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


def _require_forget_source_id(request: ForgetRequest) -> UUID:
    """SOURCE scope guarantees ``source_id`` is set; ``determine_forget_scope`` enforces it.

    This only guards direct calls to ``forget_source`` that bypass scope
    determination (as SM-422's existing unit tests do, always with a real
    ``source_id``); it narrows the optional field for the type checker.
    """

    if request.source_id is None:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="source_id is required for source forget.",
        )
    return request.source_id


def _require_active_dataset(dataset: Dataset | None, dataset_slug: str) -> Dataset:
    if dataset is None or dataset.status != DatasetStatus.ACTIVE:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.NOT_FOUND,
            message="Dataset does not exist.",
            details={"dataset": dataset_slug},
        )
    return dataset


def _require_active_or_deleting_dataset(dataset: Dataset | None, dataset_slug: str) -> Dataset:
    """Dataset/everything forget accepts ACTIVE (fresh) or DELETING (retry)."""

    if dataset is None or dataset.status not in (DatasetStatus.ACTIVE, DatasetStatus.DELETING):
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.NOT_FOUND,
            message="Dataset does not exist.",
            details={"dataset": dataset_slug},
        )
    return dataset


def _require_source_of_dataset(source: Source | None, dataset: Dataset, source_id: UUID) -> Source:
    if source is None or source.dataset_id != dataset.id:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.NOT_FOUND,
            message="Source does not exist.",
            details={"source_id": str(source_id)},
        )
    return source


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> ForgetUnitOfWork:
        return cast(ForgetUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
