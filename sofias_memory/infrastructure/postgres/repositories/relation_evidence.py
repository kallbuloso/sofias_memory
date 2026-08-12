"""Relation-evidence-specific PostgreSQL repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import RelationEvidence


class RelationEvidenceRepository:
    """Persistence operations for exact chunk evidence supporting relations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: RelationEvidence) -> RelationEvidence:
        self._session.add(evidence)
        await self._session.flush()
        return evidence

    async def exists_for_relation_chunk(self, *, relation_id: UUID, chunk_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(RelationEvidence.relation_id)
                .where(
                    RelationEvidence.relation_id == relation_id,
                    RelationEvidence.chunk_id == chunk_id,
                )
                .limit(1)
            )
            is not None
        )
