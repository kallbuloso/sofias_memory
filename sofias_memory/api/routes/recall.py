"""Recall API route."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import DATASET_NOT_FOUND_404, error_response
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import OpenAIRagAnswerClient
from sofias_memory.infrastructure.neo4j.recall import Neo4jGraphRecall
from sofias_memory.lifespan import app_neo4j_resource, app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.recall import RecallRequest, RecallResult
from sofias_memory.services.recall import RecallService

_RECALL_TOP_K_400 = error_response(
    "top_k exceeds the configured maximum. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_RECALL_DEPENDENCY_UNAVAILABLE_503 = error_response(
    "A required dependency (the embedding provider, Neo4j for graph/triplets/hybrid "
    "modes, or the LLM for mode=rag) is unavailable. ErrorEnvelope with "
    "error.code=DEPENDENCY_UNAVAILABLE."
)

router = APIRouter(tags=["recall"])


@router.post(
    "/recall",
    response_model=SuccessEnvelope[RecallResult],
    summary="Recall memory",
    description=(
        "Retrieve stored knowledge for a query across one or more datasets. "
        "Synchronous -- no PipelineRun is created. Choose `mode` based on what "
        "you need: `chunks`/`summaries` for raw retrieved text, `graph`/`triplets` "
        "for structured entities and relations, `hybrid` to combine retrieval "
        "signals, or `rag` (default) for hybrid retrieval plus a generated, "
        "provenance-backed answer -- the only mode that calls the LLM."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: _RECALL_TOP_K_400,
        HTTPStatus.NOT_FOUND: DATASET_NOT_FOUND_404,
        HTTPStatus.SERVICE_UNAVAILABLE: _RECALL_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def recall(payload: RecallRequest, request: Request) -> SuccessEnvelope[RecallResult]:
    settings = app_settings(request.app)
    graph_recall_client = (
        Neo4jGraphRecall(app_neo4j_resource(request.app))
        if payload.mode in {"graph", "triplets", "hybrid"}
        else None
    )
    service = RecallService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(settings),
        rag_answer_client=OpenAIRagAnswerClient(settings),
        graph_recall_client=graph_recall_client,
    )
    result = await service.recall(payload)
    return SuccessEnvelope[RecallResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
