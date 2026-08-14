"""Chunk-specific PostgreSQL repository."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Executable, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus, SourceStatus
from sofias_memory.infrastructure.postgres.models import Chunk, Dataset, Document, Source
from sofias_memory.schemas.recall import RecallFilters


@dataclass(frozen=True)
class RetrievedChunk:
    """Immutable retrieval snapshot detached from the database session."""

    chunk_id: UUID
    dataset_id: UUID
    source_id: UUID
    source_name: str
    source_url: str | None
    document_id: UUID
    ordinal: int
    text: str
    start_char: int
    end_char: int


@dataclass(frozen=True)
class ChunkSummaryRebuildSnapshot:
    """Detached active chunk summary metadata for document summary rebuild."""

    id: UUID
    document_id: UUID
    generation: int
    ordinal: int
    metadata: dict[str, object]


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

    async def list_active_for_document_summary(
        self,
        *,
        document_id: UUID,
        generation: int,
    ) -> list[ChunkSummaryRebuildSnapshot]:
        statement = (
            select(Chunk)
            .where(
                Chunk.document_id == document_id,
                Chunk.generation == generation,
                Chunk.is_active.is_(True),
            )
            .order_by(Chunk.ordinal, Chunk.id)
        )
        result = await self._session.scalars(statement)
        return [_chunk_summary_rebuild_snapshot(chunk) for chunk in result]

    async def vector_search(
        self,
        *,
        dataset_ids: list[UUID],
        query_embedding: list[float],
        limit: int,
        filters: RecallFilters,
    ) -> list[RetrievedChunk]:
        distance = Chunk.embedding.cosine_distance(query_embedding).label("distance")
        statement = self._base_recall_statement(dataset_ids, filters).add_columns(distance)
        statement = statement.order_by(distance.asc(), Chunk.id).limit(limit)
        return await self._retrieved_chunks(cast(Executable, statement))

    async def lexical_search(
        self,
        *,
        dataset_ids: list[UUID],
        query_text: str,
        limit: int,
        filters: RecallFilters,
    ) -> list[RetrievedChunk]:
        tsquery = func.websearch_to_tsquery("simple", query_text)
        rank = func.ts_rank_cd(Chunk.lexical, tsquery).label("rank")
        statement = self._base_recall_statement(dataset_ids, filters).add_columns(rank)
        statement = statement.where(Chunk.lexical.op("@@")(tsquery))
        statement = statement.order_by(rank.desc(), Chunk.id).limit(limit)
        return await self._retrieved_chunks(cast(Executable, statement))

    def _base_recall_statement(
        self,
        dataset_ids: list[UUID],
        filters: RecallFilters,
    ) -> Select[tuple[UUID, UUID, UUID, str, str | None, UUID, int, str, int, int]]:
        statement = (
            select(
                Chunk.id,
                Chunk.dataset_id,
                Chunk.source_id,
                Source.name,
                Source.original_uri,
                Chunk.document_id,
                Chunk.ordinal,
                Chunk.text,
                Chunk.start_char,
                Chunk.end_char,
            )
            .join(Document, Chunk.document_id == Document.id)
            .join(Source, Chunk.source_id == Source.id)
            .join(Dataset, Chunk.dataset_id == Dataset.id)
            .where(
                Chunk.dataset_id.in_(dataset_ids),
                Dataset.status == DatasetStatus.ACTIVE,
                Chunk.is_active.is_(True),
                Document.is_active.is_(True),
                Source.status == SourceStatus.ACTIVE,
                Source.dataset_id == Dataset.id,
                Document.dataset_id == Dataset.id,
                Chunk.generation == Dataset.active_generation,
                Document.generation == Dataset.active_generation,
            )
        )
        if filters.source_ids:
            statement = statement.where(Source.id.in_(filters.source_ids))
        if filters.created_after is not None:
            statement = statement.where(Source.created_at >= filters.created_after)
        if filters.created_before is not None:
            statement = statement.where(Source.created_at <= filters.created_before)
        if filters.metadata:
            statement = statement.where(Source.metadata_.contains(filters.metadata))
        return statement

    async def _retrieved_chunks(self, statement: Executable) -> list[RetrievedChunk]:
        result = await self._session.execute(statement)
        return [
            RetrievedChunk(
                chunk_id=row.id,
                dataset_id=row.dataset_id,
                source_id=row.source_id,
                source_name=row.name,
                source_url=row.original_uri,
                document_id=row.document_id,
                ordinal=row.ordinal,
                text=row.text,
                start_char=row.start_char,
                end_char=row.end_char,
            )
            for row in result
        ]


def _chunk_summary_rebuild_snapshot(chunk: Chunk) -> ChunkSummaryRebuildSnapshot:
    return ChunkSummaryRebuildSnapshot(
        id=chunk.id,
        document_id=chunk.document_id,
        generation=chunk.generation,
        ordinal=chunk.ordinal,
        metadata=dict(chunk.metadata_),
    )
