"""Skill-specific PostgreSQL repository (ADR-0013, SM-701)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Skill


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
