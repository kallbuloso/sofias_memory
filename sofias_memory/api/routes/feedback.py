"""Feedback API route."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.feedback import FeedbackRequest, FeedbackResult
from sofias_memory.services.feedback import FeedbackService

_FEEDBACK_TARGET_MISMATCH_400 = error_response(
    "The target_type/target_id combination does not match the referenced "
    "query's actual result shape. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_FEEDBACK_QUERY_NOT_FOUND_404 = error_response(
    "The query does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["feedback"])


@router.post(
    "/feedback",
    response_model=SuccessEnvelope[FeedbackResult],
    summary="Record recall feedback",
    description=(
        "Record relevance feedback for a past Recall answer or one of its "
        "references. Persisted immediately, but only affects ranking the next "
        "time Improve's feedback_weights stage runs -- it does not change "
        "anything retroactively or in real time."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: _FEEDBACK_TARGET_MISMATCH_400,
        HTTPStatus.NOT_FOUND: _FEEDBACK_QUERY_NOT_FOUND_404,
    },
)
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
