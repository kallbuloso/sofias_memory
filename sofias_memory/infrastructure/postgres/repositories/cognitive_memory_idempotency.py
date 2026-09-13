"""CognitiveMemoryIdempotency-specific PostgreSQL repository (ADR-0016 SS 14, SM-1001)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import CognitiveMemoryIdempotency


class CognitiveMemoryIdempotencyRepository:
    """Persistence operations for the Cognitive Memory idempotency ledger.

    ``add`` relies entirely on the table's ``UNIQUE(idempotency_key)``
    constraint as the authoritative race boundary -- callers must never
    treat a ``get_by_key`` miss as proof that the insert will not race
    (Feature Contract SS 17.2).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, ledger_entry: CognitiveMemoryIdempotency) -> CognitiveMemoryIdempotency:
        self._session.add(ledger_entry)
        await self._session.flush()
        return ledger_entry

    async def get_by_key(self, idempotency_key: str) -> CognitiveMemoryIdempotency | None:
        statement = select(CognitiveMemoryIdempotency).where(
            CognitiveMemoryIdempotency.idempotency_key == idempotency_key
        )
        result = await self._session.scalar(statement)
        return cast(CognitiveMemoryIdempotency | None, result)
