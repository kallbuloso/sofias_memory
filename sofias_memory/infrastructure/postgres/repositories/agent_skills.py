"""AgentSkill-specific PostgreSQL repository (ADR-0014, SM-803)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Row, Select, Table, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.postgres.models import AgentSkill, Skill, SkillRevision

_CurrentRevision = aliased(SkillRevision)
_PinnedRevision = aliased(SkillRevision)
_EffectiveRevision = aliased(SkillRevision)

type _JoinedRow = tuple[
    UUID, str, SkillStatus, int, int | None, int, str, list[str], list[str], str | None, datetime
]


@dataclass(frozen=True, slots=True)
class AgentSkillRow:
    """One Agent<->Skill association, joined with exactly the progressive-
    disclosure columns the Feature Contract froze -- ``procedure``,
    ``metadata``, ``license``, ``content_sha256``, ``resolution_embedding``,
    and every internal ``SkillRevision.id`` are never selected, not merely
    dropped after loading (mirrors ``SkillRepository.ResolvedSkillCandidate``)."""

    skill_uuid: UUID
    name: str
    status: SkillStatus
    current_revision: int
    pinned_revision: int | None
    effective_revision: int
    description: str
    tags: list[str]
    declared_tools: list[str]
    compatibility: str | None
    association_created_at: datetime


def _joined_columns_statement() -> Select[_JoinedRow]:
    effective_revision_id = func.coalesce(AgentSkill.pinned_revision_id, Skill.current_revision_id)
    return (
        select(
            Skill.id.label("skill_uuid"),
            Skill.name,
            Skill.status,
            _CurrentRevision.revision.label("current_revision"),
            _PinnedRevision.revision.label("pinned_revision"),
            _EffectiveRevision.revision.label("effective_revision"),
            _EffectiveRevision.description,
            _EffectiveRevision.tags,
            _EffectiveRevision.declared_tools,
            _EffectiveRevision.compatibility,
            AgentSkill.created_at.label("association_created_at"),
        )
        .select_from(AgentSkill)
        .join(Skill, Skill.id == AgentSkill.skill_id)
        .join(_CurrentRevision, _CurrentRevision.id == Skill.current_revision_id)
        .outerjoin(_PinnedRevision, _PinnedRevision.id == AgentSkill.pinned_revision_id)
        .join(_EffectiveRevision, _EffectiveRevision.id == effective_revision_id)
    )


def _row_to_agent_skill_row(row: Row[_JoinedRow]) -> AgentSkillRow:
    return AgentSkillRow(
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


class AgentSkillRepository:
    """Persistence operations for the explicit Agent<->Skill association."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_pin(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
        pinned_revision_id: UUID | None,
    ) -> None:
        """Idempotent set/upsert: creates the association if absent, or
        updates only ``pinned_revision_id`` if present. ``created_at`` is
        never included in the ``DO UPDATE SET`` clause, so a re-pin/unpin/
        same-value replay never changes it -- only a genuinely new
        association (after a prior ``DELETE``) gets a new ``created_at``.
        A single atomic ``INSERT ... ON CONFLICT`` statement, not a
        SELECT-then-branch, so concurrent PUTs on the same (agent_id,
        skill_id) pair never race or duplicate."""

        statement = (
            pg_insert(cast(Table, AgentSkill.__table__))
            .values(
                agent_id=agent_id,
                skill_id=skill_id,
                pinned_revision_id=pinned_revision_id,
            )
            .on_conflict_do_update(
                index_elements=["agent_id", "skill_id"],
                set_={"pinned_revision_id": pinned_revision_id},
            )
        )
        await self._session.execute(statement)
        await self._session.flush()

    async def delete(self, *, agent_id: UUID, skill_id: UUID) -> None:
        """Idempotent: deleting a non-existent association is a no-op, not
        an error -- a plain ``DELETE ... WHERE`` naturally affects zero rows
        rather than raising."""

        statement = sql_delete(AgentSkill).where(
            AgentSkill.agent_id == agent_id,
            AgentSkill.skill_id == skill_id,
        )
        await self._session.execute(statement)
        await self._session.flush()

    async def get_one_for_agent_and_skill(
        self,
        *,
        agent_id: UUID,
        skill_id: UUID,
    ) -> AgentSkillRow | None:
        statement = _joined_columns_statement().where(
            AgentSkill.agent_id == agent_id,
            AgentSkill.skill_id == skill_id,
        )
        result = await self._session.execute(statement)
        row = result.one_or_none()
        return _row_to_agent_skill_row(row) if row is not None else None

    async def list_for_agent(self, agent_id: UUID) -> list[AgentSkillRow]:
        """Set-based -- one query, never one query per association
        (Feature Contract SS 31/32). Deterministic order: association
        ``created_at`` ascending, then ``Skill.id`` ascending."""

        statement = (
            _joined_columns_statement()
            .where(AgentSkill.agent_id == agent_id)
            .order_by(AgentSkill.created_at.asc(), Skill.id.asc())
        )
        result = await self._session.execute(statement)
        return [_row_to_agent_skill_row(row) for row in result.all()]
