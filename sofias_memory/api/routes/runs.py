"""Runs read API routes (SM-508).

Read-only observability over durable pipeline lifecycle state. Retry/cancel
(``POST /api/v1/runs/{run_id}/retry`` and ``.../cancel``) belong to SM-514
and are deliberately not registered here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.runs import (
    RUN_PAGE_DEFAULT_LIMIT,
    RUN_PAGE_MAX_LIMIT,
    RunDetailResult,
    RunListResult,
)
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
