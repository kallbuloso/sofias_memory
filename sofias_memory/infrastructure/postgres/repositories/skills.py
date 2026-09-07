"""Skill-specific PostgreSQL repository (ADR-0013, SM-701/SM-702/SM-704)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import cast as sql_cast
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


@dataclass(frozen=True, slots=True)
class ResolvedSkillCandidate:
    """One ranked ``resolve`` candidate -- exactly the progressive-disclosure
    columns (Feature Contract SS 11), read directly by column rather than
    loading a full ``Skill``/``SkillRevision`` ORM pair: ``procedure``,
    ``metadata``, ``license``, ``content_sha256``, and
    ``resolution_embedding`` are never selected, not merely dropped after
    loading."""

    skill_uuid: UUID
    name: str
    description: str
    current_revision: int
    tags: list[str]
    declared_tools: list[str]
    compatibility: str | None
    score: float


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

    async def resolve_active_current(
        self, *, query_embedding: list[float], top_k: int
    ) -> list[ResolvedSkillCandidate]:
        """Rank ``active`` Skills' *current* revision by cosine similarity
        to ``query_embedding`` (Feature Contract SS 10) -- read-only, no
        row lock, ranking done entirely in PostgreSQL via the
        ``halfvec_cosine_ops`` HNSW expression index the SM-701 migration
        created on ``resolution_embedding`` (ADR-0006): the query casts both
        the column and the parameter to ``halfvec(3072)`` with the same
        ``<=>`` operator the index expression uses, so the planner can use
        the index instead of a full sequential scan.

        No hidden threshold: every candidate up to ``top_k`` is returned,
        ordered by distance ascending (``Skill.id`` as a deterministic
        tie-break) -- callers see ``score = 1.0 - cosine_distance``, so a
        higher score always means more similar.
        """

        distance = sql_cast(
            SkillRevision.resolution_embedding, HALFVEC(EMBEDDING_DIMENSIONS)
        ).cosine_distance(query_embedding)
        statement = (
            select(
                Skill.id.label("skill_uuid"),
                Skill.name,
                SkillRevision.description,
                SkillRevision.revision.label("current_revision"),
                SkillRevision.tags,
                SkillRevision.declared_tools,
                SkillRevision.compatibility,
                distance.label("distance"),
            )
            .select_from(Skill)
            .join(SkillRevision, SkillRevision.id == Skill.current_revision_id)
            .where(Skill.status == SkillStatus.ACTIVE)
            .order_by(distance.asc(), Skill.id.asc())
            .limit(top_k)
        )
        result = await self._session.execute(statement)
        return [
            ResolvedSkillCandidate(
                skill_uuid=row.skill_uuid,
                name=row.name,
                description=row.description,
                current_revision=row.current_revision,
                tags=row.tags,
                declared_tools=row.declared_tools,
                compatibility=row.compatibility,
                score=1.0 - row.distance,
            )
            for row in result.all()
        ]
