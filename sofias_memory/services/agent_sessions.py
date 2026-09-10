"""Agent <-> Session association service (SM-804, ADR-0014).

Route -> :class:`AgentSessionService` -> ``PostgresUnitOfWork`` ->
``AgentSessionRepository``/``SessionRepository``. Separate from
:class:`~sofias_memory.services.agents.AgentService` and
:class:`~sofias_memory.services.agent_skills.AgentSkillService` so each
concern stays independently testable, coupled only through the shared Unit
of Work.

``agent_sessions`` is a **current explicit management association only**.
Never provenance, never historical participation, never operation
attribution. This service never creates a ``SessionEntry``, ``Query``, or
``PipelineRun``, never touches ``Session.updated_at``/``Session.status``,
and never bypasses the Session admission barrier (ADR-0012) -- associating
an Agent with an archived Session is plain management metadata, not new
Session activity.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.infrastructure.postgres.models import Session
from sofias_memory.infrastructure.postgres.repositories.agent_sessions import AgentSessionRow
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agent_sessions import AgentSessionListResult, AgentSessionResult
from sofias_memory.services.agents import AgentRepositoryForAgents, require_agent
from sofias_memory.services.sessions import session_not_found_error


class SessionRepositoryForAgentSessions(Protocol):
    async def get_by_id(self, session_id: UUID) -> Session | None: ...


class AgentSessionRepositoryForAgentSessions(Protocol):
    async def ensure(self, *, agent_id: UUID, session_id: UUID) -> None: ...
    async def delete(self, *, agent_id: UUID, session_id: UUID) -> None: ...
    async def get_one_for_agent_and_session(
        self,
        *,
        agent_id: UUID,
        session_id: UUID,
    ) -> AgentSessionRow | None: ...
    async def list_for_agent(self, agent_id: UUID) -> list[AgentSessionRow]: ...


class AgentSessionUnitOfWork(Protocol):
    agents: AgentRepositoryForAgents
    sessions: SessionRepositoryForAgentSessions
    agent_sessions: AgentSessionRepositoryForAgentSessions

    async def __aenter__(self) -> AgentSessionUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], AgentSessionUnitOfWork]


class AgentSessionService:
    """Manage the explicit Agent<->Session association. No row lock is
    acquired anywhere here: the composite primary key `(agent_id,
    session_id)` plus an atomic `INSERT ... ON CONFLICT DO NOTHING` already
    make every mutation concurrency-safe without locking the target Agent
    or Session row."""

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

    async def list_sessions(self, agent_uuid: UUID) -> AgentSessionListResult:
        """Management/disclosure list, not paginated -- includes archived
        Sessions associated to this Agent; permitted for both active and
        archived Agents. Never returns SessionEntry/Query/PipelineRun
        content."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            rows = await uow.agent_sessions.list_for_agent(agent_uuid)
            return AgentSessionListResult(items=[agent_session_result(row) for row in rows])

    async def associate_session(self, agent_uuid: UUID, session_uuid: UUID) -> AgentSessionResult:
        """Idempotent ensure-association. Permitted regardless of Agent or
        Session status -- associating with an archived Session is
        management metadata, never new Session activity, so it does not
        touch the ADR-0012 admission barrier."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            session = await uow.sessions.get_by_id(session_uuid)
            if session is None:
                raise session_not_found_error(session_uuid)

            await uow.agent_sessions.ensure(agent_id=agent_uuid, session_id=session_uuid)
            row = await uow.agent_sessions.get_one_for_agent_and_session(
                agent_id=agent_uuid, session_id=session_uuid
            )
            if row is None:  # pragma: no cover - defensive: just written above
                raise session_not_found_error(session_uuid)
            result = agent_session_result(row)
            await uow.commit()
            return result

    async def remove_session(self, agent_uuid: UUID, session_uuid: UUID) -> None:
        """Idempotent. Agent missing -> 404. No Session existence pre-check
        is required: the association's own identity is `(agent_id,
        session_id)`, and a plain `DELETE ... WHERE` naturally succeeds as
        a no-op when the pair does not exist, whether because the
        association was never created or because the Session UUID does not
        reference anything at all."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            await uow.agent_sessions.delete(agent_id=agent_uuid, session_id=session_uuid)
            await uow.commit()


def agent_session_result(row: AgentSessionRow) -> AgentSessionResult:
    return AgentSessionResult(
        session_uuid=row.session_uuid,
        session_id=row.session_id,
        name=row.name,
        status=row.status,
        association_created_at=row.association_created_at,
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> AgentSessionUnitOfWork:
        return cast(AgentSessionUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
