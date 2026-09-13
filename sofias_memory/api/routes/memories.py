"""Native Cognitive Memory Create/Get API routes (SM-1002, ADR-0016, Feature
Contract v0.7.0 Native Cognitive Memory SS 12/13).

Create is synchronous and never creates a PipelineRun -- the external
embedding call always happens before the short authoritative PostgreSQL
transaction (``services.cognitive_memory.CognitiveMemoryService``). This
module is a pure HTTP boundary: it never executes SQL/Cypher itself
(AGENTS.md SS 7).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import (
    RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
    error_response,
)
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.memories import MemoryCreateRequest, MemoryItemResult
from sofias_memory.services.cognitive_memory import CognitiveMemoryService

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Optional retry-safety key for this write. Reusing the same key with the "
    "same logical request replays the original MemoryItem instead of creating "
    "a duplicate; reusing it for different work returns an idempotency "
    "conflict. Keys starting with 'sys:' are reserved. Without this header, "
    "every valid request is an independent create -- identical content never "
    "deduplicates."
)

_MEMORY_NOT_FOUND_404 = error_response(
    "The target MemoryItem does not exist. ErrorEnvelope with error.code=MEMORY_NOT_FOUND."
)
_MEMORY_IDEMPOTENCY_CONFLICT_409 = error_response(
    "The Idempotency-Key was already used for a different request. "
    "ErrorEnvelope with error.code=IDEMPOTENCY_CONFLICT."
)
_MEMORY_DEPENDENCY_UNAVAILABLE_503 = error_response(
    "The embedding provider is unavailable, or returned an unexpected vector "
    "dimension. ErrorEnvelope with error.code=DEPENDENCY_UNAVAILABLE."
)

router = APIRouter(tags=["memories"])


def _cognitive_memory_service(request: Request) -> CognitiveMemoryService:
    settings = app_settings(request.app)
    return CognitiveMemoryService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(settings),
    )


@router.post(
    "/memories",
    response_model=SuccessEnvelope[MemoryItemResult],
    status_code=HTTPStatus.CREATED,
    summary="Create a Cognitive Memory item",
    description=(
        "Synchronously create a native Cognitive Memory item (`profile` or "
        "`semantic`). Never creates a PipelineRun and never projects to "
        "Neo4j -- the external embedding call always happens before the "
        "short authoritative PostgreSQL transaction."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.CONFLICT: _MEMORY_IDEMPOTENCY_CONFLICT_409,
        HTTPStatus.SERVICE_UNAVAILABLE: _MEMORY_DEPENDENCY_UNAVAILABLE_503,
    },
)
async def create_memory(
    payload: MemoryCreateRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER, description=IDEMPOTENCY_KEY_DESCRIPTION),
    ] = None,
) -> SuccessEnvelope[MemoryItemResult]:
    service = _cognitive_memory_service(request)
    result = await service.create(payload, idempotency_key=idempotency_key)
    return SuccessEnvelope[MemoryItemResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/memories/{memory_id}",
    response_model=SuccessEnvelope[MemoryItemResult],
    summary="Get a Cognitive Memory item",
    description=(
        "Returns the identified MemoryItem -- an ACTIVE item today, or a "
        "future SUPERSEDED/FORGOTTEN tombstone shape once those lifecycle "
        "transitions exist. Never calls the embedding provider or reads "
        "Neo4j."
    ),
    responses={HTTPStatus.NOT_FOUND: _MEMORY_NOT_FOUND_404},
)
async def get_memory(memory_id: UUID, request: Request) -> SuccessEnvelope[MemoryItemResult]:
    service = _cognitive_memory_service(request)
    result = await service.get(memory_id)
    return SuccessEnvelope[MemoryItemResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
