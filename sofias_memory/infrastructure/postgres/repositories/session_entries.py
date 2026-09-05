"""SessionEntry-specific PostgreSQL repository (ADR-0012, SM-601).

Foundation only: append + list. SessionEntry is append-only and has no
public API in SM-601 -- admission-barrier enforcement (rejecting an append
against an archived Session) is SM-603 scope.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import SessionEntry


class SessionEntryRepository:
    """Persistence operations for append-only Session contextual history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: SessionEntry) -> SessionEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

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
