"""Feedback API route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.feedback import FeedbackRequest, FeedbackResult
from sofias_memory.services.feedback import FeedbackService

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=SuccessEnvelope[FeedbackResult])
async def feedback(
    payload: FeedbackRequest,
    request: Request,
) -> SuccessEnvelope[FeedbackResult]:
    service = FeedbackService(session_factory=app_postgres_session_factory(request.app))
    result = await service.record(payload)
    return SuccessEnvelope[FeedbackResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
