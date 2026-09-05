"""Session management API routes (SM-602, ADR-0012)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.domain import SessionStatus
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.sessions import (
    SESSION_PAGE_DEFAULT_LIMIT,
    SESSION_PAGE_MAX_LIMIT,
    SessionCreateRequest,
    SessionListResult,
    SessionResult,
    SessionUpdateRequest,
)
from sofias_memory.services.sessions import SessionService

_SESSION_NOT_FOUND_404 = error_response(
    "The target Session does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SESSION_CREATE_CONFLICT_409 = error_response(
    "A Session with this session_id already exists. Explicit create never "
    "upserts. ErrorEnvelope with error.code=INVALID_REQUEST."
)

router = APIRouter(tags=["sessions"])


@router.post(
    "/sessions",
    response_model=SuccessEnvelope[SessionResult],
    status_code=HTTPStatus.CREATED,
    summary="Create a Session",
    description=(
        "Create a first-class durable Session explicitly. If `session_id` is "
        "omitted, the server generates a UUID and uses its textual form as "
        "both `session_uuid` and `session_id`. An already-existing "
        "`session_id` is a conflict, never a silent upsert."
    ),
    responses={HTTPStatus.CONFLICT: _SESSION_CREATE_CONFLICT_409},
)
async def create_session(
    payload: SessionCreateRequest,
    request: Request,
) -> SuccessEnvelope[SessionResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.create_session(payload)
    return SuccessEnvelope[SessionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/sessions",
    response_model=SuccessEnvelope[SessionListResult],
    summary="List Sessions",
    description="List Sessions, paginated, optionally filtered by status or exact session_id.",
)
async def list_sessions(
    request: Request,
    limit: int = Query(default=SESSION_PAGE_DEFAULT_LIMIT, ge=1, le=SESSION_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    status: Annotated[SessionStatus | None, Query()] = None,
    session_id: Annotated[
        str | None,
        Query(description="Exact, case-sensitive match against the external session_id."),
    ] = None,
) -> SuccessEnvelope[SessionListResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_sessions(
        limit=limit,
        offset=offset,
        status=status,
        session_id=session_id,
    )
    return SuccessEnvelope[SessionListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/sessions/{session_uuid}",
    response_model=SuccessEnvelope[SessionResult],
    summary="Get Session details",
    description="Get one Session's metadata by its structural session_uuid.",
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def get_session(
    session_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SessionResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.get_session(session_uuid)
    return SuccessEnvelope[SessionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.patch(
    "/sessions/{session_uuid}",
    response_model=SuccessEnvelope[SessionResult],
    summary="Update a Session",
    description=(
        "Update `name` and/or `metadata`. `session_id` and `status` are "
        "immutable here. `metadata` is replaced wholesale, never deep-merged. "
        "Permitted even when the Session is archived."
    ),
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def update_session(
    session_uuid: UUID,
    payload: SessionUpdateRequest,
    request: Request,
) -> SuccessEnvelope[SessionResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.update_session(session_uuid, payload)
    return SuccessEnvelope[SessionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/sessions/{session_uuid}/archive",
    response_model=SuccessEnvelope[SessionResult],
    summary="Archive a Session",
    description=(
        "Idempotent admission barrier: active -> archived, archived -> "
        "archived (no-op). Does not cancel PipelineRuns, does not touch "
        "SessionEntry/Query, and never affects Neo4j."
    ),
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def archive_session(
    session_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SessionResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.archive_session(session_uuid)
    return SuccessEnvelope[SessionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.post(
    "/sessions/{session_uuid}/restore",
    response_model=SuccessEnvelope[SessionResult],
    summary="Restore a Session",
    description="Idempotent: archived -> active, active -> active (no-op).",
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def restore_session(
    session_uuid: UUID,
    request: Request,
) -> SuccessEnvelope[SessionResult]:
    service = SessionService(session_factory=app_postgres_session_factory(request.app))
    result = await service.restore_session(session_uuid)
    return SuccessEnvelope[SessionResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
