"""SkillRevision-specific PostgreSQL repository (ADR-0013, SM-701)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import SkillRevision


class SkillRevisionRepository:
    """Persistence operations for immutable, append-only SkillRevisions.

    No update/delete methods: a SkillRevision is immutable once created
    (Feature Contract SS 4.2)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: SkillRevision) -> SkillRevision:
        self._session.add(revision)
        await self._session.flush()
        return revision

    async def get_by_skill_and_revision(
        self, skill_id: UUID, revision: int
    ) -> SkillRevision | None:
        statement = select(SkillRevision).where(
            SkillRevision.skill_id == skill_id,
            SkillRevision.revision == revision,
        )
        result = await self._session.scalar(statement)
        return cast(SkillRevision | None, result)

    async def get_by_skill_and_content_sha256(
        self, skill_id: UUID, content_sha256: str
    ) -> SkillRevision | None:
        statement = select(SkillRevision).where(
            SkillRevision.skill_id == skill_id,
            SkillRevision.content_sha256 == content_sha256,
        )
        result = await self._session.scalar(statement)
        return cast(SkillRevision | None, result)

    async def list_for_skill(
        self, skill_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[SkillRevision]:
        statement = (
            select(SkillRevision)
            .where(SkillRevision.skill_id == skill_id)
            .order_by(SkillRevision.revision)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def next_revision_number(self, skill_id: UUID) -> int:
        """The next monotonic ``revision`` ordinal for this Skill.

        Concurrency-safe **only** when the caller already holds the Skill
        row lock (:meth:`SkillRepository.get_by_id_for_update`) for the
        duration of the transaction -- this method itself takes no lock;
        it relies entirely on the caller's Skill-row lock to serialize
        writers of the same Skill's revisions (Feature Contract SS 6.2/6.3).
        """

        statement = select(func.max(SkillRevision.revision)).where(
            SkillRevision.skill_id == skill_id
        )
        current_max = await self._session.scalar(statement)
        return int(current_max or 0) + 1
