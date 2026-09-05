"""SessionEntry append/read service: admission barrier + safe replay (SM-603)."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres.models import Query, SessionEntry
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.session_entries import (
    SessionEntryCreateRequest,
    SessionEntryListResult,
    SessionEntryResult,
    SessionQueryListResult,
    SessionQuerySummaryResult,
)
from sofias_memory.services.sessions import (
    SessionRepositoryForSessions,
    require_session,
    require_session_for_update,
)


class SessionEntryRepositoryForEntries(Protocol):
    async def add(self, entry: SessionEntry) -> SessionEntry: ...
    async def get_by_external_id(
        self, session_id: UUID, external_id: str
    ) -> SessionEntry | None: ...
    async def list_by_session(
        self,
        session_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> list[SessionEntry]: ...
    async def count_by_session(self, session_id: UUID) -> int: ...


class QueryRepositoryForSessionHistory(Protocol):
    async def list_by_session(
        self,
        session_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Query]: ...
    async def count_by_session(self, session_id: UUID) -> int: ...


class SessionEntryUnitOfWork(Protocol):
    sessions: SessionRepositoryForSessions
    session_entries: SessionEntryRepositoryForEntries
    queries: QueryRepositoryForSessionHistory

    async def __aenter__(self) -> SessionEntryUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], SessionEntryUnitOfWork]


class SessionEntryService:
    """Append-only SessionEntry history plus the lightweight Session query
    history projection. Never touches Recall, Remember, or Neo4j."""

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

    async def append_entry(
        self,
        session_uuid: UUID,
        request: SessionEntryCreateRequest,
    ) -> SessionEntryResult:
        """Frozen admission order (Feature Contract SS 9 / backlog SM-603):

        1. lock the Session row (serializes with archive/restore/other appends);
        2. missing Session -> 404;
        3. an `external_id` present: look up an existing entry for
           (session_uuid, external_id) -- *before* checking archived status,
           so a safe replay of already-admitted work is never blocked by a
           since-archived Session;
        4/5/6. existing found: identical semantic payload -> return it
           (still 201, no new row); different payload -> 409
           IDEMPOTENCY_CONFLICT, existing entry untouched;
        7/8. no matching entry: only now does Session.status matter --
           archived -> 409 SESSION_ARCHIVED;
        9/10. active -> insert and commit.

        No `IntegrityError` recovery is attempted around the insert, and
        none is needed: `require_session_for_update`'s blocking
        `SELECT ... FOR UPDATE` (never `SKIP LOCKED`) fully serializes every
        append for this exact `session_uuid` -- a second concurrent caller
        cannot reach the insert below until the first has committed (or
        rolled back), at which point its own `get_by_external_id` lookup
        above already observes the committed row and resolves the replay/
        conflict decision *before* ever attempting a second insert. The
        `uq_session_entries_session_id_external_id` partial unique index
        therefore cannot be violated through this method's own protocol; an
        earlier revision wrapped this call in a catch-all
        `except IntegrityError` that mapped *any* constraint violation
        (including an unrelated FK/check failure or schema corruption) to
        `IDEMPOTENCY_CONFLICT`, which is both unreachable in practice and
        actively misleading if it were ever hit for a different reason -- a
        genuinely unexpected `IntegrityError` here should surface as the
        ordinary unhandled-exception 500, not be relabeled as a client
        conflict.
        """

        async with self._unit_of_work_factory() as uow:
            session = await require_session_for_update(uow, session_uuid)

            if request.external_id is not None:
                existing = await uow.session_entries.get_by_external_id(
                    session.id, request.external_id
                )
                if existing is not None:
                    if _semantic_payload_matches(existing, request):
                        result = session_entry_result(existing)
                        await uow.commit()
                        return result
                    raise session_entry_idempotency_conflict_error()

            if session.status == SessionStatus.ARCHIVED:
                raise session_archived_error(session_uuid)

            entry = SessionEntry(
                id=uuid4(),
                session_id=session.id,
                external_id=request.external_id,
                role=request.role,
                content=request.content,
                metadata_=request.metadata,
                created_at=utc_now(),
            )
            entry = await uow.session_entries.add(entry)
            result = session_entry_result(entry)
            await uow.commit()
            return result

    async def list_entries(
        self,
        session_uuid: UUID,
        *,
        limit: int,
        offset: int,
        ascending: bool,
    ) -> SessionEntryListResult:
        async with self._unit_of_work_factory() as uow:
            session = await require_session(uow, session_uuid)
            entries = await uow.session_entries.list_by_session(
                session.id,
                limit=limit,
                offset=offset,
                ascending=ascending,
            )
            total = await uow.session_entries.count_by_session(session.id)
            return SessionEntryListResult(
                items=[session_entry_result(entry) for entry in entries],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def list_queries(
        self,
        session_uuid: UUID,
        *,
        limit: int,
        offset: int,
    ) -> SessionQueryListResult:
        async with self._unit_of_work_factory() as uow:
            session = await require_session(uow, session_uuid)
            queries = await uow.queries.list_by_session(session.id, limit=limit, offset=offset)
            total = await uow.queries.count_by_session(session.id)
            return SessionQueryListResult(
                items=[session_query_summary(query) for query in queries],
                limit=limit,
                offset=offset,
                total=total,
            )


def _semantic_payload_matches(entry: SessionEntry, request: SessionEntryCreateRequest) -> bool:
    """Feature Contract SS 9: semantic payload is exactly role+content+metadata
    -- entry_id/created_at/external_id (the operation's own identity) never
    enter the comparison."""

    return (
        entry.role == request.role
        and entry.content == request.content
        and entry.metadata_ == request.metadata
    )


def session_entry_result(entry: SessionEntry) -> SessionEntryResult:
    return SessionEntryResult(
        entry_id=entry.id,
        session_uuid=entry.session_id,
        external_id=entry.external_id,
        role=entry.role,
        content=entry.content,
        metadata=cast(dict[str, JSONValue], entry.metadata_),
        created_at=entry.created_at,
    )


def session_query_summary(query: Query) -> SessionQuerySummaryResult:
    return SessionQuerySummaryResult(
        query_id=query.id,
        dataset_ids=list(query.dataset_ids),
        mode=query.mode,
        query_text=query.query_text,
        answer=query.answer,
        model=query.model,
        created_at=query.created_at,
    )


def session_archived_error(session_uuid: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.SESSION_ARCHIVED,
        status_code=HTTPStatus.CONFLICT,
        message="Session is archived; new activity is not admitted.",
        details={"session_uuid": str(session_uuid)},
    )


def session_entry_idempotency_conflict_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.IDEMPOTENCY_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message="external_id was already used for a SessionEntry with a different payload.",
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> SessionEntryUnitOfWork:
        return cast(SessionEntryUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
