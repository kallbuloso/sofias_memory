"""Forget v1 pure/reusable primitives (SM-512).

This module no longer owns any run lifecycle (that is
``sofias_memory.pipelines.steps.forget``, ADR-0009 SS O, mirroring SM-510/511's
``services.cognify``/``services.improve`` split): it holds only the
business-logic building blocks the B5 Forget pipeline steps compose --
scope derivation, storage-path safety, authoritative-mutation helpers,
projection command building, and B4-legacy-intent-compatible target
recovery. Every function here is either pure or performs only read/lock
PostgreSQL access; none of them commits, and none of them is ever called
from a ``PipelineStep.persist`` when it would require external I/O
(ADR-0009 SS O forbids external I/O and independent commits there).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import UUID

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
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
from sofias_memory.ports import (
    ProjectionCommand,
    chunk_delete_command,
    chunk_next_delete_command,
    entity_delete_command,
    entity_mention_delete_command,
    relation_delete_command,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue

EVERYTHING_CONFIRM_PHRASE = "DELETE EVERYTHING"

FORGET_RESULT_METRIC_KEY = "forget_result"
FORGET_DATASET_RESULT_METRIC_KEY = "forget_dataset_result"
FORGET_EVERYTHING_RESULT_METRIC_KEY = "forget_everything_result"


class ForgetScope(StrEnum):
    """The three FR-090 forget scopes, derived from the request, not sent by it."""

    SOURCE = "source"
    DATASET = "dataset"
    EVERYTHING = "everything"


# ---------------------------------------------------------------------------
# Repository Protocols (structural typing shared with the B5 pipeline steps)
# ---------------------------------------------------------------------------


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

    async def flush(self) -> None: ...


# ---------------------------------------------------------------------------
# Scope derivation (unchanged FR-090 contract, minus the B4 wait=true-only gate)
# ---------------------------------------------------------------------------


def determine_forget_scope(
    *,
    dataset: str,
    fields_set: set[str],
    source_id: UUID | None,
    everything: bool,
    confirm: str | None,
    memory_only: bool,
) -> ForgetScope:
    """Validate the request and derive its scope (FR-090 section 5).

    Scope is not a wire field: it is inferred from which fields the caller
    actually set (``request.model_fields_set``), so the SOURCE contract from
    SM-422 (``dataset`` defaulting to ``"main"``) keeps working unchanged
    while DATASET forget still requires an explicit target.

    SM-512: unlike B4, this no longer rejects ``wait=false`` -- both wait
    modes are real in the B5 async submission contract, so ``wait`` plays no
    part in scope derivation at all any more.
    """

    del dataset
    if everything:
        if source_id is not None:
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
        if memory_only:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="everything does not support memory_only=true.",
            )
        if confirm != EVERYTHING_CONFIRM_PHRASE:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message='everything requires confirm="DELETE EVERYTHING".',
            )
        return ForgetScope.EVERYTHING
    if confirm is not None:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="confirm is only accepted when everything=true.",
        )
    if source_id is not None:
        return ForgetScope.SOURCE
    if "dataset" not in fields_set:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="dataset forget requires an explicit dataset.",
        )
    return ForgetScope.DATASET


# ---------------------------------------------------------------------------
# Work identity (SM-512 SS 4): wait/confirm excluded, memory_only included.
# ---------------------------------------------------------------------------


def forget_source_run_input(
    *, dataset: str, source_id: UUID, memory_only: bool
) -> dict[str, JSONValue]:
    return {
        "scope": ForgetScope.SOURCE.value,
        "dataset": dataset,
        "source_id": str(source_id),
        "memory_only": memory_only,
    }


def forget_dataset_run_input(*, dataset: str, memory_only: bool) -> dict[str, JSONValue]:
    return {
        "scope": ForgetScope.DATASET.value,
        "dataset": dataset,
        "memory_only": memory_only,
    }


def forget_everything_run_input() -> dict[str, JSONValue]:
    return {"scope": ForgetScope.EVERYTHING.value}


# ---------------------------------------------------------------------------
# B4 -> B5 semantic intent compatibility (SM-512 SS 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForgetSemanticIntent:
    """The durable, comparable *meaning* of a Forget target-recovery intent,
    independent of which checkpoint (B4 or B5) persisted it.

    Deliberately excludes ``wait`` (B4 persisted it; B5 never does) and
    ``confirm`` (a one-time safety acknowledgement, never identity) --
    comparing two intents for target-recovery purposes (may a fresh attempt
    resume a ``DELETING`` target left by a prior one?) must never be
    confused with the SM-509 submission Idempotency-Key/payload-hash
    contract, which is a completely different, unrelated mechanism.
    """

    scope: str
    dataset: str | None
    source_id: str | None
    memory_only: bool


def forget_semantic_intent_from_run_input(
    run_input: Mapping[str, Any] | None,
) -> ForgetSemanticIntent | None:
    """Derive a comparable intent from a persisted ``PipelineRun.input``.

    Tolerates both the B5 shape (no ``wait``) and the B4 legacy shape
    (``wait`` present, ignored) -- only the fields that carry real semantic
    meaning are read. Returns ``None`` when ``run_input`` does not look like
    a recognizable Forget work identity at all (defensive: a malformed or
    unrelated payload must never be silently treated as "the same intent").
    """

    if not isinstance(run_input, Mapping):
        return None
    scope = run_input.get("scope")
    if scope not in (
        ForgetScope.SOURCE.value,
        ForgetScope.DATASET.value,
        ForgetScope.EVERYTHING.value,
    ):
        return None
    memory_only = run_input.get("memory_only", False)
    if not isinstance(memory_only, bool):
        return None
    dataset = run_input.get("dataset")
    if scope in (ForgetScope.SOURCE.value, ForgetScope.DATASET.value) and not isinstance(
        dataset, str
    ):
        return None
    source_id = run_input.get("source_id")
    if scope == ForgetScope.SOURCE.value and not isinstance(source_id, str):
        return None
    return ForgetSemanticIntent(
        scope=str(scope),
        dataset=str(dataset)
        if scope in (ForgetScope.SOURCE.value, ForgetScope.DATASET.value)
        else None,
        source_id=str(source_id) if scope == ForgetScope.SOURCE.value else None,
        memory_only=memory_only,
    )


def same_forget_intent(
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
) -> bool:
    """Whether two persisted ``PipelineRun.input`` payloads express the same
    Forget target-recovery intent (SM-512 SS 5/38), B4-legacy-tolerant.

    Two unrecognizable/malformed inputs are never considered "the same" --
    defensive comparison never resolves to a false positive.
    """

    left_intent = forget_semantic_intent_from_run_input(left)
    right_intent = forget_semantic_intent_from_run_input(right)
    if left_intent is None or right_intent is None:
        return False
    return left_intent == right_intent


# ---------------------------------------------------------------------------
# Result dataclasses shared by the pipeline steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForgetMutation:
    dataset_id: UUID
    generation: int
    source_id: UUID
    source_status: str
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

    All-zero when the attempt did not itself mutate content -- either
    because a compatible attempt owns the target (``REENTRANT``/``RESUMED``)
    or because it was rejected (``BLOCKED``); see :class:`DatasetAttempt`.
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
        source_status=source.status.value,
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


def empty_dataset_mutation_part(dataset: Dataset) -> DatasetMutationPart:
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


# ---------------------------------------------------------------------------
# Authoritative mutation helpers (PostgreSQL-only, no commit)
# ---------------------------------------------------------------------------


def reset_document_for_recognify(document: Document) -> Document:
    from uuid import uuid4

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
        metadata_={RESET_DOCUMENT_METADATA_KEY: {"version": RESET_DOCUMENT_METADATA_VERSION}},
        is_active=True,
    )


async def apply_source_forget_mutation(
    uow: ForgetUnitOfWork,
    *,
    dataset: Dataset,
    source: Source,
    memory_only: bool,
) -> ForgetMutation:
    """Full SOURCE-scope authoritative mutation (SM-422/SM-512), unchanged
    algorithm: deactivate content, orphan-detect relations then entities,
    deactivate summaries, enqueue projection deletes. Caller (the pipeline
    step's ``persist``) owns setting ``source.status = DELETING`` beforehand
    and committing afterward -- this function only mutates in-session state
    plus flushes for the orphan queries to observe it."""

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
    mentions = await uow.entity_mentions.list_for_chunks(chunk_ids=[chunk.id for chunk in chunks])
    evidence = await uow.relation_evidence.list_for_chunks(chunk_ids=[chunk.id for chunk in chunks])
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
    valid_relation_ids = await uow.relation_evidence.list_relation_ids_with_authoritative_evidence(
        dataset_id=dataset.id,
        relation_ids=[relation.id for relation in relation_candidates],
    )
    deactivated_relations = [
        relation for relation in relation_candidates if relation.id not in valid_relation_ids
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
    valid_entity_mentions = await uow.entity_mentions.list_entity_ids_with_authoritative_mentions(
        dataset_id=dataset.id,
        entity_ids=entity_ids,
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

    if memory_only and documents:
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

    return ForgetMutation(
        dataset_id=dataset.id,
        generation=dataset.active_generation,
        source_id=source.id,
        source_status="deleting",
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


async def apply_dataset_forget_mutation(
    uow: ForgetUnitOfWork,
    *,
    dataset: Dataset,
    sources: list[Source],
    memory_only: bool,
) -> DatasetMutationPart:
    """Full DATASET-scope authoritative mutation (SM-512), unchanged
    algorithm: the whole dataset is in scope, so unlike source-scoped
    forget there is no orphan detection -- every active/current entity and
    relation of this dataset is deactivated unconditionally. Caller owns
    setting ``dataset.status``/``source.status = DELETING`` beforehand and
    committing afterward."""

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


# ---------------------------------------------------------------------------
# Projection command building (unchanged ADR-0008 identities)
# ---------------------------------------------------------------------------


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
        _add_command(commands, chunk_delete_command(chunk_id=chunk.id, dataset_id=dataset_id))
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
        _add_command(commands, entity_delete_command(entity_id=entity.id, dataset_id=dataset_id))

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


# ---------------------------------------------------------------------------
# Storage deletion safety (unchanged guards)
# ---------------------------------------------------------------------------


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
        data_directory, dataset_id=dataset_id, source_id=source_id, storage_uri=storage_uri
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


def dataset_not_found_error(dataset_slug: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Dataset does not exist.",
        details={"dataset": dataset_slug},
    )


def source_not_found_error(source_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Source does not exist.",
        details={"source_id": str(source_id)},
    )


__all__ = [
    "EVERYTHING_CONFIRM_PHRASE",
    "FORGET_DATASET_RESULT_METRIC_KEY",
    "FORGET_EVERYTHING_RESULT_METRIC_KEY",
    "FORGET_RESULT_METRIC_KEY",
    "FORGET_TARGET_CONFLICT_ERROR_CODE",
    "DatasetAttempt",
    "DatasetAttemptOutcome",
    "DatasetFinalizeCounts",
    "DatasetMutationPart",
    "ForgetMutation",
    "ForgetScope",
    "ForgetSemanticIntent",
    "ForgetUnitOfWork",
    "StorageDeleteResult",
    "StorageDeleteStatus",
    "apply_dataset_forget_mutation",
    "apply_source_forget_mutation",
    "chunk_next_delete_commands",
    "dataset_not_found_error",
    "delete_source_storage",
    "determine_forget_scope",
    "empty_dataset_mutation_part",
    "empty_mutation",
    "forget_dataset_run_input",
    "forget_everything_run_input",
    "forget_projection_commands",
    "forget_semantic_intent_from_run_input",
    "forget_source_run_input",
    "invalid_storage_uri_error",
    "reset_document_for_recognify",
    "same_forget_intent",
    "source_not_found_error",
    "source_storage_path",
]
