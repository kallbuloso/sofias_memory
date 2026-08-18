"""Read-only provenance API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.provenance import (
    QueryProvenanceResult,
    RelationProvenanceResult,
    SourceProvenanceResult,
)
from sofias_memory.services.provenance import ProvenanceService

router = APIRouter(tags=["provenance"])


@router.get(
    "/provenance/source/{source_id}", response_model=SuccessEnvelope[SourceProvenanceResult]
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


@router.get("/provenance/query/{query_id}", response_model=SuccessEnvelope[QueryProvenanceResult])
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
