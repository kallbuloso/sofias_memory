"""Improve route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import OpenAIDatasetSummaryClient, OpenAIDocumentSummaryClient
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
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from sofias_memory.services.improve import ImproveService
from sofias_memory.services.summary_rebuild_service import SummaryRebuildService

router = APIRouter(tags=["improve"])


@router.post("/improve", response_model=SuccessEnvelope[ImproveResult])
async def improve(
    payload: ImproveRequest,
    request: Request,
) -> SuccessEnvelope[ImproveResult]:
    settings = app_settings(request.app)
    session_factory = app_postgres_session_factory(request.app)
    neo4j_resource = app_neo4j_resource(request.app)
    projection = Neo4jProjection(neo4j_resource)
    embedding_client = OpenAIEmbeddingClient(settings)
    outbox_processor = GraphOutboxProcessor(
        session_factory=session_factory,
        projection=projection,
    )
    rebuild_service = GraphRebuildService(
        session_factory=session_factory,
        neo4j_resource=neo4j_resource,
        projection=projection,
    )
    service = ImproveService(
        settings,
        session_factory=session_factory,
        embedding_client=embedding_client,
        graph_projection_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=outbox_processor,
        ),
        graph_reconciliation=GraphReconciliationService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            rebuild_service=rebuild_service,
        ),
        summary_rebuild=SummaryRebuildService(
            settings,
            session_factory=session_factory,
            embedding_client=embedding_client,
            document_summary_client=OpenAIDocumentSummaryClient(settings),
            dataset_summary_client=OpenAIDatasetSummaryClient(settings),
        ),
    )
    result = await service.improve(payload)
    return SuccessEnvelope[ImproveResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
