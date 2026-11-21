"""ContextCheckpoint API routes (SM-CTX-002/SM-CTX-003, Contract v1 Contextual
Project Memory SS 27-35/75, handoff 10 SS 48-64/79, handoff 11 SS 28/76).

Exactly the six SM-CTX-002 operations plus supersede (SM-CTX-003) -- no
PATCH/PUT/DELETE, no Contextual Recall. Pure HTTP boundary: no SQL, no locking, no
lifecycle/revision/idempotency logic (all in ``services.context_checkpoints``).
Every mutation requires ``Idempotency-Key``; a missing header is a 422
``INVALID_REQUEST``. Mutations are synchronous: no PipelineRun, no graph_outbox,
no Neo4j, no embedding.
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
from sofias_memory.domain import DEFAULT_CONTEXT_KEY, ContextCheckpointLifecycle
from sofias_memory.lifespan import app_postgres_session_factory, app_settings
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.context_checkpoints import (
    CHECKPOINT_PAGE_DEFAULT_LIMIT,
    CHECKPOINT_PAGE_MAX_LIMIT,
    ContextCheckpointCreateRequest,
    ContextCheckpointInvalidateRequest,
    ContextCheckpointListResult,
    ContextCheckpointResult,
    ContextCheckpointSupersedeRequest,
)
from sofias_memory.schemas.episodes import ContextKey, ContextualScope
from sofias_memory.services.context_checkpoints import ContextCheckpointService

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Required caller-generated retry-safety key. Reusing it with the same logical "
    "request returns the original result without a duplicate (even when the checkpoint "
    "it created is now current); reusing it for different work returns 409 "
    "IDEMPOTENCY_CONFLICT. Keys starting with 'sys:' are reserved. The key is never "
    "stored in plain text."
)

_VALIDATION_422 = error_response(
    "Invalid request. ErrorEnvelope with error.code one of: INVALID_REQUEST (including a "
    "missing Idempotency-Key or current_state), INVALID_SCOPE, INVALID_CONTEXT_KEY."
)
_CHECKPOINT_NOT_FOUND_404 = error_response(
    "The target checkpoint does not exist. ErrorEnvelope with error.code=CHECKPOINT_NOT_FOUND."
)
_CURRENT_NOT_FOUND_404 = error_response(
    "The Context Stream has no ACTIVE checkpoint (history is never returned instead). "
    "ErrorEnvelope with error.code=CURRENT_CHECKPOINT_NOT_FOUND."
)
_CREATE_CONFLICT_409 = error_response(
    "Conflict. ErrorEnvelope with error.code one of: IDEMPOTENCY_CONFLICT (the same "
    "Idempotency-Key was already used for different work) or CONTEXT_STATE_CONFLICT "
    "(the Context Stream already has an ACTIVE checkpoint)."
)
_INVALIDATE_CONFLICT_409 = error_response(
    "Conflict. ErrorEnvelope with error.code one of: IDEMPOTENCY_CONFLICT or "
    "CHECKPOINT_STATE_CONFLICT (the checkpoint is FORGOTTEN)."
)
_SUPERSEDE_CONFLICT_409 = error_response(
    "Conflict. ErrorEnvelope with error.code one of: IDEMPOTENCY_CONFLICT (the same "
    "Idempotency-Key was already used for different work) or CONTEXT_STATE_CONFLICT "
    "(stale writer: the target is not the current ACTIVE checkpoint of the payload's "
    "Context Stream; details carry expected_checkpoint_id and current_checkpoint_id)."
)
_FORGET_CONFLICT_409 = error_response(
    "Conflict. ErrorEnvelope with error.code one of: IDEMPOTENCY_CONFLICT or "
    "CHECKPOINT_STATE_CONFLICT."
)

router = APIRouter(tags=["context-checkpoints"])

type IdempotencyKey = Annotated[
    str,
    Header(alias=IDEMPOTENCY_KEY_HEADER, min_length=1, description=IDEMPOTENCY_KEY_DESCRIPTION),
]


def _service(request: Request) -> ContextCheckpointService:
    return ContextCheckpointService(
        app_settings(request.app),
        session_factory=app_postgres_session_factory(request.app),
    )


def _envelope[T](data: T) -> SuccessEnvelope[T]:
    return SuccessEnvelope[T](data=data, meta=ResponseMeta(request_id=current_request_id()))


@router.post(
    "/context/checkpoints",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    status_code=HTTPStatus.CREATED,
    summary="Create the current checkpoint of a Context Stream",
    description=(
        "Creates a new ACTIVE checkpoint for (scope, context_key) only when that stream has "
        "no ACTIVE checkpoint: revision 1 for a new stream, otherwise MAX(existing "
        "revision) + 1 (invalidated/forgotten history included). Never updates, merges, "
        "supersedes or reactivates history. Omitted context_key resolves to 'main'. "
        "Collection order is preserved."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.CONFLICT: _CREATE_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def create_checkpoint(
    payload: ContextCheckpointCreateRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[ContextCheckpointResult]:
    result = await _service(request).create_current(payload, idempotency_key=idempotency_key)
    return _envelope(result)


@router.get(
    "/context/checkpoints",
    response_model=SuccessEnvelope[ContextCheckpointListResult],
    summary="List checkpoints of a Project Scope",
    description=(
        "Bounded keyset pagination. Within one Context Stream: revision DESC, checkpoint_id "
        "ASC; across streams context_key ASC first. Without lifecycle only ACTIVE "
        "checkpoints are returned; history requires an explicit lifecycle filter."
    ),
    responses={HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422},
)
async def list_checkpoints(
    request: Request,
    scope: Annotated[ContextualScope, Query(description="Required 'project:<key>'.")],
    context_key: Annotated[
        ContextKey | None, Query(description="Optional exact Context Stream filter.")
    ] = None,
    lifecycle: Annotated[
        ContextCheckpointLifecycle | None, Query(description="Defaults to active only.")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=CHECKPOINT_PAGE_MAX_LIMIT)
    ] = CHECKPOINT_PAGE_DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(min_length=1, description="Opaque next_cursor of a previous page.")
    ] = None,
) -> SuccessEnvelope[ContextCheckpointListResult]:
    result = await _service(request).list_checkpoints(
        scope=scope, context_key=context_key, lifecycle=lifecycle, limit=limit, cursor=cursor
    )
    return _envelope(result)


@router.get(
    "/context/checkpoints/current",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    summary="Get the current checkpoint of a Context Stream",
    description=(
        "Returns the ACTIVE checkpoint of (scope, context_key). context_key defaults to "
        "'main'. Never falls back to an invalidated, forgotten or superseded checkpoint."
    ),
    responses={
        HTTPStatus.NOT_FOUND: _CURRENT_NOT_FOUND_404,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def get_current_checkpoint(
    request: Request,
    scope: Annotated[ContextualScope, Query(description="Required 'project:<key>'.")],
    context_key: Annotated[
        ContextKey, Query(description="Context Stream key; defaults to 'main'.")
    ] = DEFAULT_CONTEXT_KEY,
) -> SuccessEnvelope[ContextCheckpointResult]:
    result = await _service(request).get_current(scope=scope, context_key=context_key)
    return _envelope(result)


@router.get(
    "/context/checkpoints/{checkpoint_id}",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    summary="Get a checkpoint",
    description=(
        "ACTIVE and INVALIDATED checkpoints return their statements; a FORGOTTEN checkpoint "
        "returns its tombstone (no statements, no external provenance refs) while keeping "
        "scope, context_key and revision."
    ),
    responses={HTTPStatus.NOT_FOUND: _CHECKPOINT_NOT_FOUND_404},
)
async def get_checkpoint(
    checkpoint_id: UUID, request: Request
) -> SuccessEnvelope[ContextCheckpointResult]:
    return _envelope(await _service(request).get(checkpoint_id))


@router.post(
    "/context/checkpoints/{checkpoint_id}/supersede",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    status_code=HTTPStatus.CREATED,
    summary="Supersede the current checkpoint",
    description=(
        "Atomically replaces the ACTIVE checkpoint {checkpoint_id} (the caller's expected "
        "current) with a complete new snapshot: the target becomes SUPERSEDED with "
        "superseded_by pointing to the replacement, and the replacement is the new ACTIVE "
        "with revision = target revision + 1, new statement ids and new provenance. Not a "
        "patch: omitted optional fields are absent from the new revision. scope and the "
        "resolved context_key must match the target. A stale target (not ACTIVE) returns "
        "409 CONTEXT_STATE_CONFLICT and creates nothing; there is no automatic merge or "
        "retry. Returns the replacement."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: _CHECKPOINT_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _SUPERSEDE_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def supersede_checkpoint(
    checkpoint_id: UUID,
    payload: ContextCheckpointSupersedeRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[ContextCheckpointResult]:
    result = await _service(request).supersede(
        checkpoint_id, payload, idempotency_key=idempotency_key
    )
    return _envelope(result)


@router.post(
    "/context/checkpoints/{checkpoint_id}/invalidate",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    summary="Invalidate a checkpoint",
    description=(
        "ACTIVE or SUPERSEDED -> INVALIDATED (content and supersession lineage preserved). "
        "Invalidating the ACTIVE leaves the stream without a current checkpoint until a new "
        "explicit Create; invalidating history never affects its replacement. Already "
        "INVALIDATED is a 200 state no-op; FORGOTTEN is 409 CHECKPOINT_STATE_CONFLICT. The "
        "reason is persisted for audit."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: _CHECKPOINT_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _INVALIDATE_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def invalidate_checkpoint(
    checkpoint_id: UUID,
    payload: ContextCheckpointInvalidateRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[ContextCheckpointResult]:
    result = await _service(request).invalidate(
        checkpoint_id, payload, idempotency_key=idempotency_key
    )
    return _envelope(result)


@router.post(
    "/context/checkpoints/{checkpoint_id}/forget",
    response_model=SuccessEnvelope[ContextCheckpointResult],
    summary="Forget a checkpoint",
    description=(
        "Destructive direct Forget: ACTIVE|SUPERSEDED|INVALIDATED -> FORGOTTEN, deleting "
        "every statement and scrubbing external provenance refs and the invalidation reason. "
        "The tombstone keeps scope, context_key, revision and supersession lineage, so "
        "revisions are never reused. Never follows lineage to other checkpoints. Already "
        "FORGOTTEN is a 200 state no-op. No automatic fallback, no restore."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: RESERVED_IDEMPOTENCY_KEY_NAMESPACE_400,
        HTTPStatus.NOT_FOUND: _CHECKPOINT_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _FORGET_CONFLICT_409,
        HTTPStatus.UNPROCESSABLE_ENTITY: _VALIDATION_422,
    },
)
async def forget_checkpoint(
    checkpoint_id: UUID,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SuccessEnvelope[ContextCheckpointResult]:
    result = await _service(request).forget(checkpoint_id, idempotency_key=idempotency_key)
    return _envelope(result)
