"""Session-specific PostgreSQL repository (ADR-0012, SM-601)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres.models import Session


class SessionRepository:
    """Persistence operations for first-class durable Sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: Session) -> Session:
        self._session.add(session)
        await self._session.flush()
        return session

    async def get_by_id(self, session_id: UUID) -> Session | None:
        statement = select(Session).where(Session.id == session_id)
        result = await self._session.scalar(statement)
        return cast(Session | None, result)

    async def get_by_key(self, key: str) -> Session | None:
        statement = select(Session).where(Session.key == key)
        result = await self._session.scalar(statement)
        return cast(Session | None, result)

    async def get_by_id_for_update(self, session_id: UUID) -> Session | None:
        """Row-locked read for an admission decision (archive/append/etc.)."""

        statement = select(Session).where(Session.id == session_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(Session | None, result)

    async def get_or_create_by_key(self, candidate: Session) -> Session:
        """Lazily resolve ``candidate.key``, race-safe against a concurrent
        first-ever caller (Feature Contract SS 7: Recall/Remember lazy
        creation).

        Mirrors :meth:`DatasetRepository.get_or_create_by_slug`:
        ``INSERT ... ON CONFLICT (key) DO NOTHING`` + re-read, rather than
        get-then-add -- two concurrent transactions racing to create the same
        key for the first time never see an uncaught ``IntegrityError`` from
        this call. Never updates an existing Session; a key that already
        exists is returned unchanged, even if ``candidate``'s other fields
        differ.

        Inserts against ``Session.__table__`` (the Core ``Table``), not the
        ORM class: ``pg_insert(Session).values(metadata=...)`` resolves the
        ``metadata`` keyword against the reserved ``Session.metadata``
        declarative class attribute (a ``MetaData`` instance, not the mapped
        column) instead of the actual ``metadata`` column, which is exactly
        the collision ``metadata_`` exists to avoid.
        """

        statement = (
            pg_insert(cast(Table, Session.__table__))
            .values(
                id=candidate.id,
                key=candidate.key,
                name=candidate.name,
                status=candidate.status,
                metadata=candidate.metadata_,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        await self._session.execute(statement)
        await self._session.flush()
        resolved = await self.get_by_key(candidate.key)
        assert resolved is not None  # noqa: S101 - just inserted or already existed
        return resolved

    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: SessionStatus | None = None,
        key: str | None = None,
    ) -> tuple[list[Session], int]:
        statement = select(Session)
        total_statement = select(func.count()).select_from(Session)
        if status is not None:
            statement = statement.where(Session.status == status)
            total_statement = total_statement.where(Session.status == status)
        if key is not None:
            statement = statement.where(Session.key == key)
            total_statement = total_statement.where(Session.key == key)
        statement = statement.order_by(Session.created_at, Session.id).limit(limit).offset(offset)
        result = await self._session.scalars(statement)
        total = await self._session.scalar(total_statement)
        return list(result), int(total or 0)
