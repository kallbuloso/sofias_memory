"""Agent Management service (SM-802, ADR-0014).

Route -> :class:`AgentService` -> ``PostgresUnitOfWork`` -> ``AgentRepository``.
Routes contain no SQL or lifecycle logic; the repository never produces HTTP
errors; this service is the only layer that translates domain/persistence
failures into :class:`~sofias_memory.api.errors.SofiasMemoryError`.

No embedding provider, no Neo4j, no network call of any kind -- every
mutation here is one short PostgreSQL transaction (mirrors
``services/sessions.py``, the primary precedent for this file).
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import AgentStatus
from sofias_memory.infrastructure.postgres.models import Agent
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agents import (
    AgentCreateRequest,
    AgentListItem,
    AgentListResult,
    AgentResult,
    AgentUpdateRequest,
)
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now

AGENT_NAME_UNIQUE_CONSTRAINT = "uq_agents_name"
"""The one PostgreSQL constraint whose violation may ever be reinterpreted
as an Agent-name conflict (mirrors
``services/skills.py.SKILL_NAME_UNIQUE_CONSTRAINT``). An ``IntegrityError``
from any OTHER constraint must always propagate unchanged."""


class AgentRepositoryForAgents(Protocol):
    async def add(self, agent: Agent) -> Agent: ...
    async def get_by_id(self, agent_id: UUID) -> Agent | None: ...
    async def get_by_id_for_update(self, agent_id: UUID) -> Agent | None: ...
    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: AgentStatus,
    ) -> tuple[list[Agent], int]: ...


class AgentUnitOfWork(Protocol):
    agents: AgentRepositoryForAgents

    async def __aenter__(self) -> AgentUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], AgentUnitOfWork]


class AgentService:
    """Manage durable Agent Profile identity and lifecycle -- never a
    runtime. No association (Skill/Session), no resolve, no execution."""

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

    async def create_agent(self, request: AgentCreateRequest) -> AgentResult:
        """Explicit create -- `name` already existing, in any status, is a
        conflict, never an upsert. The unique constraint is the sole
        concurrency authority: no pre-check `get_by_name()` gates this."""

        now = utc_now()
        agent = Agent(
            id=uuid4(),
            name=request.name,
            display_name=request.display_name,
            description=request.description,
            instructions=request.instructions,
            metadata_=cast(dict[str, object], request.metadata),
            status=AgentStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        try:
            async with self._unit_of_work_factory() as uow:
                agent = await uow.agents.add(agent)
                result = agent_result(agent)
                await uow.commit()
                return result
        except IntegrityError as exc:
            if _is_agent_name_unique_violation(exc):
                raise agent_conflict_error() from exc
            raise

    async def list_agents(
        self,
        *,
        limit: int,
        offset: int,
        status: AgentStatus,
    ) -> AgentListResult:
        async with self._unit_of_work_factory() as uow:
            agents, total = await uow.agents.list_paginated(
                limit=limit, offset=offset, status=status
            )
            return AgentListResult(
                items=[agent_list_item(agent) for agent in agents],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_agent(self, agent_uuid: UUID) -> AgentResult:
        async with self._unit_of_work_factory() as uow:
            agent = await require_agent(uow, agent_uuid)
            return agent_result(agent)

    async def update_agent(
        self,
        agent_uuid: UUID,
        request: AgentUpdateRequest,
    ) -> AgentResult:
        """Permitted on an archived Agent -- archive is a discovery filter,
        never an admission barrier over management (Feature Contract SS
        7/11). A syntactically valid PATCH always advances `updated_at`,
        even when the assigned value equals the prior one (SS 18) -- the
        same convention `SessionService.update_session` already uses."""

        fields_set = request.model_fields_set
        async with self._unit_of_work_factory() as uow:
            agent = await require_agent_for_update(uow, agent_uuid)
            if "display_name" in fields_set:
                agent.display_name = request.display_name
            if "description" in fields_set:
                agent.description = request.description
            if "instructions" in fields_set:
                agent.instructions = request.instructions
            if "metadata" in fields_set:
                agent.metadata_ = cast(dict[str, object], request.metadata)
            agent.updated_at = utc_now()
            result = agent_result(agent)
            await uow.commit()
            return result

    async def archive_agent(self, agent_uuid: UUID) -> AgentResult:
        async with self._unit_of_work_factory() as uow:
            agent = await require_agent_for_update(uow, agent_uuid)
            if agent.status == AgentStatus.ACTIVE:
                now = utc_now()
                agent.status = AgentStatus.ARCHIVED
                agent.archived_at = now
                agent.updated_at = now
            # Already archived: idempotent no-op, no timestamp churn.
            result = agent_result(agent)
            await uow.commit()
            return result

    async def restore_agent(self, agent_uuid: UUID) -> AgentResult:
        async with self._unit_of_work_factory() as uow:
            agent = await require_agent_for_update(uow, agent_uuid)
            if agent.status == AgentStatus.ARCHIVED:
                agent.status = AgentStatus.ACTIVE
                agent.archived_at = None
                agent.updated_at = utc_now()
            # Already active: idempotent no-op, no timestamp churn.
            result = agent_result(agent)
            await uow.commit()
            return result


def _is_agent_name_unique_violation(error: IntegrityError) -> bool:
    """Whether ``error`` was caused specifically by
    :data:`AGENT_NAME_UNIQUE_CONSTRAINT` -- read safely off the underlying
    DBAPI exception's ``constraint_name``, never guessed from the error
    message string (same pattern as
    ``services/skills.py._is_skill_name_unique_violation``)."""

    orig = error.orig
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return constraint_name == AGENT_NAME_UNIQUE_CONSTRAINT


async def require_agent(uow: AgentUnitOfWork, agent_uuid: UUID) -> Agent:
    agent = await uow.agents.get_by_id(agent_uuid)
    if agent is None:
        raise agent_not_found_error(agent_uuid)
    return agent


async def require_agent_for_update(uow: AgentUnitOfWork, agent_uuid: UUID) -> Agent:
    agent = await uow.agents.get_by_id_for_update(agent_uuid)
    if agent is None:
        raise agent_not_found_error(agent_uuid)
    return agent


def agent_result(agent: Agent) -> AgentResult:
    return AgentResult(
        agent_uuid=agent.id,
        name=agent.name,
        display_name=agent.display_name,
        description=agent.description,
        instructions=agent.instructions,
        metadata=cast(dict[str, JSONValue], agent.metadata_),
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        archived_at=agent.archived_at,
    )


def agent_list_item(agent: Agent) -> AgentListItem:
    return AgentListItem(
        agent_uuid=agent.id,
        name=agent.name,
        display_name=agent.display_name,
        description=agent.description,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        archived_at=agent.archived_at,
    )


def agent_not_found_error(agent_uuid: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Agent does not exist.",
        details={"agent_uuid": str(agent_uuid)},
    )


def agent_conflict_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.CONFLICT,
        message="Agent with this name already exists.",
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> AgentUnitOfWork:
        return cast(AgentUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
