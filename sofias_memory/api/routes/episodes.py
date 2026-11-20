"""Contextual Episode API routes (SM-CTX-001, Contract v1 Contextual
Project Memory SS 17-22/75).

Exactly the five approved Episode operations -- no PATCH/PUT/DELETE, no
Checkpoint, no Contextual Recall route. Pure HTTP boundary: no SQL, no
pgvector query, no lifecycle logic (all in ``services.episodes``). Every
mutation requires ``Idempotency-Key``; a missing header is a 422
``INVALID_REQUEST``. Mutations are synchronous: no PipelineRun, no
graph_outbox, no Neo4j.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import (
    RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
    error_response,
)
from sofias_memory.domain import EpisodeLifecycle
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.episodes import (
    EPISODE_PAGE_DEFAULT_LIMIT,
    EPISODE_PAGE_MAX_LIMIT,
    ContextKey,
    ContextualScope,
    EpisodeCreateRequest,
    EpisodeInvalidateRequest,
    EpisodeKindFilter,
    EpisodeListResult,
    EpisodeResult,
)
from sofias_memory.services.episodes import EpisodeService

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Required caller-generated retry-safety key. Reusing it with the same logical "
    "request returns the original result without a duplicate; reusing it for "
    "different work returns 409 IDEMPOTENCY_CONFLICT. Keys starting with 'sys:' "
    "are reserved. The key is never stored in plain text."
)

_VALIDATION_422 = error_response(
    "Invalid request. ErrorEnvelope with error.code one of: INVALID_REQUEST (including a "
    "missing Idempotency-Key), INVALID_SCOPE, INVALID_CONTEXT_KEY, INVALID_EPISODE_KIND."
)
_EPISODE_NOT_FOUND_404 = error_response(
    "The target Episode does not exist. ErrorEnvelope with error.code=EPISODE_NOT_FOUND."
)
_IDEMPOTENCY_CONFLICT_409 = error_response(
    "The Idempotency-Key was already used for different work. ErrorEnvelope with "
    "error.code=IDEMPOTENCY_CONFLICT."
)
_EPISODE_MUTATION_CONFLICT_409 = error_response(
    "Conflict. ErrorEnvelope with error.code one of: IDEMPOTENCY_CONFLICT (the same "
    "Idempotency-Key was already used for different work) or EPISODE_STATE_CONFLICT "
    "(the Episode is FORGOTTEN)."
)
_DEPENDENCY_UNAVAILABLE_503 = error_response(
    "The embedding provider is unavailable or returned an unexpected vector dimension; "
    "no Episode was persisted. ErrorEnvelope with error.code=DEPENDENCY_UNAVAILABLE."
)

router = APIRouter(tags=["episodes"])

type IdempotencyKey = Annotated[
    str,
    Header(alias=IDEMPOTENCY_KEY_HEADER, min_length=1, description=IDEMPOTENCY_KEY_DESCRIPTION),
]


def _episode_service(request: Request) -> EpisodeService:
    settings = app_settings(request.app)
    return EpisodeService(
        settings,
        session_factory=app_postgres_session_factory(request.app),
        embedding_client=OpenAIEmbeddingClient(settings),
    )


def _envelope[T](data: T) -> SuccessEnvelope[T]:
    return SuccessEnvelope[T](data=data, meta=ResponseMeta(request_id=current_request_id()))


@router.post(
    "/episodes",
    response_model=SuccessEnvelope[EpisodeResult],
    status_code=HTTPStatus.CREATED,
    summary="Create an Episode",
    description=(
        "Explicitly preserve one historical experience (what happened) of a Project Scope. "
        "An Episode is not current truth and never creates a MemoryItem, SessionEntry, "
        "PipelineRun or graph projection. The embedding is computed before the short "
        "authoritative PostgreSQL transaction; on provider failure nothing is persisted. "
        "Omitted context_key means a project-wide Episode."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.CONFLICT: _IDEMPOTENCY_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
        HTTPStatus.SERVICE_UNAVAILABLE: _DEPENDENCY_UNAVAILABLE_503,
    },
)
async def create_episode(
    payload: EpisodeCreateRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[EpisodeResult]:
    result = await _episode_service(request).create(payload, idempotency_key=idempotency_key)
    return _envelope(result)


@router.get(
    "/episodes",
    response_model=SuccessEnvelope[EpisodeListResult],
    summary="List Episodes of a Project Scope",
    description=(
        "Bounded keyset pagination ordered by occurred_at DESC, episode_id ASC. Without "
        "lifecycle only ACTIVE Episodes are returned. FORGOTTEN tombstones no longer belong "
        "to any Project Scope and are never listed; read them by id."
    ),
    responses={HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422},
)
async def list_episodes(
    request: Request,
    scope: Annotated[ContextualScope, Query(description="Required 'project:<key>'.")],
    context_key: Annotated[
        ContextKey | None, Query(description="Optional exact context_key filter.")
    ] = None,
    kind: Annotated[EpisodeKindFilter, Query(description="Optional exact kind filter.")] = None,
    lifecycle: Annotated[
        EpisodeLifecycle | None, Query(description="Defaults to active only.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=EPISODE_PAGE_MAX_LIMIT)] = EPISODE_PAGE_DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(min_length=1, description="Opaque next_cursor of a previous page.")
    ] = None,
) -> SuccessEnvelope[EpisodeListResult]:
    result = await _episode_service(request).list_episodes(
        scope=scope,
        context_key=context_key,
        kind=kind,
        lifecycle=lifecycle,
        limit=limit,
        cursor=cursor,
    )
    return _envelope(result)


@router.get(
    "/episodes/{episode_id}",
    response_model=SuccessEnvelope[EpisodeResult],
    summary="Get an Episode",
    description=(
        "ACTIVE and INVALIDATED Episodes return their content; a FORGOTTEN Episode returns "
        "its tombstone with every semantic field null. Never calls the embedding provider."
    ),
    responses={HTTPStatus.NOT_FOUND: _EPISODE_NOT_FOUND_404},
)
async def get_episode(episode_id: UUID, request: Request) -> SuccessEnvelope[EpisodeResult]:
    return _envelope(await _episode_service(request).get(episode_id))


@router.post(
    "/episodes/{episode_id}/invalidate",
    response_model=SuccessEnvelope[EpisodeResult],
    summary="Invalidate an Episode",
    description=(
        "ACTIVE -> INVALIDATED (content preserved, excluded from normal listing). Already "
        "INVALIDATED is a 200 state no-op; FORGOTTEN is 409 EPISODE_STATE_CONFLICT. An "
        "optional replacement_episode_id must exist, not be forgotten and share the Project "
        "Scope; it is never created automatically."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: _EPISODE_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _EPISODE_MUTATION_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def invalidate_episode(
    episode_id: UUID,
    payload: EpisodeInvalidateRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[EpisodeResult]:
    result = await _episode_service(request).invalidate(
        episode_id, payload, idempotency_key=idempotency_key
    )
    return _envelope(result)


@router.post(
    "/episodes/{episode_id}/forget",
    response_model=SuccessEnvelope[EpisodeResult],
    summary="Forget an Episode",
    description=(
        "Destructive direct Forget: ACTIVE|INVALIDATED -> FORGOTTEN, scrubbing every "
        "semantic field, the embedding and external provenance refs. Already FORGOTTEN is a "
        "200 state no-op. There is no restore/unforget."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: _EPISODE_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _IDEMPOTENCY_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def forget_episode(
    episode_id: UUID,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[EpisodeResult]:
    result = await _episode_service(request).forget(episode_id, idempotency_key=idempotency_key)
    return _envelope(result)
