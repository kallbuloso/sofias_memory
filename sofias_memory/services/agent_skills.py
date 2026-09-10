"""Agent <-> Skill association service (SM-803, ADR-0014).

Route -> :class:`AgentSkillService` -> ``PostgresUnitOfWork`` ->
``AgentSkillRepository``/``SkillRepository``/``SkillRevisionRepository``.
Separate from :class:`~sofias_memory.services.agents.AgentService` so Agent
Profile management and this association stay independently testable and
coupled only through the shared Unit of Work (Feature Contract SS 36).

Pure management persistence: never executes a Skill, never loads
``procedure`` for a prompt, never creates a ``SessionEntry``/``Query``/
``PipelineRun``, never calls an embedding provider or Neo4j.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.repositories.agent_skills import AgentSkillRow
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agent_skills import (
    AgentSkillListResult,
    AgentSkillResult,
    AgentSkillSetRequest,
)
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.agents import AgentRepositoryForAgents, require_agent
from sofias_memory.services.skills import skill_not_found_error


class SkillRepositoryForAgentSkills(Protocol):
    async def get_by_id(self, skill_id: UUID) -> Skill | None: ...


class SkillRevisionRepositoryForAgentSkills(Protocol):
    async def get_by_skill_and_revision(
        self, skill_id: UUID, revision: int
    ) -> SkillRevision | None: ...


class AgentSkillRepositoryForAgentSkills(Protocol):
    async def set_pin(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
        pinned_revision_id: UUID | None,
    ) -> None: ...
    async def delete(self, *, agent_id: UUID, skill_id: UUID) -> None: ...
    async def get_one_for_agent_and_skill(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
    ) -> AgentSkillRow | None: ...
    async def list_for_agent(self, agent_id: UUID) -> list[AgentSkillRow]: ...


class AgentSkillUnitOfWork(Protocol):
    agents: AgentRepositoryForAgents
    skills: SkillRepositoryForAgentSkills
    skill_revisions: SkillRevisionRepositoryForAgentSkills
    agent_skills: AgentSkillRepositoryForAgentSkills

    async def __aenter__(self) -> AgentSkillUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], AgentSkillUnitOfWork]


class AgentSkillService:
    """Manage the explicit Agent<->Skill association -- never authorization,
    never execution. No row lock is acquired anywhere here: the composite
    primary key `(agent_id, skill_id)` plus an atomic `INSERT ... ON
    CONFLICT` upsert already make every mutation concurrency-safe without
    locking the target Agent or Skill row (Feature Contract SS 23)."""

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

    async def list_skills(self, agent_uuid: UUID) -> AgentSkillListResult:
        """Management/disclosure list, not paginated (Feature Contract SS
        34) -- includes archived Skills associated to this Agent; permitted
        for both active and archived Agents (SS 39/42)."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            rows = await uow.agent_skills.list_for_agent(agent_uuid)
            return AgentSkillListResult(items=[agent_skill_result(row) for row in rows])

    async def set_skill(
        self,
        agent_uuid: UUID,
        skill_uuid: UUID,
        request: AgentSkillSetRequest,
    ) -> AgentSkillResult:
        """Idempotent set/upsert. `pinned_revision` (omitted or null) means
        follow-current; an integer must reference an existing revision of
        *this exact* Skill -- lookup is always `(skill_id, revision)`,
        never a global revision number (SS 12, 50)."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            skill = await uow.skills.get_by_id(skill_uuid)
            if skill is None:
                raise skill_not_found_error(skill_uuid)

            pinned_revision_id: UUID | None = None
            if request.pinned_revision is not None:
                revision = await uow.skill_revisions.get_by_skill_and_revision(
                    skill_uuid, request.pinned_revision
                )
                if revision is None:
                    raise invalid_pinned_revision_error(skill_uuid, request.pinned_revision)
                pinned_revision_id = revision.id

            await uow.agent_skills.set_pin(
                agent_id=agent_uuid,
                skill_id=skill_uuid,
                pinned_revision_id=pinned_revision_id,
            )
            row = await uow.agent_skills.get_one_for_agent_and_skill(
                agent_id=agent_uuid, skill_id=skill_uuid
            )
            if row is None:  # pragma: no cover - defensive: just written above
                raise skill_not_found_error(skill_uuid)
            result = agent_skill_result(row)
            await uow.commit()
            return result

    async def remove_skill(self, agent_uuid: UUID, skill_uuid: UUID) -> None:
        """Idempotent. Agent missing -> 404. Skill missing (the path itself
        addresses `{skill_uuid}` as a resource) -> 404. Agent and Skill both
        exist but no association row -> succeeds as a no-op, same as an
        existing association being removed (SS 14, 25)."""

        async with self._unit_of_work_factory() as uow:
            await require_agent(uow, agent_uuid)
            skill = await uow.skills.get_by_id(skill_uuid)
            if skill is None:
                raise skill_not_found_error(skill_uuid)
            await uow.agent_skills.delete(agent_id=agent_uuid, skill_id=skill_uuid)
            await uow.commit()


def invalid_pinned_revision_error(skill_uuid: UUID, pinned_revision: int) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="pinned_revision does not reference an existing revision of this Skill.",
        details={"skill_uuid": str(skill_uuid), "pinned_revision": pinned_revision},
    )


def agent_skill_result(row: AgentSkillRow) -> AgentSkillResult:
    return AgentSkillResult(
        skill_uuid=row.skill_uuid,
        name=row.name,
        status=row.status,
        current_revision=row.current_revision,
        pinned_revision=row.pinned_revision,
        effective_revision=row.effective_revision,
        description=row.description,
        tags=row.tags,
        declared_tools=row.declared_tools,
        compatibility=row.compatibility,
        association_created_at=row.association_created_at,
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> AgentSkillUnitOfWork:
        return cast(AgentSkillUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
