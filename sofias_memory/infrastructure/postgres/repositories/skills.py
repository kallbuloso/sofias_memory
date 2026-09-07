"""Skill-specific PostgreSQL repository (ADR-0013, SM-701/SM-702)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision


class SkillRepository:
    """Persistence operations for first-class durable procedural Skills."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, skill: Skill) -> Skill:
        self._session.add(skill)
        await self._session.flush()
        return skill

    async def get_by_id(self, skill_id: UUID) -> Skill | None:
        statement = select(Skill).where(Skill.id == skill_id)
        result = await self._session.scalar(statement)
        return cast(Skill | None, result)

    async def get_by_name(self, name: str) -> Skill | None:
        statement = select(Skill).where(Skill.name == name)
        result = await self._session.scalar(statement)
        return cast(Skill | None, result)

    async def get_by_id_for_update(self, skill_id: UUID) -> Skill | None:
        """Row-locked read serializing every revision/pointer-mutating
        operation for this Skill (create revision, ``current_revision``
        rollback, archive, restore -- Feature Contract SS 6.2). Callers must
        acquire this lock before reading or writing ``SkillRevision`` rows
        for the same Skill; this method itself takes no lock on
        ``skill_revisions``."""

        statement = select(Skill).where(Skill.id == skill_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(Skill | None, result)

    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: SkillStatus | None = None,
    ) -> tuple[list[tuple[Skill, SkillRevision]], int]:
        """Each Skill paired with its *current* SkillRevision (joined on
        ``Skill.current_revision_id == SkillRevision.id``) -- a single
        query, never an N+1 per-Skill lookup. Ordered by
        ``(created_at, id)``, the same deterministic-tiebreak convention
        Session/Dataset listing already use."""

        statement = select(Skill, SkillRevision).join(
            SkillRevision, Skill.current_revision_id == SkillRevision.id
        )
        total_statement = select(func.count()).select_from(Skill)
        if status is not None:
            statement = statement.where(Skill.status == status)
            total_statement = total_statement.where(Skill.status == status)
        statement = statement.order_by(Skill.created_at, Skill.id).limit(limit).offset(offset)
        result = await self._session.execute(statement)
        pairs = [(row[0], row[1]) for row in result.all()]
        total = await self._session.scalar(total_statement)
        return pairs, int(total or 0)
