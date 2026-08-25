"""Runs API routes (SM-508 read, SM-514 cancel/retry control).

``GET /runs``/``GET /runs/{run_id}`` are read-only observability over
durable pipeline lifecycle state (``RunService``). ``POST .../cancel`` and
``POST .../retry`` are the control surface (``RunControlService``, SM-514):
neither accepts a request body, and neither executes business pipeline
code -- see ``services.run_control`` for the full contract.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from sofias_memory.api.errors import current_request_id
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.lifespan import (
    app_pipeline_registry,
    app_pipeline_worker,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.runs import (
    RUN_PAGE_DEFAULT_LIMIT,
    RUN_PAGE_MAX_LIMIT,
    RunDetailResult,
    RunListResult,
)
from sofias_memory.services.run_control import RunControlService
from sofias_memory.services.runs import RunService

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=SuccessEnvelope[RunListResult])
async def list_runs(
    request: Request,
    limit: int = Query(default=RUN_PAGE_DEFAULT_LIMIT, ge=1, le=RUN_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    status: Annotated[list[PipelineRunStatus] | None, Query()] = None,
    pipeline_type: Annotated[PipelineType | None, Query(alias="type")] = None,
    dataset_id: Annotated[UUID | None, Query()] = None,
) -> SuccessEnvelope[RunListResult]:
    service = RunService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_runs(
        limit=limit,
        offset=offset,
        statuses=status,
        dataset_id=dataset_id,
        pipeline_type=pipeline_type,
    )
    return SuccessEnvelope[RunListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/runs/{run_id}", response_model=SuccessEnvelope[RunDetailResult])
async def get_run(
    run_id: UUID,
    request: Request,
) -> SuccessEnvelope[RunDetailResult]:
    service = RunService(session_factory=app_postgres_session_factory(request.app))
    result = await service.get_run(run_id)
    return SuccessEnvelope[RunDetailResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


def _control_service(request: Request) -> RunControlService:
    return RunControlService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        settings=app_settings(request.app),
        session_factory=app_postgres_session_factory(request.app),
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=SuccessEnvelope[RunDetailResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "Cancellation was accepted (RUNNING -> CANCELLING, or already "
                "CANCELLING). Poll GET /api/v1/runs/{run_id} for the terminal state."
            )
        }
    },
)
async def cancel_run(
    run_id: UUID,
    request: Request,
    response: Response,
) -> SuccessEnvelope[RunDetailResult]:
    control = _control_service(request)
    result = await control.cancel(run_id)
    response.status_code = result.http_status
    return SuccessEnvelope[RunDetailResult](
        data=result.detail,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/runs/{run_id}/retry",
    response_model=SuccessEnvelope[RunDetailResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "A new (or already-existing, still non-terminal) retry run was "
                "accepted. Poll GET /api/v1/runs/{run_id} for the terminal state."
            )
        }
    },
)
async def retry_run(
    run_id: UUID,
    request: Request,
    response: Response,
) -> SuccessEnvelope[RunDetailResult]:
    control = _control_service(request)
    result = await control.retry(run_id)
    response.status_code = result.http_status
    return SuccessEnvelope[RunDetailResult](
        data=result.detail,
        meta=ResponseMeta(request_id=current_request_id()),
    )
