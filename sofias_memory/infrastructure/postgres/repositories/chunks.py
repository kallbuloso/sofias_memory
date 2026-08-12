"""Chunk-specific PostgreSQL repository."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Chunk


class ChunkRepository:
    """Persistence operations for chunk records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, chunk: Chunk) -> Chunk:
        chunk.lexical = cast(str, func.to_tsvector("simple", chunk.text))
        self._session.add(chunk)
        await self._session.flush()
        return chunk

    async def add_many(self, chunks: Sequence[Chunk]) -> list[Chunk]:
        for chunk in chunks:
            chunk.lexical = cast(str, func.to_tsvector("simple", chunk.text))
            self._session.add(chunk)
        await self._session.flush()
        return list(chunks)

    async def get_by_id(self, chunk_id: UUID) -> Chunk | None:
        result = await self._session.scalar(select(Chunk).where(Chunk.id == chunk_id))
        return cast(Chunk | None, result)

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]:
        statement = (
            select(Chunk)
            .where(
                Chunk.source_id == source_id,
                Chunk.generation == generation,
            )
            .order_by(Chunk.ordinal, Chunk.id)
        )
        if active_only:
            statement = statement.where(Chunk.is_active.is_(True))
        result = await self._session.scalars(statement)
        return list(result)

    async def exists_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> bool:
        statement = select(Chunk.id).where(
            Chunk.source_id == source_id,
            Chunk.generation == generation,
        )
        if active_only:
            statement = statement.where(Chunk.is_active.is_(True))
        statement = statement.limit(1)
        return await self._session.scalar(statement) is not None
