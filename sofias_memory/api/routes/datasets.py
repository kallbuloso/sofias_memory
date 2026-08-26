"""Dataset management API routes."""

from __future__ import annotations

from http import HTTPStatus
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import (
    app_pipeline_registry,
    app_pipeline_worker,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.datasets import (
    DATASET_PAGE_DEFAULT_LIMIT,
    DATASET_PAGE_MAX_LIMIT,
    DatasetCreateRequest,
    DatasetDeleteResult,
    DatasetListResult,
    DatasetRenameRequest,
    DatasetResult,
    DatasetSourcesResult,
    DatasetStatsResult,
)
from sofias_memory.services.dataset_delete import DatasetDeleteService
from sofias_memory.services.datasets import DatasetService

router = APIRouter(tags=["datasets"])


@router.post(
    "/datasets",
    response_model=SuccessEnvelope[DatasetResult],
    status_code=HTTPStatus.CREATED,
)
async def create_dataset(
    payload: DatasetCreateRequest,
    request: Request,
) -> SuccessEnvelope[DatasetResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.create_dataset(payload)
    return SuccessEnvelope[DatasetResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/datasets", response_model=SuccessEnvelope[DatasetListResult])
async def list_datasets(
    request: Request,
    limit: int = Query(default=DATASET_PAGE_DEFAULT_LIMIT, ge=1, le=DATASET_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> SuccessEnvelope[DatasetListResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_datasets(limit=limit, offset=offset)
    return SuccessEnvelope[DatasetListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/datasets/{dataset_id}", response_model=SuccessEnvelope[DatasetResult])
async def get_dataset(
    dataset_id: UUID,
    request: Request,
) -> SuccessEnvelope[DatasetResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.get_dataset(dataset_id)
    return SuccessEnvelope[DatasetResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.patch("/datasets/{dataset_id}", response_model=SuccessEnvelope[DatasetResult])
async def rename_dataset(
    dataset_id: UUID,
    payload: DatasetRenameRequest,
    request: Request,
) -> SuccessEnvelope[DatasetResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.rename_dataset(dataset_id, payload)
    return SuccessEnvelope[DatasetResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/datasets/{dataset_id}/sources", response_model=SuccessEnvelope[DatasetSourcesResult])
async def list_dataset_sources(
    dataset_id: UUID,
    request: Request,
    limit: int = Query(default=DATASET_PAGE_DEFAULT_LIMIT, ge=1, le=DATASET_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> SuccessEnvelope[DatasetSourcesResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_sources(dataset_id=dataset_id, limit=limit, offset=offset)
    return SuccessEnvelope[DatasetSourcesResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get("/datasets/{dataset_id}/stats", response_model=SuccessEnvelope[DatasetStatsResult])
async def get_dataset_stats(
    dataset_id: UUID,
    request: Request,
) -> SuccessEnvelope[DatasetStatsResult]:
    service = DatasetService(session_factory=app_postgres_session_factory(request.app))
    result = await service.get_stats(dataset_id)
    return SuccessEnvelope[DatasetStatsResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.delete(
    "/datasets/{dataset_id}",
    response_model=SuccessEnvelope[DatasetDeleteResult],
    responses={
        HTTPStatus.ACCEPTED: {
            "description": (
                "A new (or already-existing, still non-terminal) administrative "
                "delete run was accepted. Poll GET /api/v1/runs/{run_id} for the "
                "terminal state."
            )
        }
    },
)
async def delete_dataset(
    dataset_id: UUID,
    request: Request,
    response: Response,
) -> SuccessEnvelope[DatasetDeleteResult]:
    """Administrative Dataset deletion (SM-515, ADR-0010). Not an alias of
    ``POST /api/v1/forget``: this permanently retires the Dataset namespace
    (tombstone, ``name``/``slug`` reserved forever) rather than clearing its
    content while leaving it usable."""

    service = DatasetDeleteService(
        registry=app_pipeline_registry(request.app),
        worker=app_pipeline_worker(request.app),
        settings=app_settings(request.app),
        session_factory=app_postgres_session_factory(request.app),
    )
    control = await service.request_delete(dataset_id)
    response.status_code = control.http_status
    return SuccessEnvelope[DatasetDeleteResult](
        data=control.result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
