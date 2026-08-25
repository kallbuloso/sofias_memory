"""Run cancellation and manual-retry control surface (SM-514, ADR-0009).

``RunService`` (SM-508) stays strictly read-only; this module is the only
place that mutates ``PipelineRun``/``PipelineStep`` lifecycle state outside
the engine (cancel) or creates a new ``PipelineRun`` outside a public write
route's own submission (manual retry). Neither operation executes business
pipeline code: cancel is a pure, guarded status transition (mirroring what
``PipelineEngine`` already does at its own checkpoints); retry is a durable
resubmission of the original's own persisted work, through the same shared
B5 submission contract every write route already uses -- via a narrow
internal-trust entry point (``submit_trusted_internal``) rather than a
second engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    TERMINAL_RUN_STATUSES,
    PipelineRunStatus,
    PipelineStepStatus,
    PipelineType,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.registry import PipelineRegistry
from sofias_memory.schemas.common import ErrorCode, JSONValue
from sofias_memory.schemas.runs import RunDetailResult
from sofias_memory.services.pipeline_lifecycle import transition_run, transition_step
from sofias_memory.services.pipeline_submission import (
    PipelineSubmissionService,
    PreparationHook,
    SubmissionTargets,
    SubmissionUnitOfWork,
    WorkerAvailability,
)
from sofias_memory.services.remember import delete_ingress_artifact, prepare_remember_retry_ingress
from sofias_memory.services.runs import run_detail_result, run_not_found_error

RETRY_IDEMPOTENCY_KEY_PREFIX = "sys:retry:"
RETRYABLE_STATUSES = frozenset({PipelineRunStatus.FAILED, PipelineRunStatus.CANCELLED})


def run_not_retryable_error(run_id: UUID, *, reason: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.RUN_NOT_RETRYABLE,
        status_code=HTTPStatus.CONFLICT,
        message="This run cannot be manually retried.",
        details={"run_id": str(run_id), "reason": reason},
    )


def _internal_lifecycle_error(run_id: UUID) -> SofiasMemoryError:
    """Fail-safe for an impossible persisted state (SM-514 SS 8): never
    silently reclassify corruption as a normal outcome."""

    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Run lifecycle state is inconsistent.",
        details={"run_id": str(run_id)},
    )


@dataclass(frozen=True, slots=True)
class ControlResult:
    """What a cancel/retry call resolved to -- a public Run projection plus
    the HTTP status the route should use (ADR-0009 SS R point 4: this
    service knows nothing about FastAPI)."""

    detail: RunDetailResult
    http_status: int


@dataclass(frozen=True, slots=True)
class _OriginalSnapshot:
    id: UUID
    pipeline_type: PipelineType
    dataset_id: UUID | None
    source_id: UUID | None
    input: dict[str, JSONValue]


def _is_legitimate_global_run(*, pipeline_type: PipelineType, input_: dict[str, JSONValue]) -> bool:
    """SM-514 SS 29: the only currently-legitimate ``dataset_id IS NULL``
    persisted run is a Forget EVERYTHING run, provable from its own
    persisted ``input`` (never inferred/guessed). A pre-B5 historical row
    that happens to carry a NULL ``dataset_id`` for some other reason fails
    closed rather than being silently treated as global."""

    return pipeline_type == PipelineType.FORGET and input_.get("scope") == "everything"


class RunControlService:
    """Cancel and manual-retry operations over durable PipelineRun state."""

    def __init__(
        self,
        *,
        registry: PipelineRegistry,
        worker: WorkerAvailability,
        settings: Settings,
        session_factory: AsyncSessionFactory,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._submission = PipelineSubmissionService(
            registry=registry,
            worker=worker,
            config_fingerprint=settings.config_fingerprint(),
            session_factory=session_factory,
        )

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    async def cancel(self, run_id: UUID) -> ControlResult:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            if run is None:
                raise run_not_found_error(run_id)
            now = await uow.pipeline_runs.get_database_now()

            if run.status == PipelineRunStatus.QUEUED:
                steps = await uow.pipeline_steps.list_for_run(run.id)
                if any(step.status == PipelineStepStatus.RUNNING for step in steps):
                    # SM-514 SS 8: a QUEUED run can never legitimately own a
                    # RUNNING step -- fail loudly instead of silently
                    # converting it, and leave recovery to investigate.
                    raise _internal_lifecycle_error(run_id)
                for step in steps:
                    if step.status == PipelineStepStatus.QUEUED:
                        transition_step(step, PipelineStepStatus.CANCELLED, now=now)
                transition_run(run, PipelineRunStatus.CANCELLED, now=now)
                await uow.commit()
                http_status = HTTPStatus.OK
            elif run.status == PipelineRunStatus.RUNNING:
                # Pure status flip (SM-514 SS 9/10): never touches the
                # in-flight step, provider call, or filesystem/Neo4j. The
                # engine's own next safe checkpoint (or stale-CANCELLING
                # recovery, if the worker dies first) finishes the job.
                transition_run(run, PipelineRunStatus.CANCELLING, now=now)
                await uow.commit()
                http_status = HTTPStatus.ACCEPTED
            elif run.status == PipelineRunStatus.CANCELLING:
                http_status = HTTPStatus.ACCEPTED
            else:
                # SUCCEEDED / FAILED / CANCELLED: immutable, idempotent no-op.
                http_status = HTTPStatus.OK

            steps_for_detail = await uow.pipeline_steps.list_for_run(run.id)
            detail = run_detail_result(run, steps_for_detail)

        return ControlResult(detail=detail, http_status=int(http_status))

    # ------------------------------------------------------------------
    # manual retry
    # ------------------------------------------------------------------

    async def retry(self, original_run_id: UUID) -> ControlResult:
        original = await self._load_retryable_original(original_run_id)

        idempotency_key = f"{RETRY_IDEMPOTENCY_KEY_PREFIX}{original_run_id}"

        # Existing-child lookup precedes the worker gate and any Remember
        # ingress preparation (SM-514 SS 26/37): re-observing an
        # already-created child must never require a live worker or spend
        # filesystem I/O staging a candidate that will just be discarded.
        existing_id = await self._existing_retry_child(
            idempotency_key, original_run_id=original_run_id
        )
        if existing_id is not None:
            return await self._respond(existing_id)

        candidate_run_id = uuid4()
        if original.pipeline_type == PipelineType.REMEMBER:
            staged = await prepare_remember_retry_ingress(
                session_factory=self._session_factory,
                data_directory=self._settings.data_directory,
                original_run_id=original.id,
                original_source_id=original.source_id,
                candidate_run_id=candidate_run_id,
                source_kind=str(original.input.get("source_kind", "")),
            )
            if not staged:
                raise run_not_retryable_error(original_run_id, reason="ingress_unrecoverable")

        try:
            outcome = await self._submission.submit_trusted_internal(
                pipeline_type=original.pipeline_type,
                work_input=original.input,
                idempotency_key=idempotency_key,
                prepare=_retry_preparation_hook(original),
                run_id=candidate_run_id,
                retry_of_run_id=original_run_id,
            )
        except Exception:
            if original.pipeline_type == PipelineType.REMEMBER:
                delete_ingress_artifact(self._settings.data_directory, run_id=candidate_run_id)
            raise

        if not outcome.created:
            # Lost an idempotency-key race to a concurrent retry request
            # (SM-514 SS 23) -- clean up only this request's own candidate,
            # never the winner's.
            if original.pipeline_type == PipelineType.REMEMBER:
                delete_ingress_artifact(self._settings.data_directory, run_id=candidate_run_id)
            if outcome.retry_of_run_id != original_run_id:
                # SM-514 SS 25: fail-safe against an internal-key collision
                # with an unrelated row -- never return an unrelated run.
                raise run_not_retryable_error(original_run_id, reason="internal_key_collision")

        return await self._respond(outcome.run_id)

    async def _load_retryable_original(self, original_run_id: UUID) -> _OriginalSnapshot:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            original = await uow.pipeline_runs.get_by_id(original_run_id)
            if original is None:
                raise run_not_found_error(original_run_id)
            if original.status not in RETRYABLE_STATUSES:
                raise run_not_retryable_error(
                    original_run_id, reason=f"status={original.status.value}"
                )
            input_ = cast("dict[str, JSONValue]", dict(original.input))
            if original.dataset_id is None and not _is_legitimate_global_run(
                pipeline_type=original.pipeline_type, input_=input_
            ):
                raise run_not_retryable_error(original_run_id, reason="scope_unresolvable")
            return _OriginalSnapshot(
                id=original.id,
                pipeline_type=original.pipeline_type,
                dataset_id=original.dataset_id,
                source_id=original.source_id,
                input=input_,
            )

    async def _existing_retry_child(
        self, idempotency_key: str, *, original_run_id: UUID
    ) -> UUID | None:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            existing = await uow.pipeline_runs.get_by_idempotency_key(idempotency_key)
            if existing is None:
                return None
            if existing.retry_of_run_id != original_run_id:
                raise run_not_retryable_error(original_run_id, reason="internal_key_collision")
            return existing.id

    async def _respond(self, run_id: UUID) -> ControlResult:
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            assert run is not None  # noqa: S101 - just resolved/created, cannot vanish
            status = run.status
            steps = await uow.pipeline_steps.list_for_run(run_id)
            detail = run_detail_result(run, steps)
        http_status = HTTPStatus.OK if status in TERMINAL_RUN_STATUSES else HTTPStatus.ACCEPTED
        return ControlResult(detail=detail, http_status=int(http_status))


def _retry_preparation_hook(original: _OriginalSnapshot) -> PreparationHook:
    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        # Closed, pipeline-agnostic rule (SM-514 SS 19): preserve exactly
        # what the ORIGINAL submission already resolved as authoritative --
        # never re-derive, never guess, never open-heuristic per type. Each
        # pipeline's own original PreparationHook already validated these
        # ids once; a retry is not a new semantic request, so there is
        # nothing new to validate here.
        del uow
        return SubmissionTargets(dataset_id=original.dataset_id, source_id=original.source_id)

    return prepare


__all__ = [
    "RETRY_IDEMPOTENCY_KEY_PREFIX",
    "ControlResult",
    "RunControlService",
    "run_not_retryable_error",
]
