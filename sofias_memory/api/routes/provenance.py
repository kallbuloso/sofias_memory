"""Read-only provenance API routes."""

from __future__ import annotations

from http import HTTPStatus
from uuid import UUID

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.provenance import (
    QueryProvenanceResult,
    RelationProvenanceResult,
    SourceProvenanceResult,
)
from sofias_memory.services.provenance import ProvenanceService

_SOURCE_NOT_FOUND_404 = error_response(
    "The source does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_RELATION_NOT_FOUND_404 = error_response(
    "The relation does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_QUERY_NOT_FOUND_404 = error_response(
    "The query does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["provenance"])


@router.get(
    "/provenance/source/{source_id}",
    response_model=SuccessEnvelope[SourceProvenanceResult],
    summary="Get source provenance",
    description="Trace a source back to its original document and ingestion metadata.",
    responses={HTTPStatus.NOT_FOUND: _SOURCE_NOT_FOUND_404},
)
async def provenance_source(
    source_id: UUID, request: Request
) -> SuccessEnvelope[SourceProvenanceResult]:
    service = _build_service(request)
    result = await service.source(source_id)
    return SuccessEnvelope[SourceProvenanceResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/provenance/relation/{relation_id}",
    response_model=SuccessEnvelope[RelationProvenanceResult],
    summary="Get relation provenance",
    description="Trace a graph relation back to the evidence chunk(s) it was extracted from.",
    responses={HTTPStatus.NOT_FOUND: _RELATION_NOT_FOUND_404},
)
async def provenance_relation(
    relation_id: UUID, request: Request
) -> SuccessEnvelope[RelationProvenanceResult]:
    service = _build_service(request)
    result = await service.relation(relation_id)
    return SuccessEnvelope[RelationProvenanceResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/provenance/query/{query_id}",
    response_model=SuccessEnvelope[QueryProvenanceResult],
    summary="Get query provenance",
    description="Trace a past Recall query back to the context/references it retrieved.",
    responses={HTTPStatus.NOT_FOUND: _QUERY_NOT_FOUND_404},
)
async def provenance_query(
    query_id: UUID, request: Request
) -> SuccessEnvelope[QueryProvenanceResult]:
    service = _build_service(request)
    result = await service.query(query_id)
    return SuccessEnvelope[QueryProvenanceResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


def _build_service(request: Request) -> ProvenanceService:
    settings = app_settings(request.app)
    return ProvenanceService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
    )
