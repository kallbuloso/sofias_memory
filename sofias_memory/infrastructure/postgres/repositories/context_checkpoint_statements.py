"""ContextCheckpointStatement PostgreSQL repository (SM-CTX-002)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import ContextCheckpointStatement


class ContextCheckpointStatementRepository:
    """Persistence operations for ContextStatements."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_all(
        self, statements: Sequence[ContextCheckpointStatement]
    ) -> list[ContextCheckpointStatement]:
        self._session.add_all(list(statements))
        await self._session.flush()
        return list(statements)

    async def list_by_checkpoint_ids(
        self, checkpoint_ids: Sequence[UUID]
    ) -> list[ContextCheckpointStatement]:
        """Deterministic order: checkpoint, type, ordinal."""

        if not checkpoint_ids:
            return []
        statement = (
            select(ContextCheckpointStatement)
            .where(ContextCheckpointStatement.checkpoint_id.in_(list(checkpoint_ids)))
            .order_by(
                ContextCheckpointStatement.checkpoint_id,
                ContextCheckpointStatement.statement_type,
                ContextCheckpointStatement.ordinal,
            )
        )
        result = await self._session.scalars(statement)
        return list(result.all())

    async def delete_by_checkpoint_id(self, checkpoint_id: UUID) -> None:
        """Hard-delete every semantic statement of one checkpoint (direct Forget)."""

        await self._session.execute(
            delete(ContextCheckpointStatement).where(
                ContextCheckpointStatement.checkpoint_id == checkpoint_id
            )
        )
