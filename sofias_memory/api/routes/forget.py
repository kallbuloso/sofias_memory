"""Forget API route."""

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
from sofias_memory.schemas.forget import ForgetRequest, ForgetResult
from sofias_memory.services.forget import ForgetService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor

router = APIRouter(tags=["forget"])


@router.post("/forget", response_model=SuccessEnvelope[ForgetResult])
async def forget(
    payload: ForgetRequest,
    request: Request,
) -> SuccessEnvelope[ForgetResult]:
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    projection = Neo4jProjection(app_neo4j_resource(request.app))
    processor = GraphOutboxProcessor(session_factory=session_factory, projection=projection)
    service = ForgetService(
        settings,
        session_factory=session_factory,
        graph_projection_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=processor,
        ),
    )
    result = await service.forget_source(payload)
    return SuccessEnvelope[ForgetResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
