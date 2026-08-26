"""Dataset administrative delete pipeline steps (SM-515, ADR-0010 D9).

Five fixed steps, always present, always executed in this order, for every
Dataset (empty or populated alike -- ADR-0010 D8):

1. ``begin_delete`` -- PostgreSQL-only/ATOMIC. Locks the Dataset, transitions
   ``ACTIVE -> DELETING`` (idempotent when already ``DELETING`` -- this run's
   own administrative lineage retrying), and administratively cancels other
   still-``QUEUED`` incompatible dataset-scoped runs (ADR-0010 D14).
2. ``deactivate_authoritative`` -- PostgreSQL-only/ATOMIC. Reuses Forget's
   already-audited ``apply_dataset_forget_mutation`` primitive over the whole
   Dataset (never Forget's public lifecycle/finalizer, ADR-0010 D1/D25).
3. ``converge_projection`` -- external (Neo4j via outbox), RECONCILABLE.
   Drains the ``graph_outbox`` rows step 2 enqueued.
4. ``delete_storage`` -- external (filesystem), AMBIGUOUS. Removes every
   Source's original storage for this Dataset.
5. ``finalize_tombstone`` -- PostgreSQL-only/ATOMIC. Sets every ``Source`` to
   its terminal ``DELETED`` state, clears confirmed-deleted ``storage_uri``,
   transitions ``Dataset.status: DELETING -> DELETED``, and persists final
   counters.

No nested Forget ``PipelineRun`` is ever created (ADR-0010 D9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sofias_memory.domain import (
    DatasetStatus,
    GraphOutboxStatus,
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError, RetryablePipelineStepError
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    CancellationRecoveryOutcome,
    PipelineCancellationRecoveryContext,
    PipelineDefinition,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
    no_op_persist,
)
from sofias_memory.services.forget import apply_dataset_forget_mutation, delete_source_storage
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
from sofias_memory.services.pipeline_lifecycle import transition_run, transition_step

DATASET_DELETE_RESOURCES_RESOURCE = "dataset_delete_resources"

BEGIN_DELETE_STEP = "begin_delete"
DEACTIVATE_AUTHORITATIVE_STEP = "deactivate_authoritative"
CONVERGE_PROJECTION_STEP = "converge_projection"
DELETE_STORAGE_STEP = "delete_storage"
FINALIZE_TOMBSTONE_STEP = "finalize_tombstone"

DATASET_DELETE_RESULT_METRIC_KEY = "dataset_delete_result"

_DEFINITION_ID_PREFIX = "dataset_delete."

DATASET_DELETE_TARGET_MISSING_ERROR_CODE = "DATASET_DELETE_TARGET_MISSING"
DATASET_DELETE_TARGET_MISSING_MESSAGE = "Dataset no longer exists or is in an unexpected state."
DATASET_DELETE_RESOURCE_MISSING_ERROR_CODE = "DATASET_DELETE_RESOURCE_MISSING"
DATASET_DELETE_RESOURCE_MISSING_MESSAGE = "Dataset delete processing resources are not configured."
DATASET_DELETE_DEPENDENCY_ERROR_CODE = "DATASET_DELETE_DEPENDENCY_UNAVAILABLE"
DATASET_DELETE_DEPENDENCY_ERROR_MESSAGE = "A Dataset delete dependency was unavailable."
DATASET_DELETE_PROJECTION_NOT_CONVERGED_ERROR_CODE = "DATASET_DELETE_PROJECTION_NOT_CONVERGED"
DATASET_DELETE_PROJECTION_NOT_CONVERGED_MESSAGE = (
    "Graph projection for this administrative delete could not converge."
)


@dataclass(frozen=True)
class DatasetDeletePipelineResources:
    """Everything a Dataset delete step needs, built once per process
    (mirrors ``ForgetPipelineResources``, SM-512 -- same shape, distinct
    resource key so each pipeline's wiring stays independently traceable)."""

    settings: Any
    graph_outbox_drain: GraphOutboxBatchProcessor | None


def _resources(context: PipelineContext) -> DatasetDeletePipelineResources:
    resource = context.resources.get(DATASET_DELETE_RESOURCES_RESOURCE)
    if resource is None:
        raise PermanentPipelineStepError(
            DATASET_DELETE_RESOURCE_MISSING_ERROR_CODE, DATASET_DELETE_RESOURCE_MISSING_MESSAGE
        )
    return cast(DatasetDeletePipelineResources, resource)


def _dataset_id(context: PipelineContext) -> UUID:
    if context.dataset_id is None:
        raise PermanentPipelineStepError(
            DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
        )
    return context.dataset_id


def _uniform_input(run_input: Any, step_outputs: Any) -> Any:
    """Every step's semantic input is derivable purely from ``run_input``
    (the Dataset's own id) -- identical pattern to Forget (SM-512)."""

    del step_outputs
    return dict(run_input)


# ---------------------------------------------------------------------------
# 1. begin_delete -- PostgreSQL-only/ATOMIC.
# ---------------------------------------------------------------------------


class BeginDeleteStep:
    """ATOMIC: zero external dependency -- the Dataset transition and the
    administrative cancellation of other queued work are both pure
    PostgreSQL, so the whole sequence lives in ``persist`` (ADR-0010 D9)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        dataset = await uow.datasets.get_by_id_for_update(dataset_id)
        if dataset is None:
            raise PermanentPipelineStepError(
                DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
            )

        if dataset.status == DatasetStatus.ACTIVE:
            dataset.status = DatasetStatus.DELETING
        elif dataset.status == DatasetStatus.DELETING:
            # ADR-0010 D20: this run's own administrative retry lineage
            # re-affirming a state it (or its parent attempt) already
            # produced -- idempotent, not a fresh transition.
            pass
        else:
            # DatasetStatus.DELETED is never a legitimate observation here:
            # the submission layer never creates/retries a DATASET_DELETE
            # run against an already-DELETED dataset. Fail loudly rather
            # than silently reinterpreting an impossible state (ADR-0010 D15
            # / "fail safe otherwise").
            raise PermanentPipelineStepError(
                DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
            )

        now = await uow.pipeline_runs.get_database_now()
        incompatible = await uow.pipeline_runs.list_incompatible_queued_for_dataset_for_update(
            dataset_id=dataset_id, exclude_run_id=context.run_id
        )
        cancelled_run_ids: list[str] = []
        for run in incompatible:
            steps = await uow.pipeline_steps.list_for_run(run.id)
            for step in steps:
                if step.status == PipelineStepStatus.QUEUED:
                    transition_step(step, PipelineStepStatus.CANCELLED, now=now)
            transition_run(run, PipelineRunStatus.CANCELLED, now=now)
            cancelled_run_ids.append(str(run.id))

        result.output.update(
            {"dataset_id": str(dataset_id), "cancelled_run_ids": cancelled_run_ids}
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 2. deactivate_authoritative -- PostgreSQL-only/ATOMIC.
# ---------------------------------------------------------------------------


class DeactivateAuthoritativeStep:
    """ATOMIC: reuses Forget's ``apply_dataset_forget_mutation`` (SM-512)
    unchanged -- pure PostgreSQL, naturally idempotent against already
    ``is_active=False``/already-``DELETING`` state on a fresh retry attempt
    (ADR-0010 D25/D30's business-idempotence requirement)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        dataset = await uow.datasets.get_by_id_for_update(dataset_id)
        if dataset is None or dataset.status != DatasetStatus.DELETING:
            raise PermanentPipelineStepError(
                DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
            )
        sources = await uow.sources.list_for_dataset_for_update(dataset.id)
        for source in sources:
            if source.status != SourceStatus.DELETED:
                source.status = SourceStatus.DELETING

        part = await apply_dataset_forget_mutation(
            cast(Any, uow), dataset=dataset, sources=sources, memory_only=False
        )
        result.output.update(
            {
                "dataset_id": str(dataset_id),
                "sources_touched": part.sources_touched,
                "documents_deactivated": part.documents_deactivated,
                "chunks_deactivated": part.chunks_deactivated,
                "summaries_deactivated": part.summaries_deactivated,
                "entities_deactivated": part.entities_deactivated,
                "relations_deactivated": part.relations_deactivated,
                "graph_events_enqueued": part.graph_events_enqueued,
                # ADR-0010 Finding 2: the exact graph_outbox row ids this
                # attempt enqueued -- converge_projection must prove these
                # SPECIFIC rows reached DONE, never merely that PostgreSQL's
                # claimable-backlog query has nothing left for the dataset
                # (which a permanently FAILED-at-ceiling row is invisible to).
                "graph_outbox_ids": list(part.graph_outbox_ids),
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 3. converge_projection -- external/RECONCILABLE.
# ---------------------------------------------------------------------------


class ConvergeProjectionStep:
    """RECONCILABLE (ADR-0010 D9): no authoritative PostgreSQL mutation of
    its own -- draining is independently idempotent (SM-506 leasing), and a
    stale orphaned attempt's safety is provable purely from durable
    ``graph_outbox`` state (see :func:`converge_projection_reconcile`)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        deactivate_output = context.step_outputs.get(DEACTIVATE_AUTHORITATIVE_STEP, {})
        outbox_ids = [int(value) for value in deactivate_output.get("graph_outbox_ids", [])]
        if not outbox_ids:
            # Nothing this attempt's own deactivation enqueued -- vacuously
            # converged, regardless of whether Neo4j is configured at all
            # (matches Forget/Improve's own documented degrade, ADR-0010
            # Finding 2: there is nothing here that could be falsely
            # reported as converged).
            return StepResult(output={"graph_events_processed": 0})

        resources = _resources(context)
        if resources.graph_outbox_drain is None:
            # Relevant rows exist but there is no way to drain them --
            # unlike the empty case above, this is a real, retryable
            # dependency gap, never silently reported as "0 processed"
            # (ADR-0010 Finding 2).
            raise RetryablePipelineStepError(
                DATASET_DELETE_DEPENDENCY_ERROR_CODE, DATASET_DELETE_DEPENDENCY_ERROR_MESSAGE
            )
        await resources.graph_outbox_drain.process_dataset(dataset_id)

        async with PostgresUnitOfWork(context.session_factory) as uow:
            statuses = await uow.graph_outbox.list_status_by_ids(outbox_ids)

        if _has_failed_at_ceiling(outbox_ids, statuses):
            # ADR-0010 Finding 2's central invariant: a permanently failed
            # relevant row must never let this step (and therefore
            # finalize_tombstone) succeed. Permanent -- retrying the exact
            # same drain will not change a row already at the attempt
            # ceiling; recovery is manual (a fresh DATASET_DELETE retry
            # redoes deactivate_authoritative, producing fresh rows).
            raise PermanentPipelineStepError(
                DATASET_DELETE_PROJECTION_NOT_CONVERGED_ERROR_CODE,
                DATASET_DELETE_PROJECTION_NOT_CONVERGED_MESSAGE,
            )
        if _has_unfinished(outbox_ids, statuses):
            # Still PENDING/PROCESSING -- possibly under another live lease
            # (SM-506 claim-or-observe). Never fabricate success; let the
            # engine's own retry policy observe again later.
            raise RetryablePipelineStepError(
                DATASET_DELETE_DEPENDENCY_ERROR_CODE, DATASET_DELETE_DEPENDENCY_ERROR_MESSAGE
            )
        return StepResult(output={"graph_events_processed": len(outbox_ids)})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def _has_failed_at_ceiling(
    outbox_ids: list[int], statuses: dict[int, tuple[GraphOutboxStatus, int]]
) -> bool:
    return any(
        statuses.get(outbox_id, (None, 0))[0] == GraphOutboxStatus.FAILED
        and statuses.get(outbox_id, (None, 0))[1] >= DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
        for outbox_id in outbox_ids
    )


def _has_unfinished(
    outbox_ids: list[int], statuses: dict[int, tuple[GraphOutboxStatus, int]]
) -> bool:
    return any(
        statuses.get(outbox_id, (None, 0))[0] != GraphOutboxStatus.DONE for outbox_id in outbox_ids
    )


async def converge_projection_reconcile(
    context: PipelineCancellationRecoveryContext, uow: PostgresUnitOfWork
) -> CancellationRecoveryOutcome:
    """SM-507/ADR-0010 D9 (Finding 2): an orphaned ``RUNNING`` attempt of
    this step is safe to report ``CANCELLED`` if and only if the EXACT
    ``graph_outbox`` rows this run's own ``deactivate_authoritative`` step
    enqueued are all durably ``DONE`` -- never inferred from "nothing
    processable remains" (a permanently FAILED-at-ceiling row would be
    invisible to that query). A FAILED-at-ceiling or still-pending row is
    treated the same as "not proven safe" here: recovery only ever needs to
    decide CANCELLED-vs-fail-safe for the run being cancelled, and the exact
    distinction between those two unsafe cases matters only to a future
    manual retry, not to this decision. Read-only, PostgreSQL-only, never
    Neo4j/filesystem."""

    if context.dataset_id is None:
        return CancellationRecoveryOutcome.INCONCLUSIVE
    steps = await uow.pipeline_steps.list_for_run(context.run_id)
    deactivate_step = next(
        (step for step in steps if step.name == DEACTIVATE_AUTHORITATIVE_STEP), None
    )
    if deactivate_step is None or deactivate_step.status != PipelineStepStatus.SUCCEEDED:
        # deactivate_authoritative itself never completed -- nothing was
        # ever durably enqueued by this attempt, so there is nothing for
        # projection convergence to be unsafe about.
        return CancellationRecoveryOutcome.SAFE
    raw_outbox_ids = cast("list[Any]", deactivate_step.output.get("graph_outbox_ids", []))
    outbox_ids = [int(value) for value in raw_outbox_ids]
    if not outbox_ids:
        return CancellationRecoveryOutcome.SAFE
    statuses = await uow.graph_outbox.list_status_by_ids(outbox_ids)
    if _has_unfinished(outbox_ids, statuses):
        return CancellationRecoveryOutcome.INCONCLUSIVE
    return CancellationRecoveryOutcome.SAFE


# ---------------------------------------------------------------------------
# 4. delete_storage -- external filesystem, AMBIGUOUS.
# ---------------------------------------------------------------------------


class DeleteStorageStep:
    """AMBIGUOUS (ADR-0010 D9/D26): identical justification to Forget's own
    ``StorageDeletionStep`` -- a PostgreSQL-only reconciliation callback
    cannot prove whether an orphaned attempt already unlinked a file before
    crashing. The Dataset stays ``DELETING``; a later retry safely
    re-observes/deletes (``delete_source_storage`` is idempotent)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        dataset_id = _dataset_id(context)
        resources = _resources(context)
        entries: list[dict[str, Any]] = []
        async with PostgresUnitOfWork(context.session_factory) as uow:
            sources = await uow.sources.list_for_dataset_not_deleted(dataset_id)
            snapshots = [(source.id, source.status, source.storage_uri) for source in sources]
        try:
            for source_id, status, storage_uri in snapshots:
                if status != SourceStatus.DELETING:
                    continue
                delete_result = delete_source_storage(
                    resources.settings.data_directory,
                    dataset_id=dataset_id,
                    source_id=source_id,
                    storage_uri=storage_uri,
                )
                entries.append({"source_id": str(source_id), "status": delete_result.status.value})
        except OSError as exc:  # pragma: no cover - delete_source_storage already wraps
            raise RetryablePipelineStepError(
                DATASET_DELETE_DEPENDENCY_ERROR_CODE, DATASET_DELETE_DEPENDENCY_ERROR_MESSAGE
            ) from exc

        deleted_now = sum(1 for entry in entries if entry["status"] == "deleted_now")
        already_absent = sum(1 for entry in entries if entry["status"] == "already_absent")
        return StepResult(
            output={
                "sources": entries,
                "deleted_now": deleted_now,
                "already_absent": already_absent,
            }
        )

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        no_op_persist(context, result, uow)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 5. finalize_tombstone -- PostgreSQL-only/ATOMIC.
# ---------------------------------------------------------------------------


class FinalizeTombstoneStep:
    """ATOMIC: pure PostgreSQL, so the whole finalization sequence --
    per-Source terminal state, the ``Dataset`` tombstone transition, and
    final counters -- lives in ``persist`` (ADR-0010 D9/D24)."""

    async def execute(self, context: PipelineContext) -> StepResult:
        return StepResult(output={})

    async def persist(
        self, context: PipelineContext, result: StepResult, uow: PostgresUnitOfWork
    ) -> None:
        dataset_id = _dataset_id(context)
        dataset = await uow.datasets.get_by_id_for_update(dataset_id)
        if dataset is None:
            raise PermanentPipelineStepError(
                DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
            )

        deactivate_output = context.step_outputs.get(DEACTIVATE_AUTHORITATIVE_STEP, {})
        projection_output = context.step_outputs.get(CONVERGE_PROJECTION_STEP, {})
        storage_output = context.step_outputs.get(DELETE_STORAGE_STEP, {})
        storage_status_by_source = {
            UUID(str(entry["source_id"])): entry["status"]
            for entry in storage_output.get("sources", [])
        }

        sources = await uow.sources.list_for_dataset_for_update(dataset.id)
        sources_deleted = 0
        for source in sources:
            if source.status != SourceStatus.DELETING:
                continue
            source.status = SourceStatus.DELETED
            sources_deleted += 1
            storage_status = storage_status_by_source.get(source.id)
            if storage_status in ("deleted_now", "already_absent"):
                source.storage_uri = None

        if dataset.status == DatasetStatus.DELETING:
            # ADR-0010 D10/Case G: never re-applies the destructive effect on
            # an idempotent re-run once already terminal -- this branch only
            # ever fires once per lineage's successful completion.
            dataset.status = DatasetStatus.DELETED

        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                DATASET_DELETE_TARGET_MISSING_ERROR_CODE, DATASET_DELETE_TARGET_MISSING_MESSAGE
            )
        run_result = {
            "dataset_id": str(dataset_id),
            "sources_deleted": sources_deleted,
            "documents_deactivated": int(deactivate_output.get("documents_deactivated", 0) or 0),
            "chunks_deactivated": int(deactivate_output.get("chunks_deactivated", 0) or 0),
            "summaries_deactivated": int(deactivate_output.get("summaries_deactivated", 0) or 0),
            "entities_deactivated": int(deactivate_output.get("entities_deactivated", 0) or 0),
            "relations_deactivated": int(deactivate_output.get("relations_deactivated", 0) or 0),
            "graph_events_enqueued": int(deactivate_output.get("graph_events_enqueued", 0) or 0),
            "graph_events_processed": int(projection_output.get("graph_events_processed", 0) or 0),
            "storage_deleted": int(storage_output.get("deleted_now", 0) or 0),
            "storage_already_absent": int(storage_output.get("already_absent", 0) or 0),
        }
        run.metrics = {**run.metrics, DATASET_DELETE_RESULT_METRIC_KEY: run_result}
        result.output.update(run_result)

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


def build_dataset_delete_pipeline_definition() -> PipelineDefinition:
    """The single registered Dataset delete pipeline (SM-515, ADR-0010 D9)."""

    def step_def(
        name: str,
        step: Any,
        *,
        recovery: CancellationRecoveryMode = CancellationRecoveryMode.ATOMIC,
        reconcile: Any = None,
    ) -> PipelineStepDefinition:
        return PipelineStepDefinition(
            name=name,
            definition_id=f"{_DEFINITION_ID_PREFIX}{name}.v1",
            step=step,
            input_deriver=_uniform_input,
            cancellation_recovery_mode=recovery,
            cancellation_reconcile=reconcile,
        )

    return PipelineDefinition(
        pipeline_type=PipelineType.DATASET_DELETE,
        steps=(
            step_def(BEGIN_DELETE_STEP, BeginDeleteStep()),
            step_def(DEACTIVATE_AUTHORITATIVE_STEP, DeactivateAuthoritativeStep()),
            step_def(
                CONVERGE_PROJECTION_STEP,
                ConvergeProjectionStep(),
                recovery=CancellationRecoveryMode.RECONCILABLE,
                reconcile=converge_projection_reconcile,
            ),
            step_def(
                DELETE_STORAGE_STEP,
                DeleteStorageStep(),
                recovery=CancellationRecoveryMode.AMBIGUOUS,
            ),
            step_def(FINALIZE_TOMBSTONE_STEP, FinalizeTombstoneStep()),
        ),
    )


__all__ = [
    "BEGIN_DELETE_STEP",
    "CONVERGE_PROJECTION_STEP",
    "DATASET_DELETE_RESOURCES_RESOURCE",
    "DATASET_DELETE_RESULT_METRIC_KEY",
    "DEACTIVATE_AUTHORITATIVE_STEP",
    "DELETE_STORAGE_STEP",
    "FINALIZE_TOMBSTONE_STEP",
    "BeginDeleteStep",
    "ConvergeProjectionStep",
    "DatasetDeletePipelineResources",
    "DeactivateAuthoritativeStep",
    "DeleteStorageStep",
    "FinalizeTombstoneStep",
    "build_dataset_delete_pipeline_definition",
    "converge_projection_reconcile",
]
