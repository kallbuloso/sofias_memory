"""Synchronous Session management service (SM-602, ADR-0012)."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import InvalidSessionIdError, SessionStatus, normalize_session_id
from sofias_memory.infrastructure.postgres.models import Session
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.sessions import (
    SessionCreateRequest,
    SessionListResult,
    SessionResult,
    SessionUpdateRequest,
)


class SessionRepositoryForSessions(Protocol):
    async def add(self, session: Session) -> Session: ...
    async def get_by_id(self, session_id: UUID) -> Session | None: ...
    async def get_by_id_for_update(self, session_id: UUID) -> Session | None: ...
    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: SessionStatus | None = None,
        key: str | None = None,
    ) -> tuple[list[Session], int]: ...


class SessionUnitOfWork(Protocol):
    sessions: SessionRepositoryForSessions

    async def __aenter__(self) -> SessionUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], SessionUnitOfWork]


class SessionService:
    """Manage first-class Session lifecycle without touching SessionEntry,
    Query, or PipelineRun (out of scope for SM-602)."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def create_session(self, request: SessionCreateRequest) -> SessionResult:
        """Explicit create -- never `get_or_create_by_key` (SM-601's lazy
        primitive has different semantics: a caller here always wants a
        fresh Session, and an existing `session_id` is a conflict, not a
        silent reuse)."""

        now = utc_now()
        if request.session_id is not None:
            session_uuid = uuid4()
            external_key = request.session_id
        else:
            # The generated UUID textual form *is* the external key -- one
            # UUID, never two independently generated identifiers.
            session_uuid = uuid4()
            external_key = str(session_uuid)

        session = Session(
            id=session_uuid,
            key=external_key,
            name=request.name,
            status=SessionStatus.ACTIVE,
            metadata_=request.metadata,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        try:
            async with self._unit_of_work_factory() as uow:
                session = await uow.sessions.add(session)
                result = session_result(session)
                await uow.commit()
                return result
        except IntegrityError as exc:
            raise session_conflict_error() from exc

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
        status: SessionStatus | None,
        session_id: str | None,
    ) -> SessionListResult:
        normalized_key = _normalize_filter_session_id(session_id)
        async with self._unit_of_work_factory() as uow:
            sessions, total = await uow.sessions.list_paginated(
                limit=limit,
                offset=offset,
                status=status,
                key=normalized_key,
            )
            return SessionListResult(
                items=[session_result(session) for session in sessions],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_session(self, session_uuid: UUID) -> SessionResult:
        async with self._unit_of_work_factory() as uow:
            session = await require_session(uow, session_uuid)
            return session_result(session)

    async def update_session(
        self,
        session_uuid: UUID,
        request: SessionUpdateRequest,
    ) -> SessionResult:
        fields_set = request.model_fields_set
        async with self._unit_of_work_factory() as uow:
            session = await require_session_for_update(uow, session_uuid)
            if "name" in fields_set:
                session.name = request.name
            if "metadata" in fields_set:
                session.metadata_ = cast(dict[str, object], request.metadata)
            session.updated_at = utc_now()
            result = session_result(session)
            await uow.commit()
            return result

    async def archive_session(self, session_uuid: UUID) -> SessionResult:
        async with self._unit_of_work_factory() as uow:
            session = await require_session_for_update(uow, session_uuid)
            if session.status == SessionStatus.ACTIVE:
                now = utc_now()
                session.status = SessionStatus.ARCHIVED
                session.archived_at = now
                session.updated_at = now
            # Already archived: idempotent no-op, no timestamp churn.
            result = session_result(session)
            await uow.commit()
            return result

    async def restore_session(self, session_uuid: UUID) -> SessionResult:
        async with self._unit_of_work_factory() as uow:
            session = await require_session_for_update(uow, session_uuid)
            if session.status == SessionStatus.ARCHIVED:
                session.status = SessionStatus.ACTIVE
                session.archived_at = None
                session.updated_at = utc_now()
            # Already active: idempotent no-op, no timestamp churn.
            result = session_result(session)
            await uow.commit()
            return result


def _normalize_filter_session_id(session_id: str | None) -> str | None:
    try:
        return normalize_session_id(session_id)
    except InvalidSessionIdError as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="session_id filter is invalid.",
            details={"reason": exc.reason},
        ) from exc


async def require_session(uow: SessionUnitOfWork, session_uuid: UUID) -> Session:
    session = await uow.sessions.get_by_id(session_uuid)
    if session is None:
        raise session_not_found_error(session_uuid)
    return session


async def require_session_for_update(uow: SessionUnitOfWork, session_uuid: UUID) -> Session:
    session = await uow.sessions.get_by_id_for_update(session_uuid)
    if session is None:
        raise session_not_found_error(session_uuid)
    return session


def session_result(session: Session) -> SessionResult:
    return SessionResult(
        session_uuid=session.id,
        session_id=session.key,
        name=session.name,
        status=session.status,
        metadata=cast(dict[str, JSONValue], session.metadata_),
        created_at=session.created_at,
        updated_at=session.updated_at,
        archived_at=session.archived_at,
    )


def session_not_found_error(session_uuid: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Session does not exist.",
        details={"session_uuid": str(session_uuid)},
    )


def session_conflict_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.CONFLICT,
        message="Session with this session_id already exists.",
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> SessionUnitOfWork:
        return cast(SessionUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
