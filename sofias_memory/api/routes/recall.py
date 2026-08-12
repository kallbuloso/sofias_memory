"""Recall API route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import OpenAIRagAnswerClient
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.recall import RecallRequest, RecallResult
from sofias_memory.services.recall import RecallService

router = APIRouter(tags=["recall"])


@router.post("/recall", response_model=SuccessEnvelope[RecallResult])
async def recall(payload: RecallRequest, request: Request) -> SuccessEnvelope[RecallResult]:
    settings = app_settings(request.app)
    service = RecallService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(settings),
        rag_answer_client=OpenAIRagAnswerClient(settings),
    )
    result = await service.recall(payload)
    return SuccessEnvelope[RecallResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
