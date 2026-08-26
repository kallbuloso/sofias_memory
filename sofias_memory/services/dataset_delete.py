"""Administrative Dataset deletion control surface (SM-515, ADR-0010).

``DELETE /api/v1/datasets/{dataset_id}`` orchestration only: this module
never executes pipeline business logic (that is
``pipelines.steps.dataset_delete``) -- it resolves/creates a durable
``DATASET_DELETE`` ``PipelineRun`` through the same ``create_run_with_steps``
primitive every other B5 pipeline submission uses, under a Dataset-row lock
that makes concurrent-DELETE convergence and the D21 repeated-DELETE state
machine PostgreSQL-authoritative without a permanent per-dataset
idempotency key (ADR-0010 D6/D21 -- deliberately avoided, SM-515 "CRÍTICO —
NÃO USAR UMA KEY PERMANENTE").

This is deliberately NOT routed through ``PipelineSubmissionService.submit``:
that service's idempotency-key resolution model does not fit D21's
status-based state machine (ACTIVE/DELETING/DELETED, not "same key = same
run"), and a permanent key would wrongly resolve a fresh, legitimate DELETE
request to a long-cancelled earlier one. It DOES reuse
``create_run_with_steps``/the registry's ``build_step_plan`` -- the same
primitives ``PipelineSubmissionService`` itself uses -- so there is still
only one way a ``DATASET_DELETE`` run is ever materialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.hashing import canonical_work_payload_hash
from sofias_memory.pipelines.registry import PipelineRegistry
from sofias_memory.pipelines.steps.dataset_delete import DATASET_DELETE_RESULT_METRIC_KEY
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.datasets import DatasetDeleteCounters, DatasetDeleteResult
from sofias_memory.services.datasets import DEFAULT_DATASET_SLUG
from sofias_memory.services.pipeline_lifecycle import create_run_with_steps
from sofias_memory.services.pipeline_submission import WorkerAvailability, worker_disabled_error


def dataset_not_found_error(dataset_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Dataset does not exist.",
        details={"dataset_id": str(dataset_id)},
    )


def main_dataset_delete_forbidden_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.MAIN_DATASET_DELETE_FORBIDDEN,
        status_code=HTTPStatus.CONFLICT,
        message="The 'main' dataset cannot be administratively deleted.",
        details={"slug": DEFAULT_DATASET_SLUG},
    )


def dataset_deleting_awaiting_retry_error(
    dataset_id: UUID, *, run_id: UUID | None
) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.DATASET_DELETING,
        status_code=HTTPStatus.CONFLICT,
        message=(
            "This dataset's administrative delete did not complete and requires a "
            "manual retry (POST /api/v1/runs/{run_id}/retry)."
        ),
        details={"dataset_id": str(dataset_id), "run_id": str(run_id) if run_id else None},
    )


@dataclass(frozen=True, slots=True)
class DatasetDeleteControlResult:
    """What a DELETE request resolved to -- a public projection plus the
    HTTP status the route should use (mirrors ``run_control.ControlResult``,
    SM-514)."""

    result: DatasetDeleteResult
    http_status: int


def _counters_from_metrics(run: PipelineRun) -> DatasetDeleteCounters | None:
    if run.status != PipelineRunStatus.SUCCEEDED:
        return None
    metric = run.metrics.get(DATASET_DELETE_RESULT_METRIC_KEY)
    if not isinstance(metric, dict):
        return None
    return DatasetDeleteCounters(
        sources_deleted=int(metric.get("sources_deleted", 0) or 0),
        documents_deactivated=int(metric.get("documents_deactivated", 0) or 0),
        chunks_deactivated=int(metric.get("chunks_deactivated", 0) or 0),
        entities_deactivated=int(metric.get("entities_deactivated", 0) or 0),
        relations_deactivated=int(metric.get("relations_deactivated", 0) or 0),
        summaries_deactivated=int(metric.get("summaries_deactivated", 0) or 0),
        storage_deleted=int(metric.get("storage_deleted", 0) or 0),
        storage_already_absent=int(metric.get("storage_already_absent", 0) or 0),
        graph_events_processed=int(metric.get("graph_events_processed", 0) or 0),
    )


def _run_result(run: PipelineRun) -> DatasetDeleteResult:
    # ADR-0010 D2: DATASET_DELETE is always dataset-scoped, never global --
    # dataset_id is never NULL on a run this module ever creates or resolves.
    assert run.dataset_id is not None  # noqa: S101
    return DatasetDeleteResult(
        run_id=run.id,
        dataset_id=run.dataset_id,
        status=run.status,
        counters=_counters_from_metrics(run),
    )


class DatasetDeleteService:
    """Cancel-free, worker-gated resolve-or-create control surface for
    administrative Dataset deletion (ADR-0010 D21/D23)."""

    def __init__(
        self,
        *,
        registry: PipelineRegistry,
        worker: WorkerAvailability,
        settings: Settings,
        session_factory: AsyncSessionFactory,
    ) -> None:
        self._registry = registry
        self._worker = worker
        self._settings = settings
        self._session_factory = session_factory

    async def request_delete(self, dataset_id: UUID) -> DatasetDeleteControlResult:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            # ADR-0010 D8/D21: the Dataset row lock, held for the whole
            # decision + (conditionally) the new run's own creation, is what
            # makes two concurrent DELETE requests converge to exactly one
            # initial deletion lineage -- the second caller blocks here until
            # the first commits, then observes the just-created nonterminal
            # run instead of racing to create a second one.
            dataset = await uow.datasets.get_by_id_for_update(dataset_id)
            if dataset is None:
                raise dataset_not_found_error(dataset_id)
            if dataset.slug == DEFAULT_DATASET_SLUG:
                raise main_dataset_delete_forbidden_error()

            if dataset.status == DatasetStatus.DELETED:
                latest = await uow.pipeline_runs.find_latest_dataset_delete_for_dataset(dataset_id)
                assert latest is not None  # noqa: S101 - only the pipeline ever sets DELETED
                return DatasetDeleteControlResult(
                    result=_run_result(latest), http_status=int(HTTPStatus.OK)
                )

            find_nonterminal = (
                uow.pipeline_runs.find_nonterminal_dataset_delete_for_dataset_for_update
            )
            existing = await find_nonterminal(dataset_id)
            if existing is not None:
                return DatasetDeleteControlResult(
                    result=_run_result(existing), http_status=int(HTTPStatus.ACCEPTED)
                )

            if dataset.status == DatasetStatus.DELETING:
                # No nonterminal lineage exists (checked above): the owning
                # DATASET_DELETE run is terminal FAILED/CANCELLED, awaiting
                # manual retry (ADR-0010 D18/D21).
                latest = await uow.pipeline_runs.find_latest_dataset_delete_for_dataset(dataset_id)
                raise dataset_deleting_awaiting_retry_error(
                    dataset_id, run_id=latest.id if latest is not None else None
                )

            # ACTIVE, no nonterminal DATASET_DELETE -> a brand-new deletion
            # lineage is required. Only a NEW run needs the worker
            # (ADR-0010 D23); every branch above observes existing/terminal
            # state and works without one.
            if not (self._worker.enabled and self._worker.is_running):
                raise worker_disabled_error()

            work_input = {"dataset_id": str(dataset_id)}
            step_plan = self._registry.build_step_plan(
                PipelineType.DATASET_DELETE, run_input=work_input
            )
            run = await create_run_with_steps(
                uow,
                pipeline_type=PipelineType.DATASET_DELETE,
                dataset_id=dataset_id,
                source_id=None,
                idempotency_key=None,
                payload_hash=canonical_work_payload_hash(work_input),
                input=work_input,
                config_fingerprint=self._settings.config_fingerprint(),
                steps=step_plan,
            )
            new_run_id = run.id
            await uow.commit()

        # A fresh, independent read (mirrors PipelineSubmissionService's own
        # SM-509 Part L pattern): the object just committed above is
        # detached once its owning session closes at the end of the `async
        # with` block, so its attributes are never read after that point --
        # this also correctly reports RUNNING rather than a stale QUEUED if
        # the worker has already claimed it by the time this line runs.
        async with PostgresUnitOfWork(self._session_factory) as fresh_uow:
            fresh_run = await fresh_uow.pipeline_runs.get_by_id(new_run_id)
            assert fresh_run is not None  # noqa: S101 - just committed, cannot vanish
            return DatasetDeleteControlResult(
                result=_run_result(fresh_run), http_status=int(HTTPStatus.ACCEPTED)
            )


__all__ = [
    "DatasetDeleteControlResult",
    "DatasetDeleteService",
    "dataset_deleting_awaiting_retry_error",
    "dataset_not_found_error",
    "main_dataset_delete_forbidden_error",
]
