"""AgentSession-specific PostgreSQL repository (ADR-0014, SM-804)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Row, Select, Table, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres.models import AgentSession, Session

type _JoinedRow = tuple[UUID, str, str | None, SessionStatus, datetime]


@dataclass(frozen=True, slots=True)
class AgentSessionRow:
    """One Agent<->Session association, joined with exactly the management
    disclosure columns the Feature Contract froze. Never includes any
    Session context/content column (``metadata``, timestamps beyond the
    association's own ``created_at``) -- this is a management list, never a
    transcript or provenance read."""

    session_uuid: UUID
    session_id: str
    name: str | None
    status: SessionStatus
    association_created_at: datetime


def _joined_columns_statement() -> Select[_JoinedRow]:
    return (
        select(
            Session.id.label("session_uuid"),
            Session.key.label("session_id"),
            Session.name,
            Session.status,
            AgentSession.created_at.label("association_created_at"),
        )
        .select_from(AgentSession)
        .join(Session, Session.id == AgentSession.session_id)
    )


def _row_to_agent_session_row(row: Row[_JoinedRow]) -> AgentSessionRow:
    return AgentSessionRow(
        session_uuid=row.session_uuid,
        session_id=row.session_id,
        name=row.name,
        status=row.status,
        association_created_at=row.association_created_at,
    )


class AgentSessionRepository:
    """Persistence operations for the explicit Agent<->Session association."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure(self, *, agent_id: UUID, session_id: UUID) -> None:
        """Idempotent ensure-association: creates the row if absent, does
        nothing if present. A single atomic ``INSERT ... ON CONFLICT DO
        NOTHING`` statement, not a SELECT-then-branch, so concurrent PUTs on
        the same (agent_id, session_id) pair never race or duplicate.
        ``created_at`` is only ever set on the INSERT branch -- a replay
        never touches the existing row at all."""

        statement = (
            pg_insert(cast(Table, AgentSession.__table__))
            .values(agent_id=agent_id, session_id=session_id)
            .on_conflict_do_nothing(index_elements=["agent_id", "session_id"])
        )
        await self._session.execute(statement)
        await self._session.flush()

    async def delete(self, *, agent_id: UUID, session_id: UUID) -> None:
        """Idempotent: deleting a non-existent association is a no-op, not
        an error -- a plain ``DELETE ... WHERE`` naturally affects zero rows
        rather than raising."""

        statement = sql_delete(AgentSession).where(
            AgentSession.agent_id == agent_id,
            AgentSession.session_id == session_id,
        )
        await self._session.execute(statement)
        await self._session.flush()

    async def get_one_for_agent_and_session(
        self,
        *,
        agent_id: UUID,
        session_id: UUID,
    ) -> AgentSessionRow | None:
        statement = _joined_columns_statement().where(
            AgentSession.agent_id == agent_id,
            AgentSession.session_id == session_id,
        )
        result = await self._session.execute(statement)
        row = result.one_or_none()
        return _row_to_agent_session_row(row) if row is not None else None

    async def list_for_agent(self, agent_id: UUID) -> list[AgentSessionRow]:
        """Set-based -- one query, never one query per association.
        Deterministic order: association ``created_at`` ascending, then
        ``Session.id`` ascending. Never filters by Session status --
        archived Sessions remain visible (Feature Contract SS 52)."""

        statement = (
            _joined_columns_statement()
            .where(AgentSession.agent_id == agent_id)
            .order_by(AgentSession.created_at.asc(), Session.id.asc())
        )
        result = await self._session.execute(statement)
        return [_row_to_agent_session_row(row) for row in result.all()]
