"""Improve route (SM-511).

A pure submission/observation boundary, mirroring ``routes.cognify`` (SM-510):
it validates the request, submits a durable ``PipelineRun`` through the
shared SM-509 contract, and -- when the caller asked to wait -- observes the
run's persisted terminal state. It never executes a pipeline step, never
constructs a provider/Neo4j client, and never builds a result from anything
other than PostgreSQL.
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
    RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
    WORKER_DISABLED_503,
)
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
from sofias_memory.schemas.improve import ImproveRequest, ImproveResult
from sofias_memory.services.improve import (
    IMPROVE_RESULT_METRIC_KEY,
    dataset_not_found_error,
    improve_run_input,
    normalize_improve_stages,
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

router = APIRouter(tags=["improve"])


@router.post(
    "/improve",
    response_model=SuccessEnvelope[ImproveResult],
    summary="Improve dataset memory quality",
    description=(
        "Run background hygiene on a dataset: feedback-weighted ranking, entity "
        "deduplication, relation embedding refresh, summary maintenance, and graph "
        "reconciliation. Always explicit -- never triggered implicitly by any other "
        "request. Creates a durable PipelineRun; use `wait=false` for an immediate "
        "`202` or `wait=true` to wait for the terminal result." + RETRY_SAFETY_DESCRIPTION
    ),
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "The run was accepted durably and has not reached a terminal state "
                "(wait=false, or wait=true timed out). Poll GET /api/v1/runs/{run_id}."
            )
        },
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.CONFLICT: IDEMPOTENCY_OR_DATASET_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: WORKER_DISABLED_503,
    },
)
async def improve(
    payload: ImproveRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
) -> SuccessEnvelope[ImproveResult]:
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    stages = normalize_improve_stages(payload.stages)
    work_input = improve_run_input(payload.dataset, stages)

    submission = PipelineSubmissionService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        config_fingerprint=settings.config_fingerprint(),
        session_factory=session_factory,
    )
    outcome = await submission.submit(
        pipeline_type=PipelineType.IMPROVE,
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


def _dataset_preparation_hook(dataset_slug: str) -> PreparationHook:
    """Resolve the target dataset inside the submission transaction.

    PostgreSQL-only and read-only, same contract as Cognify's hook: creates
    nothing, so it owns no uniqueness race of its own. Improve additionally
    requires the dataset to be ``ACTIVE`` (B4 parity) -- checked in this same
    transaction rather than in the handler.
    """

    async def prepare(uow: SubmissionUnitOfWork) -> SubmissionTargets:
        dataset = await cast(PostgresUnitOfWork, uow).datasets.get_by_slug(dataset_slug)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            raise dataset_not_found_error(dataset_slug)
        return SubmissionTargets(dataset_id=dataset.id, source_id=None)

    return prepare


async def _respond(
    response: Response,
    *,
    session_factory: AsyncSessionFactory,
    outcome: SubmissionOutcome,
    status: PipelineRunStatus,
) -> SuccessEnvelope[ImproveResult]:
    if status == PipelineRunStatus.SUCCEEDED:
        result = await _succeeded_result(session_factory, run_id=outcome.run_id)
    elif status == PipelineRunStatus.FAILED:
        raise await _failed_run_error(session_factory, run_id=outcome.run_id)
    else:
        result = ImproveResult(run_id=outcome.run_id, status=status)
        if status != PipelineRunStatus.CANCELLED:
            response.status_code = HTTPStatus.ACCEPTED

    return SuccessEnvelope[ImproveResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


async def _succeeded_result(
    session_factory: AsyncSessionFactory,
    *,
    run_id: UUID,
) -> ImproveResult:
    metrics = await _run_metrics(session_factory, run_id=run_id)
    persisted = metrics.get(IMPROVE_RESULT_METRIC_KEY)
    if not isinstance(persisted, dict):
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Improve run succeeded without a persisted result.",
            details={"run_id": str(run_id)},
        )
    return ImproveResult(
        run_id=run_id,
        status=PipelineRunStatus.SUCCEEDED,
        dataset_id=UUID(str(persisted["dataset_id"])),
        generation=int(persisted["generation"]),
        stages=[str(stage) for stage in persisted["stages"]],
        feedback_processed=int(persisted["feedback_processed"]),
        feedback_applied=int(persisted["feedback_applied"]),
        feedback_skipped=int(persisted["feedback_skipped"]),
        entities_updated=int(persisted["entities_updated"]),
        relations_updated=int(persisted["relations_updated"]),
        relations_embedded=int(persisted["relations_embedded"]),
        entities_embedded=int(persisted["entities_embedded"]),
        entity_duplicate_candidates=int(persisted["entity_duplicate_candidates"]),
        entities_merged=int(persisted["entities_merged"]),
        entity_mentions_reassigned=int(persisted["entity_mentions_reassigned"]),
        relations_rewired=int(persisted["relations_rewired"]),
        relations_deactivated=int(persisted["relations_deactivated"]),
        relation_evidence_copied=int(persisted["relation_evidence_copied"]),
        document_summaries_rebuilt=int(persisted["document_summaries_rebuilt"]),
        dataset_summaries_rebuilt=int(persisted["dataset_summaries_rebuilt"]),
        summaries_deactivated=int(persisted["summaries_deactivated"]),
        graph_relations_deactivated=int(persisted["graph_relations_deactivated"]),
        graph_entities_importance_updated=int(persisted["graph_entities_importance_updated"]),
        graph_relations_importance_updated=int(persisted["graph_relations_importance_updated"]),
        graph_entities_missing=int(persisted["graph_entities_missing"]),
        graph_entities_extra=int(persisted["graph_entities_extra"]),
        graph_chunks_missing=int(persisted["graph_chunks_missing"]),
        graph_chunks_extra=int(persisted["graph_chunks_extra"]),
        graph_entity_mentions_missing=int(persisted["graph_entity_mentions_missing"]),
        graph_entity_mentions_extra=int(persisted["graph_entity_mentions_extra"]),
        graph_relations_missing=int(persisted["graph_relations_missing"]),
        graph_relations_extra=int(persisted["graph_relations_extra"]),
        graph_next_missing=int(persisted["graph_next_missing"]),
        graph_next_extra=int(persisted["graph_next_extra"]),
        graph_rebuilt=bool(persisted["graph_rebuilt"]),
        graph_events_enqueued=int(persisted["graph_events_enqueued"]),
        graph_events_processed=int(persisted["graph_events_processed"]),
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
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Improve run failed.",
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
