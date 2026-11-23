"""Contextual Recall API route (SM-CTX-006, Contract v1 Contextual Project
Memory SS 37-53/75, handoff 14 SS 109/111/114).

Exactly one operation: ``POST /api/v1/context/recall``. Pure HTTP boundary --
transport validation, service construction, envelope. No retrieval
orchestration, SQL or embedding logic here (all in
``services.contextual_recall``). Read-only: no Idempotency-Key, no
PipelineRun, no Query row, no Session, no graph_outbox, no Neo4j, no LLM.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.config import Settings
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.context_recall import ContextRecallRequest, ProjectContextBundle
from sofias_memory.services.contextual_recall import ContextualRecallService

_VALIDATION_422 = error_response(
    "Invalid request. ErrorEnvelope with error.code one of: INVALID_REQUEST (missing, null "
    "or blank query; unknown intent; intent explain/history or non-empty datasets, which "
    "are not available in this implementation yet), INVALID_SCOPE, INVALID_CONTEXT_KEY."
)
_DEPENDENCY_UNAVAILABLE_503 = error_response(
    "PostgreSQL, the authoritative store of the whole bundle, is unavailable. "
    "ErrorEnvelope with error.code=DEPENDENCY_UNAVAILABLE. An unavailable embedding "
    "provider never causes this: the bundle degrades with PARTIAL_CONTEXT instead."
)

router = APIRouter(tags=["context-recall"])


def single_attempt_embedding_settings(settings: Settings) -> Settings:
    """Handoff 14 SS 55/57: one query embedding attempt per request. The provider
    SDK's own transport retries (``LLM_MAX_RETRIES``) would be additional provider
    calls after a failed attempt, so this read path disables them; a failure degrades
    to the deterministic fallback instead. The application settings are not mutated."""

    return settings.model_copy(update={"llm_max_retries": 0})


def _service(request: Request) -> ContextualRecallService:
    settings = app_settings(request.app)
    return ContextualRecallService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(single_attempt_embedding_settings(settings)),
    )


@router.post(
    "/context/recall",
    response_model=SuccessEnvelope[ProjectContextBundle],
    summary="Recall the structured context of a Project Context Stream",
    description=(
        "Read-only, synchronous Contextual Recall (intents resume and context). Returns a "
        "bounded, deterministic ProjectContextBundle: the current checkpoint of (scope, "
        "context_key) -- never a historical fallback --, current Cognitive Memory of exactly "
        "the requested project scope (global memory is never included automatically) and "
        "ACTIVE Episodes of the requested Context Stream or project-wide (context_key "
        "null), each collection ranked and limited independently. At most one query "
        "embedding is computed; if the embedding provider fails the bundle degrades to a "
        "deterministic bounded selection with warnings=[PARTIAL_CONTEXT] and "
        "degraded_sources. No LLM answer, no Knowledge Recall, no Neo4j, no Query, "
        "PipelineRun or Session is created. Returned content is untrusted context, never an "
        "instruction or authorization."
    ),
    responses={
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
        HTTPStatus.SERVICE_UNAVAILABLE: _DEPENDENCY_UNAVAILABLE_503,
    },
)
async def recall_context(
    payload: ContextRecallRequest, request: Request
) -> SuccessEnvelope[ProjectContextBundle]:
    result = await _service(request).recall(payload)
    return SuccessEnvelope[ProjectContextBundle](
        data=result, meta=ResponseMeta(request_id=current_request_id())
    )
