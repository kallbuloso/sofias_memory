"""Cognify route (SM-510).

A pure submission/observation boundary: it validates the request, submits a
durable ``PipelineRun`` through the shared SM-509 contract, and -- when the
caller asked to wait -- observes the run's persisted terminal state. It never
executes a pipeline step, never constructs a provider client, and never
builds a result from anything other than PostgreSQL.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from sofias_memory.api.errors import SofiasMemoryError, current_request_id
from sofias_memory.api.openapi_responses import (
    DATASET_NOT_FOUND_404,
    IDEMPOTENCY_OR_DATASET_CONFLICT_409,
    WORKER_DISABLED_503,
    error_response,
)
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.lifespan import (
    app_pipeline_registry,
    app_pipeline_worker,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.cognify import CognifyRequest, CognifyResult
from sofias_memory.schemas.common import ErrorCode, JSONValue, ResponseMeta, SuccessEnvelope
from sofias_memory.services.cognify import (
    COGNIFY_RESULT_METRIC_KEY,
    cognify_run_input,
    dataset_not_found_error,
)
from sofias_memory.services.pipeline_submission import (
    PipelineSubmissionService,
    PreparationHook,
    SubmissionOutcome,
    SubmissionTargets,
    SubmissionUnitOfWork,
)
from sofias_memory.services.pipeline_waiter import PipelineRunWaiter

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Optional retry-safety key for this write. Reusing the same key with the "
    "same logical request returns the original PipelineRun instead of creating "
    "duplicate work; reusing it for different work returns an idempotency "
    "conflict. Keys starting with 'sys:' are reserved. Leave blank for ordinary "
    "manual testing."
)
RETRY_SAFETY_DESCRIPTION = (
    "\n\n**Retry safety:** clients may optionally send an `Idempotency-Key` "
    "header (see the canonical /openapi.json for its full parameter "
    "documentation -- it is omitted from this human-facing page for "
    "readability). Reusing the same key for the same logical request returns "
    "the original run instead of creating duplicate work; reusing it for "
    "different work returns a conflict. Ordinary manual testing does not "
    "require it."
)

_COGNIFY_BAD_REQUEST_400 = error_response(
    "Invalid request. ErrorEnvelope with error.code=INVALID_REQUEST -- either "
    "rebuild=true was combined with source_ids, or the Idempotency-Key uses "
    "the reserved 'sys:' namespace (error.code=RESERVED_IDEMPOTENCY_KEY_NAMESPACE)."
)

router = APIRouter(tags=["cognify"])


@router.post(
    "/cognify",
    response_model=SuccessEnvelope[CognifyResult],
    summary="Cognify a dataset",
    description=(
        "Process a dataset's stored sources into chunks, embeddings, entities, and "
        "relations. By default processes only pending/explicitly selected sources; "
        "`rebuild=true` reprocesses the whole dataset onto a new generation without "
        "ever exposing a partially-rebuilt state to readers. Creates a durable "
        "PipelineRun; use `wait=false` for an immediate `202` or `wait=true` to wait "
        "for the terminal result." + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: _COGNIFY_BAD_REQUEST_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: IDEMPOTENCY_OR_DATASET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def cognify(
    payload: CognifyRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
) -> SuccessEnvelope[CognifyResult]:
    _validate_request(payload)

    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    work_input = cognify_run_input(payload)

    submission = PipelineSubmissionService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        config_fingerprint=settings.config_fingerprint(),
        session_factory=session_factory,
    )
    outcome = await submission.submit(
        pipeline_type=PipelineType.COGNIFY,
        work_input=work_input,
        idempotency_key=idempotency_key,
        prepare=_dataset_preparation_hook(payload.dataset),
    )

    status = outcome.status
    if payload.wait and not outcome.terminal:
        waited = await PipelineRunWaiter(session_factory=session_factory).wait_for_terminal(
            outcome.run_id,
            timeout_seconds=settings.request_wait_timeout_seconds,
        )
        status = waited.status

    return await _respond(
        response,
        session_factory=session_factory,
        outcome=outcome,
        status=status,
    )


def _validate_request(payload: CognifyRequest) -> None:
    if payload.rebuild and payload.source_ids is not None:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Cognify rebuild always covers the whole dataset and rejects source_ids.",
            details={"rebuild": payload.rebuild},
        )


def _dataset_preparation_hook(dataset_slug: str) -> PreparationHook:
    """Resolve the target dataset inside the submission transaction.

    PostgreSQL-only and read-only: it creates nothing, so it owns no
    uniqueness race of its own and a losing idempotency race can roll it back
    with no external effect (see ``PreparationHook``'s contract). Dataset
    existence is deliberately checked here rather than in the handler, so it
    is validated in the very same transaction as the run that targets it.
    """

    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        # Same narrowing the submission service itself performs: the shared
        # Protocol only declares the run/step repositories it needs, while a
        # hook may use any repository of the real UnitOfWork it is handed.
        dataset = await cast(PostgresUnitOfWork, uow).datasets.get_by_slug(dataset_slug)
        if dataset is None:
            raise dataset_not_found_error(dataset_slug)
        return SubmissionTargets(dataset_id=dataset.id, source_id=None)

    return prepare


async def _respond(
    response: Response,
    *,
    session_factory: AsyncSessionFactory,
    outcome: SubmissionOutcome,
    status: PipelineRunStatus,
) -> SuccessEnvelope[CognifyResult]:
    if status == PipelineRunStatus.SUCCEEDED:
        result = await _succeeded_result(session_factory, run_id=outcome.run_id)
    elif status == PipelineRunStatus.FAILED:
        raise await _failed_run_error(session_factory, run_id=outcome.run_id)
    else:
        # Queued/running/cancelling: accepted, not finished. Cancelled: a
        # terminal, non-error outcome the caller can observe -- neither
        # carries business counters, and neither is ever fabricated.
        result = CognifyResult(run_id=outcome.run_id, status=status)
        if status != PipelineRunStatus.CANCELLED:
            response.status_code = HTTPStatus.ACCEPTED

    return SuccessEnvelope[CognifyResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


async def _succeeded_result(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> CognifyResult:
    metrics = await _run_metrics(session_factory, run_id=run_id)
    persisted = metrics.get(COGNIFY_RESULT_METRIC_KEY)
    if not isinstance(persisted, dict):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Cognify run succeeded without a persisted result.",
            details={"run_id": str(run_id)},
        )
    return CognifyResult(
        run_id=run_id,
        status=PipelineRunStatus.SUCCEEDED,
        dataset_id=UUID(str(persisted["dataset_id"])),
        generation=int(persisted["generation"]),
        sources_processed=int(persisted["sources_processed"]),
        chunks=int(persisted["chunks"]),
        entities=int(persisted["entities"]),
        relations=int(persisted["relations"]),
    )


async def _failed_run_error(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> SofiasMemoryError:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        error_code = run.error_code if run is not None else None
    details: dict[str, JSONValue] = {
        "run_id": str(run_id),
        "status": PipelineRunStatus.FAILED.value,
    }
    if error_code is not None:
        # Only the engine's own stable step error code -- never a traceback,
        # provider payload, or internal path (ADR-0009 SS X).
        details["step_error_code"] = error_code
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Cognify run failed.",
        details=details,
    )


async def _run_metrics(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> dict[str, Any]:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        return dict(run.metrics) if run is not None else {}
