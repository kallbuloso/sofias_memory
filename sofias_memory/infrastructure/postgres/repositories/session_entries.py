"""SessionEntry-specific PostgreSQL repository (ADR-0012, SM-601/SM-603)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import SessionEntry


class SessionEntryRepository:
    """Persistence operations for append-only Session contextual history.

    No update/delete methods: SessionEntry is append-only for the lifetime
    of the v0.3.0 contract.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: SessionEntry) -> SessionEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def get_by_external_id(
        self,
        session_id: UUID,
        external_id: str,
    ) -> SessionEntry | None:
        """Used by the SM-603 safe-replay decision. Relies on the caller
        already holding the Session row lock (``get_by_id_for_update``) for
        its concurrency guarantee -- this method itself takes no lock."""

        statement = select(SessionEntry).where(
            SessionEntry.session_id == session_id,
            SessionEntry.external_id == external_id,
        )
        result = await self._session.scalar(statement)
        return cast(SessionEntry | None, result)

    async def list_by_session(
        self,
        session_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> list[SessionEntry]:
        """Deterministic ``(created_at, id)`` ordering, oldest-first by default."""

        order = (
            (SessionEntry.created_at.asc(), SessionEntry.id.asc())
            if ascending
            else (SessionEntry.created_at.desc(), SessionEntry.id.desc())
        )
        statement = (
            select(SessionEntry)
            .where(SessionEntry.session_id == session_id)
            .order_by(*order)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def count_by_session(self, session_id: UUID) -> int:
        statement = (
            select(func.count())
            .select_from(SessionEntry)
            .where(SessionEntry.session_id == session_id)
        )
        result = await self._session.scalar(statement)
        return int(result or 0)
