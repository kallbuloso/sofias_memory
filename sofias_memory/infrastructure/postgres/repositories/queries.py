"""Query audit persistence."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Query


class QueryRepository:
    """Persist recall audit records without exposing SQLAlchemy from services."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, query: Query) -> Query:
        self._session.add(query)
        await self._session.flush()
        return query

    async def get_by_id(self, query_id: UUID) -> Query | None:
        result = await self._session.scalar(select(Query).where(Query.id == query_id))
        return cast(Query | None, result)
