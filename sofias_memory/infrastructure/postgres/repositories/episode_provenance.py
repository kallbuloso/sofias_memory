"""EpisodeProvenance-specific PostgreSQL repository (SM-CTX-001)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import EpisodeProvenance


class EpisodeProvenanceRepository:
    """Persistence operations for the 1:1 Episode <-> provenance row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, provenance: EpisodeProvenance) -> EpisodeProvenance:
        self._session.add(provenance)
        await self._session.flush()
        return provenance

    async def get_by_episode_id(self, episode_id: UUID) -> EpisodeProvenance | None:
        statement = select(EpisodeProvenance).where(EpisodeProvenance.episode_id == episode_id)
        return cast(EpisodeProvenance | None, await self._session.scalar(statement))

    async def get_by_episode_id_for_update(self, episode_id: UUID) -> EpisodeProvenance | None:
        """Row-locked read for the atomic Forget scrub."""

        statement = (
            select(EpisodeProvenance)
            .where(EpisodeProvenance.episode_id == episode_id)
            .with_for_update()
        )
        return cast(EpisodeProvenance | None, await self._session.scalar(statement))
