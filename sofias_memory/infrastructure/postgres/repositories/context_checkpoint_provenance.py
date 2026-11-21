"""ContextCheckpointProvenance PostgreSQL repository (SM-CTX-002)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import ContextCheckpointProvenance


class ContextCheckpointProvenanceRepository:
    """Persistence operations for the 1:1 checkpoint <-> provenance row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, provenance: ContextCheckpointProvenance) -> ContextCheckpointProvenance:
        self._session.add(provenance)
        await self._session.flush()
        return provenance

    async def get_by_checkpoint_ids(
        self, checkpoint_ids: Sequence[UUID]
    ) -> dict[UUID, ContextCheckpointProvenance]:
        if not checkpoint_ids:
            return {}
        statement = select(ContextCheckpointProvenance).where(
            ContextCheckpointProvenance.checkpoint_id.in_(list(checkpoint_ids))
        )
        result = await self._session.scalars(statement)
        return {row.checkpoint_id: row for row in result.all()}

    async def get_by_checkpoint_id_for_update(
        self, checkpoint_id: UUID
    ) -> ContextCheckpointProvenance | None:
        """Row-locked read for the atomic Forget scrub."""

        statement = (
            select(ContextCheckpointProvenance)
            .where(ContextCheckpointProvenance.checkpoint_id == checkpoint_id)
            .with_for_update()
        )
        return cast(ContextCheckpointProvenance | None, await self._session.scalar(statement))
