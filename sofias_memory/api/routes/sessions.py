"""Session management API routes (SM-602, ADR-0012)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request

from sofias_memory.api.errors import current_request_id
from sofias_memory.api.openapi_responses import error_response
from sofias_memory.domain import SessionStatus
from sofias_memory.lifespan import app_postgres_session_factory
from sofias_memory.schemas.common import ResponseMeta, SuccessEnvelope
from sofias_memory.schemas.session_entries import (
    SESSION_ENTRY_PAGE_DEFAULT_LIMIT,
    SESSION_ENTRY_PAGE_MAX_LIMIT,
    SESSION_QUERY_PAGE_DEFAULT_LIMIT,
    SESSION_QUERY_PAGE_MAX_LIMIT,
    SessionEntryCreateRequest,
    SessionEntryListResult,
    SessionEntryResult,
    SessionQueryListResult,
)
from sofias_memory.schemas.sessions import (
    SESSION_PAGE_DEFAULT_LIMIT,
    SESSION_PAGE_MAX_LIMIT,
    SessionCreateRequest,
    SessionListResult,
    SessionResult,
    SessionUpdateRequest,
)
from sofias_memory.services.session_entries import SessionEntryService
from sofias_memory.services.sessions import SessionService

_SESSION_NOT_FOUND_404 = error_response(
    "The target Session does not exist. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SESSION_CREATE_CONFLICT_409 = error_response(
    "A Session with this session_id already exists. Explicit create never "
    "upserts. ErrorEnvelope with error.code=INVALID_REQUEST."
)
_SESSION_ENTRY_APPEND_CONFLICT_409 = error_response(
    "Conflict appending a SessionEntry. ErrorEnvelope with error.code one of: "
    "SESSION_ARCHIVED (the Session is archived and admits no new activity -- "
    "does not apply to a safe replay of already-admitted work), or "
    "IDEMPOTENCY_CONFLICT (external_id was already used with a different "
    "role/content/metadata payload)."
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


@router.post(
    "/sessions/{session_uuid}/entries",
    response_model=SuccessEnvelope[SessionEntryResult],
    status_code=HTTPStatus.CREATED,
    summary="Append a SessionEntry",
    description=(
        "Append explicit contextual history to a Session. Without "
        "`external_id`, every request creates a new entry (not idempotent). "
        "With `external_id`: a replay with an identical role/content/metadata "
        "payload resolves and returns the existing entry (still `201`, no "
        "second row, even against an archived Session); a payload conflict "
        "or a brand-new append against an archived Session is rejected."
    ),
    responses={
        HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404,
        HTTPStatus.CONFLICT: _SESSION_ENTRY_APPEND_CONFLICT_409,
    },
)
async def append_session_entry(
    session_uuid: UUID,
    payload: SessionEntryCreateRequest,
    request: Request,
) -> SuccessEnvelope[SessionEntryResult]:
    service = SessionEntryService(session_factory=app_postgres_session_factory(request.app))
    result = await service.append_entry(session_uuid, payload)
    return SuccessEnvelope[SessionEntryResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/sessions/{session_uuid}/entries",
    response_model=SuccessEnvelope[SessionEntryListResult],
    summary="List SessionEntries",
    description=(
        "List a Session's contextual history, paginated. Allowed for both "
        "active and archived Sessions. No semantic search."
    ),
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def list_session_entries(
    session_uuid: UUID,
    request: Request,
    limit: int = Query(
        default=SESSION_ENTRY_PAGE_DEFAULT_LIMIT, ge=1, le=SESSION_ENTRY_PAGE_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
    order: Annotated[
        Literal["asc", "desc"],
        Query(description="Sort direction over (created_at, entry_id)."),
    ] = "asc",
) -> SuccessEnvelope[SessionEntryListResult]:
    service = SessionEntryService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_entries(
        session_uuid,
        limit=limit,
        offset=offset,
        ascending=order == "asc",
    )
    return SuccessEnvelope[SessionEntryListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )


@router.get(
    "/sessions/{session_uuid}/queries",
    response_model=SuccessEnvelope[SessionQueryListResult],
    summary="List a Session's Query history",
    description=(
        "Lightweight, paginated projection of Queries associated with this "
        "Session (oldest first). Full provenance, references, timings, and "
        "session_context_entry_ids remain the responsibility of the existing "
        "Query Provenance endpoint. Allowed for both active and archived "
        "Sessions; never mutates the Session."
    ),
    responses={HTTPStatus.NOT_FOUND: _SESSION_NOT_FOUND_404},
)
async def list_session_queries(
    session_uuid: UUID,
    request: Request,
    limit: int = Query(
        default=SESSION_QUERY_PAGE_DEFAULT_LIMIT, ge=1, le=SESSION_QUERY_PAGE_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
) -> SuccessEnvelope[SessionQueryListResult]:
    service = SessionEntryService(session_factory=app_postgres_session_factory(request.app))
    result = await service.list_queries(session_uuid, limit=limit, offset=offset)
    return SuccessEnvelope[SessionQueryListResult](
        data=result,
        meta=ResponseMeta(request_id=current_request_id()),
    )
