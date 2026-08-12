"""Cognify route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import (
    OpenAIDocumentSummaryClient,
    OpenAIKnowledgeExtractionClient,
)
from sofias_memory.infrastructure.neo4j import Neo4jProjection
from sofias_memory.lifespan import (
    app_neo4j_resource,
    app_postgres_session_factory,
    app_settings,
)
from sofias_memory.schemas.cognify import CognifyRequest, CognifyResult
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor

router = APIRouter(tags=["cognify"])


@router.post("/cognify", response_model=SuccessEnvelope[CognifyResult])
async def cognify(
    payload: CognifyRequest,
    request: Request,
) -> SuccessEnvelope[CognifyResult]:
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    projection = Neo4jProjection(app_neo4j_resource(request.app))
    outbox_processor = GraphOutboxProcessor(
        session_factory=session_factory,
        projection=projection,
    )
    service = CognifyService(
        settings,
        session_factory=session_factory,
        embedding_client=OpenAIEmbeddingClient(settings),
        knowledge_extraction_client=OpenAIKnowledgeExtractionClient(settings),
        document_summary_client=OpenAIDocumentSummaryClient(settings),
        graph_projection_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=outbox_processor,
        ),
    )
    result = await service.cognify(payload)
    return SuccessEnvelope[CognifyResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
