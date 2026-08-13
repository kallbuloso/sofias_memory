"""Improve route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.infrastructure.neo4j import Neo4jProjection
from sofias_memory.lifespan import (
    app_neo4j_resource,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.improve import ImproveRequest, ImproveResult
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.improve import ImproveService

router = APIRouter(tags=["improve"])


@router.post("/improve", response_model=SuccessEnvelope[ImproveResult])
async def improve(
    payload: ImproveRequest,
    request: Request,
) -> SuccessEnvelope[ImproveResult]:
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    projection = Neo4jProjection(app_neo4j_resource(request.app))
    outbox_processor = GraphOutboxProcessor(
        session_factory=session_factory,
        projection=projection,
    )
    service = ImproveService(
        settings,
        session_factory=session_factory,
        graph_projection_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=outbox_processor,
        ),
    )
    result = await service.improve(payload)
    return SuccessEnvelope[ImproveResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
