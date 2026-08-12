"""Remember text ingest route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.remember import RememberTextRequest, RememberTextResult
from sofias_memory.services.remember import RememberService

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

router = APIRouter(tags=["remember"])


@router.post("/remember", response_model=SuccessEnvelope[RememberTextResult])
async def remember_text(
    payload: RememberTextRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> SuccessEnvelope[RememberTextResult]:
    service = RememberService(
        app_settings(request.app),
        session_factory=app_postgres_session_factory(request.app),
    )
    result = await service.remember_text(payload, idempotency_key=idempotency_key)
    return SuccessEnvelope[RememberTextResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
