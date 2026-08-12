"""Source-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Source


class SourceRepository:
    """Persistence operations for source records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, source: Source) -> Source:
        self._session.add(source)
        await self._session.flush()
        return source

    async def get_by_id(self, source_id: UUID) -> Source | None:
        statement = select(Source).where(Source.id == source_id)
        result = await self._session.scalar(statement)
        return cast(Source | None, result)

    async def get_by_content_hash(
        self,
        *,
        dataset_id: UUID,
        content_sha256: str,
        version: int,
    ) -> Source | None:
        statement = select(Source).where(
            Source.dataset_id == dataset_id,
            Source.content_sha256 == content_sha256,
            Source.version == version,
        )
        result = await self._session.scalar(statement)
        return cast(Source | None, result)

    async def get_latest_by_content_hash(
        self,
        *,
        dataset_id: UUID,
        content_sha256: str,
    ) -> Source | None:
        statement = (
            select(Source)
            .where(
                Source.dataset_id == dataset_id,
                Source.content_sha256 == content_sha256,
            )
            .order_by(Source.version.desc(), Source.created_at.desc(), Source.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(Source | None, result)
