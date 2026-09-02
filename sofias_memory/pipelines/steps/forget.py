"""Forget pipeline steps (SM-512, ADR-0009 SS O).

Five fixed steps, always present, always executed in this order, for every
scope (SOURCE/DATASET/EVERYTHING alike -- the request selects a scope, never
a pipeline):

1. ``authoritative_mutation`` -- PostgreSQL-only. Locks the target(s),
   resolves target-recovery intent (fresh / ``RESUMED`` / ``REENTRANT`` /
   ``BLOCKED``, B4-legacy-tolerant via ``same_forget_intent``), and applies
   the authoritative content mutation. Zero external dependency, so the
   whole sequence lives in ``persist`` (same reasoning as Improve's
   ``feedback_weights``/``entity_merge``/``graph_maintain``, SM-511).
2. ``projection_convergence`` -- drains the ``graph_outbox`` rows this run's
   own mutation enqueued, for every target dataset touched (external Neo4j,
   ``execute``-only).
3. ``storage_deletion`` -- deletes on-disk source storage for FULL-scope
   targets (external filesystem, ``execute``-only). Re-reads ``storage_uri``
   fresh from PostgreSQL rather than caching it anywhere -- it must never
   reach ``StepResult``/``PipelineStep`` (SM-512 SS 26).
4. ``finalize_target`` -- PostgreSQL-only. Sets final ``Source``/``Dataset``
   status from what steps 1 and 3 actually observed.
5. ``finalize_result`` -- PostgreSQL-only. Aggregates every prior step's
   safe output into ``run.metrics[<scope-appropriate key>]``.

EVERYTHING is the one scope with a dynamic number of targets (every existing
Dataset). ADR-0009 forbids creating steps dynamically or committing per
target inside ``execute``, so ``authoritative_mutation`` applies EVERY
target's mutation inside ONE engine-owned transaction (SM-512 SS 16): either
the whole attempt's mutation commits together, or (on a conflict with any
one target) the whole attempt rolls back and nothing commits. This is a
deliberate, disclosed simplification of B4's finer-grained "each dataset
commits and finalizes before the next starts, stopping at the first
conflict" behavior -- see the SM-512 report for the safety argument (no
partial/fabricated state either way; a fresh retry re-processes every
target, which is idempotent and correctness-preserving, only less granular).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.infrastructure.storage import (
    InvalidSourceStorageUriError,
    SourceObjectStorage,
    SourceStorageRouter,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
    UnsupportedStorageBackendError,
)
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    PipelineDefinition,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
    no_op_persist,
)
from sofias_memory.services.forget import (
    FORGET_DATASET_RESULT_METRIC_KEY,
    FORGET_EVERYTHING_RESULT_METRIC_KEY,
    FORGET_RESULT_METRIC_KEY,
    FORGET_TARGET_CONFLICT_ERROR_CODE,
    ForgetScope,
    apply_dataset_forget_mutation,
    apply_source_forget_mutation,
    empty_dataset_mutation_part,
    empty_mutation,
    same_forget_intent,
)
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor

FORGET_RESOURCES_RESOURCE = "forget_resources"

AUTHORITATIVE_MUTATION_STEP = "authoritative_mutation"
PROJECTION_CONVERGENCE_STEP = "projection_convergence"
STORAGE_DELETION_STEP = "storage_deletion"
FINALIZE_TARGET_STEP = "finalize_target"
FINALIZE_RESULT_STEP = "finalize_result"

_DEFINITION_ID_PREFIX = "forget."

FORGET_SCOPE_ERROR_CODE = "FORGET_RUN_SCOPE_INVALID"
FORGET_SCOPE_ERROR_MESSAGE = "Forget run scope/input is invalid."
FORGET_TARGET_MISSING_ERROR_CODE = "FORGET_TARGET_MISSING"
FORGET_TARGET_MISSING_MESSAGE = "Forget target no longer exists."
FORGET_DEPENDENCY_ERROR_CODE = "FORGET_DEPENDENCY_UNAVAILABLE"
FORGET_DEPENDENCY_ERROR_MESSAGE = "A Forget dependency was unavailable."
FORGET_RESOURCE_MISSING_ERROR_CODE = "FORGET_RESOURCE_MISSING"
FORGET_RESOURCE_MISSING_MESSAGE = "Forget processing resources are not configured."
FORGET_STORAGE_RESULT_MISSING_ERROR_CODE = "FORGET_STORAGE_RESULT_MISSING"
FORGET_STORAGE_RESULT_MISSING_MESSAGE = (
    "Forget storage deletion result is missing for a Source being finalized."
)
FORGET_STORAGE_RESULT_DUPLICATE_ERROR_CODE = "FORGET_STORAGE_RESULT_DUPLICATE"
FORGET_STORAGE_RESULT_DUPLICATE_MESSAGE = (
    "Forget storage deletion produced more than one result for the same Source."
)

# ADR-0011 D37: the four StorageDeleteResult outcomes this step consumes,
# spelled as their durable string values (StepResult output is plain JSON).
_STORAGE_STATUS_DELETED_NOW = StorageDeleteStatus.DELETED_NOW.value
_STORAGE_STATUS_ALREADY_ABSENT = StorageDeleteStatus.ALREADY_ABSENT.value
_STORAGE_STATUS_NOT_REQUESTED = StorageDeleteStatus.NOT_REQUESTED.value
_STORAGE_STATUS_UNRESOLVED = StorageDeleteStatus.UNRESOLVED.value
_STORAGE_STATUSES_CLEARING_URI = (
    _STORAGE_STATUS_DELETED_NOW,
    _STORAGE_STATUS_ALREADY_ABSENT,
    _STORAGE_STATUS_NOT_REQUESTED,
)


@dataclass(frozen=True)
class ForgetPipelineResources:
    """Everything a Forget step needs, built once per process (SM-512).

    No embedding/LLM client at all -- Forget is pure PostgreSQL + Neo4j +
    the ADR-0011 Source storage boundary. ``graph_outbox_drain`` is ``None``
    only when the process has no Neo4j resource configured at all (mirrors
    Improve's ``ImprovePipelineResources``, SM-511)."""

    settings: Settings
    graph_outbox_drain: GraphOutboxBatchProcessor | None
    source_storage: SourceObjectStorage | None = None
    """Injection point mirroring Cognify/Remember's own ``source_storage``
    parameter (STORAGE-003/004): ``None`` in production wiring, where a
    ``SourceStorageRouter`` is constructed lazily per call in
    :func:`_source_storage` -- never eagerly here, so a settings-only
    resource never depends on S3 configuration being present."""


def _resources(context: PipelineContext) -> ForgetPipelineResources:
    resource = context.resources.get(FORGET_RESOURCES_RESOURCE)
    if resource is None:
        raise PermanentPipelineStepError(
            FORGET_RESOURCE_MISSING_ERROR_CODE, FORGET_RESOURCE_MISSING_MESSAGE
        )
    return cast(ForgetPipelineResources, resource)


def _source_storage(resources: ForgetPipelineResources) -> SourceObjectStorage:
    return resources.source_storage or SourceStorageRouter(resources.settings)


def _scope(run_input: Mapping[str, Any]) -> ForgetScope:
    scope = run_input.get("scope")
    try:
        return ForgetScope(str(scope))
    except ValueError as exc:
        raise PermanentPipelineStepError(
            FORGET_SCOPE_ERROR_CODE, FORGET_SCOPE_ERROR_MESSAGE
        ) from exc


def _uniform_input(
    run_input: Mapping[str, Any], step_outputs: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Every step's semantic input is derivable purely from ``run_input``."""

    del step_outputs
    return dict(run_input)


# ---------------------------------------------------------------------------
# 1. authoritative_mutation -- no external dependency, entirely in persist().
# ---------------------------------------------------------------------------


class AuthoritativeMutationStep:
    """ATOMIC: zero external dependency (locking, target-recovery intent
    resolution, and the content mutation are all pure PostgreSQL), so the
    whole sequence lives in ``persist`` -- nothing commits anywhere else, so
    an orphaned RUNNING row proves nothing was committed."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        scope = _scope(context.run_input)
        if scope is ForgetScope.SOURCE:
            output = await _mutate_source(context, uow)
        elif scope is ForgetScope.DATASET:
            output = await _mutate_dataset(context, uow)
        else:
            output = await _mutate_everything(context, uow)
        result.output.update(output)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


async def _mutate_source(context: PipelineContext, uow: PostgresUnitOfWork) -> dict[str, Any]:
    if context.dataset_id is None or context.source_id is None:
        raise PermanentPipelineStepError(FORGET_SCOPE_ERROR_CODE, FORGET_SCOPE_ERROR_MESSAGE)
    memory_only = bool(context.run_input.get("memory_only", False))

    dataset = await uow.datasets.get_by_slug_for_update(str(context.run_input.get("dataset")))
    if dataset is None or dataset.status != DatasetStatus.ACTIVE:
        raise PermanentPipelineStepError(
            FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
        )
    source = await uow.sources.get_by_id_for_update(context.source_id)
    if source is None or source.dataset_id != dataset.id:
        raise PermanentPipelineStepError(
            FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
        )

    # Terminal/no-op statuses never mutate, drain, or touch storage, so they
    # are always safe regardless of any other concurrent forget.
    if source.status == SourceStatus.DELETED or (
        source.status == SourceStatus.PENDING and memory_only
    ):
        mutation = empty_mutation(dataset=dataset, source=source)
        return _source_mutation_output(mutation, proceed=True)

    running = await uow.pipeline_runs.find_running_forget_for_dataset_except(
        dataset_id=dataset.id, source_ids=[source.id], excluded_run_id=context.run_id
    )
    if running is not None:
        if not same_forget_intent(running.input, context.run_input):
            raise PermanentPipelineStepError(
                FORGET_TARGET_CONFLICT_ERROR_CODE,
                "A conflicting forget operation is already running for this source.",
            )
        mutation = empty_mutation(dataset=dataset, source=source, reentrant_in_progress=True)
        return _source_mutation_output(mutation, proceed=False)

    if source.status == SourceStatus.DELETING:
        prior = await uow.pipeline_runs.find_latest_forget_for_source_except(
            source_id=source.id, excluded_run_id=context.run_id
        )
        if prior is not None and not same_forget_intent(prior.input, context.run_input):
            raise PermanentPipelineStepError(
                FORGET_TARGET_CONFLICT_ERROR_CODE,
                "A prior forget attempt on this source used different options; retry with "
                "the same options to resume it.",
            )
        mutation = empty_mutation(dataset=dataset, source=source)
        return _source_mutation_output(mutation, proceed=True)

    source.status = SourceStatus.DELETING
    mutation = await apply_source_forget_mutation(
        cast(Any, uow), dataset=dataset, source=source, memory_only=memory_only
    )
    return _source_mutation_output(mutation, proceed=True)


def _source_mutation_output(mutation: Any, *, proceed: bool) -> dict[str, Any]:
    return {
        "scope": ForgetScope.SOURCE.value,
        "dataset_id": str(mutation.dataset_id),
        "source_id": str(mutation.source_id),
        "proceed": proceed,
        "documents_deactivated": mutation.documents_deactivated,
        "chunks_deactivated": mutation.chunks_deactivated,
        "summaries_deactivated": mutation.summaries_deactivated,
        "entities_deactivated": mutation.entities_deactivated,
        "relations_deactivated": mutation.relations_deactivated,
        "entity_mentions_unprojected": mutation.entity_mentions_unprojected,
        "relation_evidence_unprojected": mutation.relation_evidence_unprojected,
        "graph_events_enqueued": mutation.graph_events_enqueued,
    }


async def _mutate_dataset(context: PipelineContext, uow: PostgresUnitOfWork) -> dict[str, Any]:
    if context.dataset_id is None:
        raise PermanentPipelineStepError(FORGET_SCOPE_ERROR_CODE, FORGET_SCOPE_ERROR_MESSAGE)
    memory_only = bool(context.run_input.get("memory_only", False))
    target = await _mutate_one_dataset(
        context, uow, dataset_id=context.dataset_id, memory_only=memory_only
    )
    return {"scope": ForgetScope.DATASET.value, "targets": [target]}


async def _mutate_everything(context: PipelineContext, uow: PostgresUnitOfWork) -> dict[str, Any]:
    dataset_ids = await uow.datasets.list_ids_for_everything_forget()
    targets = [
        await _mutate_one_dataset(context, uow, dataset_id=dataset_id, memory_only=False)
        for dataset_id in dataset_ids
    ]
    return {"scope": ForgetScope.EVERYTHING.value, "targets": targets}


async def _mutate_one_dataset(
    context: PipelineContext,
    uow: PostgresUnitOfWork,
    *,
    dataset_id: UUID,
    memory_only: bool,
) -> dict[str, Any]:
    dataset = await uow.datasets.get_by_id_for_update(dataset_id)
    if dataset is None or dataset.status not in (DatasetStatus.ACTIVE, DatasetStatus.DELETING):
        raise PermanentPipelineStepError(
            FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
        )
    sources = await uow.sources.list_for_dataset_for_update(dataset.id)
    source_ids = [source.id for source in sources]

    running = await uow.pipeline_runs.find_running_forget_for_dataset_except(
        dataset_id=dataset.id, source_ids=source_ids, excluded_run_id=context.run_id
    )
    if running is not None:
        if not same_forget_intent(running.input, context.run_input):
            raise PermanentPipelineStepError(
                FORGET_TARGET_CONFLICT_ERROR_CODE,
                "A conflicting forget operation is already running for one of the target datasets.",
            )
        part = empty_dataset_mutation_part(dataset)
        return _dataset_mutation_output(part, proceed=False)

    if dataset.status == DatasetStatus.DELETING:
        prior = await uow.pipeline_runs.find_latest_forget_for_dataset_except(
            dataset_id=dataset.id, source_ids=source_ids, excluded_run_id=context.run_id
        )
        if prior is not None and not same_forget_intent(prior.input, context.run_input):
            raise PermanentPipelineStepError(
                FORGET_TARGET_CONFLICT_ERROR_CODE,
                "A prior forget attempt on this dataset used different options; retry with "
                "the same options to resume it.",
            )
        part = empty_dataset_mutation_part(dataset)
        return _dataset_mutation_output(part, proceed=True)

    dataset.status = DatasetStatus.DELETING
    for source in sources:
        source.status = SourceStatus.DELETING
    part = await apply_dataset_forget_mutation(
        cast(Any, uow), dataset=dataset, sources=sources, memory_only=memory_only
    )
    return _dataset_mutation_output(part, proceed=True)


def _dataset_mutation_output(part: Any, *, proceed: bool) -> dict[str, Any]:
    return {
        "dataset_id": str(part.dataset_id),
        "proceed": proceed,
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


# ---------------------------------------------------------------------------
# 2. projection_convergence -- drains what step 1 enqueued.
# ---------------------------------------------------------------------------


class ProjectionConvergenceStep:
    """ATOMIC (vacuously): no authoritative PostgreSQL mutation of its own
    -- draining is independently idempotent (SM-506 leasing)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        mutation_output = context.step_outputs.get(AUTHORITATIVE_MUTATION_STEP, {})
        dataset_ids = _proceeding_dataset_ids(mutation_output)
        if not dataset_ids:
            return StepResult(output={"graph_events_processed": 0})

        resources = _resources(context)
        if resources.graph_outbox_drain is None:
            # Documented degrade (SM-511 precedent): no Neo4j configured at
            # all means nothing can converge regardless of this run.
            return StepResult(output={"graph_events_processed": 0})

        total = 0
        for dataset_id in dataset_ids:
            drained = await resources.graph_outbox_drain.process_dataset(dataset_id)
            total += int(getattr(drained, "processed", 0))
        return StepResult(output={"graph_events_processed": total})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def _proceeding_dataset_ids(mutation_output: Mapping[str, Any]) -> list[UUID]:
    scope = mutation_output.get("scope")
    if scope == ForgetScope.SOURCE.value:
        if not mutation_output.get("proceed"):
            return []
        dataset_id = mutation_output.get("dataset_id")
        return [UUID(str(dataset_id))] if dataset_id else []
    targets = mutation_output.get("targets", [])
    return [UUID(str(target["dataset_id"])) for target in targets if target.get("proceed")]


# ---------------------------------------------------------------------------
# 3. storage_deletion -- external filesystem, execute()-only.
# ---------------------------------------------------------------------------


class StorageDeletionStep:
    """AMBIGUOUS cancellation recovery (SM-512 SS 27): a PostgreSQL-only
    reconciliation callback cannot prove whether an orphaned RUNNING attempt
    already reached a terminal outcome for a Source's storage before
    crashing, so stale-``CANCELLING`` recovery cannot safely report
    ``CANCELLED`` for this step -- it fails safe instead. The target stays
    ``DELETING`` (a durable, recovery-aware state, not corruption): a later
    semantically-compatible retry resumes and safely re-observes/deletes via
    ``SourceStorageRouter.delete`` (idempotent -- an already-absent object
    converges cleanly to ``ALREADY_ABSENT``).

    ADR-0011 D37: a recognized storage-layer failure for one Source (lost
    S3 configuration, credentials, timeout, Object Lock, an unsafe-to-
    resolve URI, ...) becomes an explicit, successful ``UNRESOLVED`` result
    for that Source -- never a step failure, and never silently skipped.
    Only an exception outside :class:`SourceStorageError` (a genuine
    programming defect) propagates as a real step failure."""

    async def execute(self, context: PipelineContext) -> StepResult:
        mutation_output = context.step_outputs.get(AUTHORITATIVE_MUTATION_STEP, {})
        memory_only = bool(context.run_input.get("memory_only", False))
        if memory_only:
            return StepResult(
                output={
                    "sources": [],
                    "deleted_now": 0,
                    "already_absent": 0,
                    "unresolved": 0,
                    "not_requested": 0,
                }
            )

        scope = mutation_output.get("scope")
        resources = _resources(context)
        storage = _source_storage(resources)
        entries: list[dict[str, Any]] = []
        if scope == ForgetScope.SOURCE.value:
            if mutation_output.get("proceed"):
                entry = await _delete_one_source_storage(
                    context,
                    storage,
                    dataset_id=UUID(str(mutation_output["dataset_id"])),
                    source_id=UUID(str(mutation_output["source_id"])),
                )
                if entry is not None:
                    entries.append(entry)
        else:
            for target in mutation_output.get("targets", []):
                if not target.get("proceed"):
                    continue
                entries.extend(
                    await _delete_dataset_storage(
                        context, storage, dataset_id=UUID(str(target["dataset_id"]))
                    )
                )

        return StepResult(output={"sources": entries, **_storage_status_counts(entries)})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def _storage_status_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "deleted_now": sum(
            1 for entry in entries if entry["status"] == _STORAGE_STATUS_DELETED_NOW
        ),
        "already_absent": sum(
            1 for entry in entries if entry["status"] == _STORAGE_STATUS_ALREADY_ABSENT
        ),
        "unresolved": sum(1 for entry in entries if entry["status"] == _STORAGE_STATUS_UNRESOLVED),
        "not_requested": sum(
            1 for entry in entries if entry["status"] == _STORAGE_STATUS_NOT_REQUESTED
        ),
    }


async def _delete_source_storage_result(
    storage: SourceObjectStorage, *, dataset_id: UUID, source_id: UUID, storage_uri: str | None
) -> StorageDeleteResult:
    """The one place ``StorageDeletionStep``/``DeleteStorageStep`` call the
    router.

    STORAGE-005 exception-classification audit (ADR-0011 D37/D38): catches
    only the ``SourceStorageError`` subclasses that a delete-path raise site
    can actually produce and that the ADR itself classifies as a recognized
    operational inability -- lost/unusable backend configuration
    (``UnsupportedStorageBackendError``, e.g. D41's lost-S3-config
    scenario), an unavailable backend/dependency
    (``SourceStorageUnavailableError``), or a storage locator that cannot be
    safely resolved (``InvalidSourceStorageUriError``, D38's "path/storage
    dependency cannot be safely resolved" clause).

    Deliberately narrower than the ``SourceStorageError`` base class:
    ``SourceStorageConflictError`` (a deterministic content-identity
    conflict -- not reachable from ``delete()`` today, but never an
    operational inability if it ever were) and ``SourceStoragePathError``
    (a finalize-only/construction-time defect, not reachable from
    ``delete()`` at all) are deliberately left uncaught here, so either one
    -- or any other current/future ``SourceStorageError`` subclass this
    audit did not classify as delete-path/operational -- surfaces as a
    genuine ``PipelineStep`` failure rather than being silently absorbed
    into ``UNRESOLVED``. An exception outside this hierarchy entirely (a
    programming defect) is untouched here and propagates as-is."""

    try:
        return await storage.delete(
            dataset_id=dataset_id, source_id=source_id, storage_uri=storage_uri
        )
    except (
        SourceStorageUnavailableError,
        InvalidSourceStorageUriError,
        UnsupportedStorageBackendError,
    ):
        return StorageDeleteResult(StorageDeleteStatus.UNRESOLVED)


async def _delete_one_source_storage(
    context: PipelineContext,
    storage: SourceObjectStorage,
    *,
    dataset_id: UUID,
    source_id: UUID,
) -> dict[str, Any] | None:
    async with PostgresUnitOfWork(context.session_factory) as uow:
        source = await uow.sources.get_by_id(source_id)
        snapshot = None if source is None else (source.status, source.storage_uri)
    if snapshot is None or snapshot[0] != SourceStatus.DELETING:
        return None
    result = await _delete_source_storage_result(
        storage, dataset_id=dataset_id, source_id=source_id, storage_uri=snapshot[1]
    )
    return {"source_id": str(source_id), "status": result.status.value}


async def _delete_dataset_storage(
    context: PipelineContext,
    storage: SourceObjectStorage,
    *,
    dataset_id: UUID,
) -> list[dict[str, Any]]:
    async with PostgresUnitOfWork(context.session_factory) as uow:
        sources = await uow.sources.list_for_dataset_not_deleted(dataset_id)
        snapshots = [(source.id, source.status, source.storage_uri) for source in sources]

    entries: list[dict[str, Any]] = []
    for source_id, status, storage_uri in snapshots:
        if status != SourceStatus.DELETING:
            continue
        result = await _delete_source_storage_result(
            storage, dataset_id=dataset_id, source_id=source_id, storage_uri=storage_uri
        )
        entries.append({"source_id": str(source_id), "status": result.status.value})
    return entries


# ---------------------------------------------------------------------------
# 4. finalize_target -- no external dependency, entirely in persist().
# ---------------------------------------------------------------------------


class FinalizeTargetStep:
    """ATOMIC: pure PostgreSQL, so the whole finalization sequence lives in
    ``persist``, using what steps 1 and 3 already durably observed."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        mutation_output = context.step_outputs.get(AUTHORITATIVE_MUTATION_STEP, {})
        storage_output = context.step_outputs.get(STORAGE_DELETION_STEP, {})
        memory_only = bool(context.run_input.get("memory_only", False))
        storage_status_by_source = _storage_status_by_source(storage_output)

        scope = mutation_output.get("scope")
        if scope == ForgetScope.SOURCE.value:
            counts = await _finalize_source_target(
                uow,
                mutation_output,
                memory_only=memory_only,
                storage_status_by_source=storage_status_by_source,
            )
            result.output.update(counts)
            return

        targets = mutation_output.get("targets", [])
        finalized_targets = [
            await _finalize_dataset_target(
                uow,
                target,
                memory_only=memory_only,
                storage_status_by_source=storage_status_by_source,
            )
            for target in targets
        ]
        result.output["targets"] = finalized_targets

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def _storage_status_by_source(storage_output: Mapping[str, Any]) -> dict[UUID, str]:
    """ADR-0011 D37 per-Source coverage: a dict keyed by ``source_id`` makes
    a duplicate entry structurally impossible to *store* silently -- this
    still detects one explicitly (rather than the later entry silently
    overwriting the earlier one) so a bookkeeping defect in the storage step
    itself is never masked."""

    by_source: dict[UUID, str] = {}
    for entry in storage_output.get("sources", []):
        source_id = UUID(str(entry["source_id"]))
        if source_id in by_source:
            raise PermanentPipelineStepError(
                FORGET_STORAGE_RESULT_DUPLICATE_ERROR_CODE, FORGET_STORAGE_RESULT_DUPLICATE_MESSAGE
            )
        by_source[source_id] = str(entry["status"])
    return by_source


def _require_storage_status(source_id: UUID, storage_status_by_source: Mapping[UUID, str]) -> str:
    """ADR-0011 D37: a missing per-Source storage result is an internal
    invariant failure, never interpreted as an implicit ``UNRESOLVED`` (or
    any other) outcome -- the Source must not be silently finalized based on
    missing evidence."""

    if source_id not in storage_status_by_source:
        raise PermanentPipelineStepError(
            FORGET_STORAGE_RESULT_MISSING_ERROR_CODE, FORGET_STORAGE_RESULT_MISSING_MESSAGE
        )
    return storage_status_by_source[source_id]


async def _finalize_source_target(
    uow: PostgresUnitOfWork,
    mutation_output: Mapping[str, Any],
    *,
    memory_only: bool,
    storage_status_by_source: Mapping[UUID, str],
) -> dict[str, Any]:
    if not mutation_output.get("proceed"):
        return {"source_status": None, "storage_deleted": False, "storage_status": None}
    source_id = UUID(str(mutation_output["source_id"]))
    source = await uow.sources.get_by_id(source_id)
    if source is None:
        raise PermanentPipelineStepError(
            FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
        )

    final_status = SourceStatus.PENDING if memory_only else SourceStatus.DELETED
    storage_status: str | None = None
    if source.status == SourceStatus.DELETING:
        if final_status == SourceStatus.DELETED:
            # A DELETING -> DELETED transition is exactly the case ADR-0011
            # D37's per-Source coverage requirement covers; memory_only's
            # DELETING -> PENDING transition never requests storage
            # deletion at all, so it is exempt (D37).
            storage_status = _require_storage_status(source_id, storage_status_by_source)
            if storage_status in _STORAGE_STATUSES_CLEARING_URI:
                source.storage_uri = None
            # UNRESOLVED (D39): storage_uri is preserved unchanged.
        source.status = final_status
    storage_deleted = storage_status in (
        _STORAGE_STATUS_DELETED_NOW,
        _STORAGE_STATUS_ALREADY_ABSENT,
    )
    return {
        "source_status": source.status.value,
        "storage_deleted": storage_deleted,
        "storage_status": storage_status,
    }


async def _finalize_dataset_target(
    uow: PostgresUnitOfWork,
    target: Mapping[str, Any],
    *,
    memory_only: bool,
    storage_status_by_source: Mapping[UUID, str],
) -> dict[str, Any]:
    dataset_id = UUID(str(target["dataset_id"]))
    if not target.get("proceed"):
        return {
            "dataset_id": str(dataset_id),
            "sources_affected": 0,
            "sources_pending": 0,
            "sources_deleted": 0,
        }

    dataset = await uow.datasets.get_by_id_for_update(dataset_id)
    if dataset is None:
        raise PermanentPipelineStepError(
            FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
        )
    sources = await uow.sources.list_for_dataset_for_update(dataset.id)
    final_status = SourceStatus.PENDING if memory_only else SourceStatus.DELETED
    sources_pending = 0
    sources_deleted = 0
    for source in sources:
        if source.status != SourceStatus.DELETING:
            continue
        if final_status == SourceStatus.DELETED:
            storage_status = _require_storage_status(source.id, storage_status_by_source)
            if storage_status in _STORAGE_STATUSES_CLEARING_URI:
                source.storage_uri = None
            sources_deleted += 1
        else:
            sources_pending += 1
        source.status = final_status
    # ADR-0010 D28/D42 defense-in-depth: re-evaluate administrative ownership
    # immediately before writing ACTIVE, even though target enumeration
    # (list_ids_for_everything_forget) already excludes an
    # administratively-owned DELETING dataset -- this guards the race
    # between that enumeration and this finalize step within the same run.
    # Forget must never resurrect an administrative tombstone.
    if dataset.status == DatasetStatus.DELETING and not (
        await uow.pipeline_runs.exists_administrative_delete_ownership(dataset.id)
    ):
        dataset.status = DatasetStatus.ACTIVE
    return {
        "dataset_id": str(dataset_id),
        "sources_affected": len(sources),
        "sources_pending": sources_pending,
        "sources_deleted": sources_deleted,
    }


# ---------------------------------------------------------------------------
# 5. finalize_result -- aggregate every prior step's safe output.
# ---------------------------------------------------------------------------


class FinalizeResultStep:
    """ATOMIC: pure aggregation of already-safe counts; writes
    ``run.metrics[<scope key>]`` in the engine's own transaction (mirrors
    Cognify/Improve's ``finalize_result``)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        mutation_output = context.step_outputs.get(AUTHORITATIVE_MUTATION_STEP, {})
        projection_output = context.step_outputs.get(PROJECTION_CONVERGENCE_STEP, {})
        storage_output = context.step_outputs.get(STORAGE_DELETION_STEP, {})
        finalize_output = context.step_outputs.get(FINALIZE_TARGET_STEP, {})
        memory_only = bool(context.run_input.get("memory_only", False))
        scope = mutation_output.get("scope")

        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                FORGET_TARGET_MISSING_ERROR_CODE, FORGET_TARGET_MISSING_MESSAGE
            )

        graph_events_processed = int(projection_output.get("graph_events_processed", 0) or 0)
        storage_deleted_count = int(storage_output.get("deleted_now", 0) or 0)
        storage_already_absent = int(storage_output.get("already_absent", 0) or 0)
        # ADR-0011 D39: storage_unresolved/storage_not_requested are tracked
        # as separate counters, never folded into storage_deleted/
        # storage_already_absent -- a PipelineRun may legitimately succeed
        # with storage_unresolved > 0 (D37's business-delete-must-converge
        # rule); storage_cleanup_complete is false whenever any cleanup
        # obligation in this run's scope remains unresolved.
        storage_unresolved_count = int(storage_output.get("unresolved", 0) or 0)
        storage_not_requested_count = int(storage_output.get("not_requested", 0) or 0)
        storage_cleanup_complete = storage_unresolved_count == 0

        if scope == ForgetScope.SOURCE.value:
            metric_key = FORGET_RESULT_METRIC_KEY
            run_result: dict[str, Any] = {
                "dataset_id": mutation_output.get("dataset_id"),
                "source_id": mutation_output.get("source_id"),
                "memory_only": memory_only,
                "source_status": finalize_output.get("source_status"),
                "documents_deactivated": int(mutation_output.get("documents_deactivated", 0) or 0),
                "chunks_deactivated": int(mutation_output.get("chunks_deactivated", 0) or 0),
                "summaries_deactivated": int(mutation_output.get("summaries_deactivated", 0) or 0),
                "entities_deactivated": int(mutation_output.get("entities_deactivated", 0) or 0),
                "relations_deactivated": int(mutation_output.get("relations_deactivated", 0) or 0),
                "entity_mentions_unprojected": int(
                    mutation_output.get("entity_mentions_unprojected", 0) or 0
                ),
                "relation_evidence_unprojected": int(
                    mutation_output.get("relation_evidence_unprojected", 0) or 0
                ),
                "graph_events_enqueued": int(mutation_output.get("graph_events_enqueued", 0) or 0),
                "graph_events_processed": graph_events_processed,
                "storage_deleted": bool(finalize_output.get("storage_deleted", False)),
                "storage_status": finalize_output.get("storage_status"),
                "storage_unresolved": storage_unresolved_count,
                "storage_not_requested": storage_not_requested_count,
                "storage_cleanup_complete": storage_cleanup_complete,
            }
            run.dataset_id = UUID(str(mutation_output["dataset_id"]))
            run.source_id = UUID(str(mutation_output["source_id"]))
        elif scope == ForgetScope.DATASET.value:
            metric_key = FORGET_DATASET_RESULT_METRIC_KEY
            targets = mutation_output.get("targets", [])
            finalize_targets = finalize_output.get("targets", [])
            target = targets[0] if targets else {}
            finalize_target = finalize_targets[0] if finalize_targets else {}
            run_result = {
                "dataset_id": target.get("dataset_id"),
                "memory_only": memory_only,
                "sources_affected": int(finalize_target.get("sources_affected", 0) or 0),
                "sources_pending": int(finalize_target.get("sources_pending", 0) or 0),
                "sources_deleted": int(finalize_target.get("sources_deleted", 0) or 0),
                "documents_deactivated": int(target.get("documents_deactivated", 0) or 0),
                "chunks_deactivated": int(target.get("chunks_deactivated", 0) or 0),
                "summaries_deactivated": int(target.get("summaries_deactivated", 0) or 0),
                "entities_deactivated": int(target.get("entities_deactivated", 0) or 0),
                "relations_deactivated": int(target.get("relations_deactivated", 0) or 0),
                "entity_mentions_unprojected": int(
                    target.get("entity_mentions_unprojected", 0) or 0
                ),
                "relation_evidence_unprojected": int(
                    target.get("relation_evidence_unprojected", 0) or 0
                ),
                "graph_events_enqueued": int(target.get("graph_events_enqueued", 0) or 0),
                "graph_events_processed": graph_events_processed,
                "storage_deleted": storage_deleted_count,
                "storage_already_absent": storage_already_absent,
                "storage_unresolved": storage_unresolved_count,
                "storage_not_requested": storage_not_requested_count,
                "storage_cleanup_complete": storage_cleanup_complete,
            }
            if target.get("dataset_id"):
                run.dataset_id = UUID(str(target["dataset_id"]))
        else:
            metric_key = FORGET_EVERYTHING_RESULT_METRIC_KEY
            targets = mutation_output.get("targets", [])
            finalize_targets = {
                entry["dataset_id"]: entry for entry in finalize_output.get("targets", [])
            }
            run_result = {
                "datasets_affected": len(targets),
                "sources_affected": sum(
                    int(finalize_targets.get(t["dataset_id"], {}).get("sources_affected", 0) or 0)
                    for t in targets
                ),
                "sources_pending": 0,
                "sources_deleted": sum(
                    int(finalize_targets.get(t["dataset_id"], {}).get("sources_deleted", 0) or 0)
                    for t in targets
                ),
                "documents_deactivated": sum(
                    int(t.get("documents_deactivated", 0) or 0) for t in targets
                ),
                "chunks_deactivated": sum(
                    int(t.get("chunks_deactivated", 0) or 0) for t in targets
                ),
                "summaries_deactivated": sum(
                    int(t.get("summaries_deactivated", 0) or 0) for t in targets
                ),
                "entities_deactivated": sum(
                    int(t.get("entities_deactivated", 0) or 0) for t in targets
                ),
                "relations_deactivated": sum(
                    int(t.get("relations_deactivated", 0) or 0) for t in targets
                ),
                "entity_mentions_unprojected": sum(
                    int(t.get("entity_mentions_unprojected", 0) or 0) for t in targets
                ),
                "relation_evidence_unprojected": sum(
                    int(t.get("relation_evidence_unprojected", 0) or 0) for t in targets
                ),
                "graph_events_enqueued": sum(
                    int(t.get("graph_events_enqueued", 0) or 0) for t in targets
                ),
                "graph_events_processed": graph_events_processed,
                "storage_deleted": storage_deleted_count,
                "storage_already_absent": storage_already_absent,
                "storage_unresolved": storage_unresolved_count,
                "storage_not_requested": storage_not_requested_count,
                "storage_cleanup_complete": storage_cleanup_complete,
            }

        run.metrics = {**run.metrics, metric_key: run_result}

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def build_forget_pipeline_definition() -> PipelineDefinition:
    """The single registered Forget pipeline (SM-512)."""

    def step_def(
        name: str,
        step: Any,
        *,
        recovery: CancellationRecoveryMode = CancellationRecoveryMode.ATOMIC,
    ) -> PipelineStepDefinition:
        return PipelineStepDefinition(
            name=name,
            definition_id=f"{_DEFINITION_ID_PREFIX}{name}.v1",
            step=step,
            input_deriver=_uniform_input,
            cancellation_recovery_mode=recovery,
        )

    return PipelineDefinition(
        pipeline_type=PipelineType.FORGET,
        steps=(
            step_def(AUTHORITATIVE_MUTATION_STEP, AuthoritativeMutationStep()),
            step_def(PROJECTION_CONVERGENCE_STEP, ProjectionConvergenceStep()),
            step_def(
                STORAGE_DELETION_STEP,
                StorageDeletionStep(),
                recovery=CancellationRecoveryMode.AMBIGUOUS,
            ),
            step_def(FINALIZE_TARGET_STEP, FinalizeTargetStep()),
            step_def(FINALIZE_RESULT_STEP, FinalizeResultStep()),
        ),
    )


__all__ = [
    "AUTHORITATIVE_MUTATION_STEP",
    "FINALIZE_RESULT_STEP",
    "FINALIZE_TARGET_STEP",
    "FORGET_RESOURCES_RESOURCE",
    "PROJECTION_CONVERGENCE_STEP",
    "STORAGE_DELETION_STEP",
    "AuthoritativeMutationStep",
    "FinalizeResultStep",
    "FinalizeTargetStep",
    "ForgetPipelineResources",
    "ProjectionConvergenceStep",
    "StorageDeletionStep",
    "build_forget_pipeline_definition",
]
