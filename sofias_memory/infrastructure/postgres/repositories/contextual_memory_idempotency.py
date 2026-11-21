"""ContextualMemoryIdempotency PostgreSQL repository (SM-CTX-001).

Resource-generic: it never interprets ``target_resource_id``/
``result_resource_id`` -- the service of each operation does. ``add`` relies
entirely on ``UNIQUE(idempotency_key)`` as the authoritative race boundary;
a ``get_by_protected_key`` miss never proves an insert will not race.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import ContextualMemoryIdempotency


class ContextualMemoryIdempotencyRepository:
    """Persistence operations for the shared contextual idempotency ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, ledger_entry: ContextualMemoryIdempotency) -> ContextualMemoryIdempotency:
        self._session.add(ledger_entry)
        await self._session.flush()
        return ledger_entry

    async def get_by_protected_key(self, protected_key: str) -> ContextualMemoryIdempotency | None:
        statement = select(ContextualMemoryIdempotency).where(
            ContextualMemoryIdempotency.idempotency_key == protected_key
        )
        return cast(ContextualMemoryIdempotency | None, await self._session.scalar(statement))
