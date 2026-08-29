"""Forget route (SM-512).

A pure submission/observation boundary, mirroring ``routes.cognify``/
``routes.improve`` (SM-510/511): it validates the request, derives its scope
(SOURCE/DATASET/EVERYTHING, FR-090), submits a durable ``PipelineRun``
through the shared SM-509 contract, and -- when the caller asked to wait --
observes the run's persisted terminal state. It never executes a pipeline
step, never constructs a Neo4j/storage service, and never builds a result
from anything other than PostgreSQL.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from sofias_memory.api.errors import SofiasMemoryError, current_request_id
from sofias_memory.api.openapi_responses import WORKER_DISABLED_503, error_response
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.lifespan import (
    app_pipeline_registry,
    app_pipeline_worker,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue, ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.forget import (
    ForgetDatasetResult,
    ForgetEverythingResult,
    ForgetRequest,
    ForgetResponseData,
    ForgetSourceResult,
)
from sofias_memory.services.forget import (
    FORGET_DATASET_RESULT_METRIC_KEY,
    FORGET_EVERYTHING_RESULT_METRIC_KEY,
    FORGET_RESULT_METRIC_KEY,
    FORGET_TARGET_CONFLICT_ERROR_CODE,
    ForgetScope,
    dataset_not_found_error,
    determine_forget_scope,
    forget_dataset_run_input,
    forget_everything_run_input,
    forget_source_run_input,
    source_not_found_error,
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

_FORGET_BAD_REQUEST_400 = error_response(
    "Invalid request. ErrorEnvelope with error.code=INVALID_REQUEST -- either "
    "the scope fields are contradictory (e.g. everything combined with "
    "source_id/dataset/memory_only, or a missing/wrong confirm phrase for "
    "everything=true), or the Idempotency-Key uses the reserved 'sys:' "
    "namespace (error.code=RESERVED_IDEMPOTENCY_KEY_NAMESPACE)."
)
_FORGET_NOT_FOUND_404 = error_response(
    "The target dataset or source does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_FORGET_CONFLICT_409 = error_response(
    "Conflict with the requested Forget operation. ErrorEnvelope with "
    "error.code one of: IDEMPOTENCY_CONFLICT (the same Idempotency-Key was "
    "already used for different work), DATASET_DELETING or DATASET_DELETED "
    "(the target dataset has an in-flight or completed administrative "
    "delete), or INVALID_REQUEST (a conflicting Forget operation already "
    "targets the same source/dataset)."
)

router = APIRouter(tags=["forget"])


@router.post(
    "/forget",
    response_model=SuccessEnvelope[ForgetResponseData],
    summary="Forget memory",
    description=(
        "**Destructive operation.** Removes memory at one of three scopes, "
        "determined by which fields are set: a single `source_id` (SOURCE scope), "
        "a `dataset` alone (DATASET scope, clears the whole dataset's memory), or "
        '`everything=true` with `confirm="DELETE EVERYTHING"` (every dataset -- '
        "requires the exact confirmation phrase). `memory_only=true` clears derived "
        "memory but keeps the original source for later re-Cognify. This is "
        "distinct from administrative Dataset DELETE (`DELETE /api/v1/datasets/"
        "{dataset_id}`), which permanently retires the dataset namespace itself. "
        "Creates a durable PipelineRun; use `wait=false` for an immediate `202` or "
        "`wait=true` to wait for the terminal result." + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: _FORGET_BAD_REQUEST_400,
        HTTPStatus.NOT_FOUND: _FORGET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _FORGET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def forget(
    payload: ForgetRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
) -> SuccessEnvelope[ForgetResponseData]:
    scope = determine_forget_scope(
        dataset=payload.dataset,
        fields_set=payload.model_fields_set,
        source_id=payload.source_id,
        everything=payload.everything,
        confirm=payload.confirm,
        memory_only=payload.memory_only,
    )

    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)

    if scope is ForgetScope.SOURCE:
        assert payload.source_id is not None  # noqa: S101 - guaranteed by determine_forget_scope
        work_input = forget_source_run_input(
            dataset=payload.dataset, source_id=payload.source_id, memory_only=payload.memory_only
        )
        prepare = _source_preparation_hook(payload.dataset, payload.source_id)
        pipeline_type = PipelineType.FORGET
        metric_key = FORGET_RESULT_METRIC_KEY
    elif scope is ForgetScope.DATASET:
        work_input = forget_dataset_run_input(
            dataset=payload.dataset, memory_only=payload.memory_only
        )
        prepare = _dataset_preparation_hook(payload.dataset)
        pipeline_type = PipelineType.FORGET
        metric_key = FORGET_DATASET_RESULT_METRIC_KEY
    else:
        work_input = forget_everything_run_input()
        prepare = _everything_preparation_hook()
        pipeline_type = PipelineType.FORGET
        metric_key = FORGET_EVERYTHING_RESULT_METRIC_KEY

    submission = PipelineSubmissionService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        config_fingerprint=settings.config_fingerprint(),
        session_factory=session_factory,
    )
    outcome = await submission.submit(
        pipeline_type=pipeline_type,
        work_input=work_input,
        idempotency_key=idempotency_key,
        prepare=prepare,
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
        scope=scope,
        metric_key=metric_key,
    )


def _source_preparation_hook(dataset_slug: str, source_id: UUID) -> PreparationHook:
    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        postgres_uow = cast(PostgresUnitOfWork, uow)
        dataset = await postgres_uow.datasets.get_by_slug(dataset_slug)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            raise dataset_not_found_error(dataset_slug)
        source = await postgres_uow.sources.get_by_id(source_id)
        if source is None or source.dataset_id != dataset.id:
            raise source_not_found_error(source_id)
        return SubmissionTargets(dataset_id=dataset.id, source_id=source.id)

    return prepare


def _dataset_preparation_hook(dataset_slug: str) -> PreparationHook:
    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        postgres_uow = cast(PostgresUnitOfWork, uow)
        dataset = await postgres_uow.datasets.get_by_slug(dataset_slug)
        if dataset is None or dataset.status not in (DatasetStatus.ACTIVE, DatasetStatus.DELETING):
            raise dataset_not_found_error(dataset_slug)
        return SubmissionTargets(dataset_id=dataset.id, source_id=None)

    return prepare


def _everything_preparation_hook() -> PreparationHook:
    """EVERYTHING is global: it resolves/creates nothing (SM-512 SS 6) --
    read-only, PostgreSQL-only, and trivial, but still exercised inside the
    same submission transaction as every other scope (SM-509 Part C)."""

    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        del uow
        return SubmissionTargets(dataset_id=None, source_id=None)

    return prepare


async def _respond(
    response: Response,
    *,
    session_factory: AsyncSessionFactory,
    outcome: SubmissionOutcome,
    status: PipelineRunStatus,
    scope: ForgetScope,
    metric_key: str,
) -> SuccessEnvelope[ForgetResponseData]:
    if status == PipelineRunStatus.SUCCEEDED:
        result = await _succeeded_result(
            session_factory, run_id=outcome.run_id, scope=scope, metric_key=metric_key
        )
    elif status == PipelineRunStatus.FAILED:
        raise await _failed_run_error(session_factory, run_id=outcome.run_id)
    else:
        result = _pending_result(outcome.run_id, status, scope)
        if status != PipelineRunStatus.CANCELLED:
            response.status_code = HTTPStatus.ACCEPTED

    return SuccessEnvelope[ForgetResponseData](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


def _pending_result(
    run_id: UUID, status: PipelineRunStatus, scope: ForgetScope
) -> ForgetResponseData:
    if scope is ForgetScope.SOURCE:
        return ForgetSourceResult(run_id=run_id, status=status)
    if scope is ForgetScope.DATASET:
        return ForgetDatasetResult(run_id=run_id, status=status)
    return ForgetEverythingResult(run_id=run_id, status=status)


async def _succeeded_result(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
    scope: ForgetScope,
    metric_key: str,
) -> ForgetResponseData:
    metrics = await _run_metrics(session_factory, run_id=run_id)
    persisted = metrics.get(metric_key)
    if not isinstance(persisted, dict):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Forget run succeeded without a persisted result.",
            details={"run_id": str(run_id)},
        )
    if scope is ForgetScope.SOURCE:
        return ForgetSourceResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED,
            dataset_id=UUID(str(persisted["dataset_id"])),
            source_id=UUID(str(persisted["source_id"])),
            memory_only=bool(persisted["memory_only"]),
            source_status=str(persisted["source_status"])
            if persisted.get("source_status")
            else None,
            documents_deactivated=int(persisted["documents_deactivated"]),
            chunks_deactivated=int(persisted["chunks_deactivated"]),
            summaries_deactivated=int(persisted["summaries_deactivated"]),
            entities_deactivated=int(persisted["entities_deactivated"]),
            relations_deactivated=int(persisted["relations_deactivated"]),
            entity_mentions_unprojected=int(persisted["entity_mentions_unprojected"]),
            relation_evidence_unprojected=int(persisted["relation_evidence_unprojected"]),
            graph_events_enqueued=int(persisted["graph_events_enqueued"]),
            graph_events_processed=int(persisted["graph_events_processed"]),
            storage_deleted=bool(persisted["storage_deleted"]),
        )
    if scope is ForgetScope.DATASET:
        return ForgetDatasetResult(
            run_id=run_id,
            status=PipelineRunStatus.SUCCEEDED,
            dataset_id=UUID(str(persisted["dataset_id"])) if persisted.get("dataset_id") else None,
            memory_only=bool(persisted["memory_only"]),
            sources_affected=int(persisted["sources_affected"]),
            sources_pending=int(persisted["sources_pending"]),
            sources_deleted=int(persisted["sources_deleted"]),
            documents_deactivated=int(persisted["documents_deactivated"]),
            chunks_deactivated=int(persisted["chunks_deactivated"]),
            summaries_deactivated=int(persisted["summaries_deactivated"]),
            entities_deactivated=int(persisted["entities_deactivated"]),
            relations_deactivated=int(persisted["relations_deactivated"]),
            entity_mentions_unprojected=int(persisted["entity_mentions_unprojected"]),
            relation_evidence_unprojected=int(persisted["relation_evidence_unprojected"]),
            graph_events_enqueued=int(persisted["graph_events_enqueued"]),
            graph_events_processed=int(persisted["graph_events_processed"]),
            storage_deleted=int(persisted["storage_deleted"]),
            storage_already_absent=int(persisted["storage_already_absent"]),
        )
    return ForgetEverythingResult(
        run_id=run_id,
        status=PipelineRunStatus.SUCCEEDED,
        datasets_affected=int(persisted["datasets_affected"]),
        sources_affected=int(persisted["sources_affected"]),
        sources_pending=int(persisted["sources_pending"]),
        sources_deleted=int(persisted["sources_deleted"]),
        documents_deactivated=int(persisted["documents_deactivated"]),
        chunks_deactivated=int(persisted["chunks_deactivated"]),
        summaries_deactivated=int(persisted["summaries_deactivated"]),
        entities_deactivated=int(persisted["entities_deactivated"]),
        relations_deactivated=int(persisted["relations_deactivated"]),
        entity_mentions_unprojected=int(persisted["entity_mentions_unprojected"]),
        relation_evidence_unprojected=int(persisted["relation_evidence_unprojected"]),
        graph_events_enqueued=int(persisted["graph_events_enqueued"]),
        graph_events_processed=int(persisted["graph_events_processed"]),
        storage_deleted=int(persisted["storage_deleted"]),
        storage_already_absent=int(persisted["storage_already_absent"]),
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
        details["step_error_code"] = error_code
    if error_code == FORGET_TARGET_CONFLICT_ERROR_CODE:
        # B4 parity (SM-422): a conflicting persisted intent is a stable,
        # retryable 409 -- not a generic 500 internal error.
        return SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.CONFLICT,
            message="A conflicting forget operation targets the same source/dataset.",
            details=details,
        )
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Forget run failed.",
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
