"""Summary-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Executable, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus, SourceStatus, SummaryTargetType
from sofias_memory.infrastructure.postgres.models import Dataset, Document, Source, Summary
from sofias_memory.schemas.recall import RecallFilters


@dataclass(frozen=True)
class RecalledDocumentSummary:
    """Active document summary snapshot for hybrid recall."""

    document_id: UUID
    text: str


@dataclass(frozen=True)
class RetrievedDocumentSummary:
    """Immutable document summary retrieval snapshot detached from the session."""

    summary_id: UUID
    dataset_id: UUID
    source_id: UUID
    source_name: str
    source_url: str | None
    document_id: UUID
    text: str
    score: float


class SummaryRepository:
    """Persistence operations for retrieval summaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, summary: Summary) -> Summary:
        self._session.add(summary)
        await self._session.flush()
        return summary

    async def get_by_id(self, summary_id: UUID) -> Summary | None:
        result = await self._session.scalar(select(Summary).where(Summary.id == summary_id))
        return cast(Summary | None, result)

    async def get_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> Summary | None:
        statement = (
            select(Summary)
            .where(
                Summary.dataset_id == dataset_id,
                Summary.generation == generation,
                Summary.target_type == target_type,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.is_active.is_(True),
            )
            .order_by(Summary.created_at.desc(), Summary.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(Summary | None, result)

    async def list_active_document_summaries_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        document_ids: list[UUID],
    ) -> list[RecalledDocumentSummary]:
        if not dataset_ids or not document_ids:
            return []

        statement = (
            select(Summary.target_id, Summary.text)
            .join(Dataset, Summary.dataset_id == Dataset.id)
            .where(
                Summary.dataset_id.in_(dataset_ids),
                Summary.target_id.in_(document_ids),
                Summary.target_type == SummaryTargetType.DOCUMENT,
                Summary.level == 0,
                Summary.is_active.is_(True),
                Dataset.status == DatasetStatus.ACTIVE,
                Summary.generation == Dataset.active_generation,
            )
            .order_by(Summary.target_id, Summary.created_at.desc(), Summary.id)
        )
        result = await self._session.execute(statement)
        summaries: dict[UUID, str] = {}
        for row in result:
            if row.target_id is not None and row.target_id not in summaries:
                summaries[row.target_id] = row.text
        return [
            RecalledDocumentSummary(document_id=document_id, text=text)
            for document_id, text in summaries.items()
        ]

    async def vector_search_document_summaries(
        self,
        *,
        dataset_ids: list[UUID],
        query_embedding: list[float],
        limit: int,
        filters: RecallFilters,
    ) -> list[RetrievedDocumentSummary]:
        distance = Summary.embedding.cosine_distance(query_embedding).label("distance")
        statement = self._base_document_summary_recall_statement(dataset_ids, filters).add_columns(
            distance
        )
        statement = statement.order_by(distance.asc(), Summary.id).limit(limit)
        return await self._retrieved_document_summaries(cast(Executable, statement))

    def _base_document_summary_recall_statement(
        self,
        dataset_ids: list[UUID],
        filters: RecallFilters,
    ) -> Select[tuple[UUID, UUID, UUID, str, str | None, UUID, str]]:
        statement = (
            select(
                Summary.id,
                Summary.dataset_id,
                Source.id.label("source_id"),
                Source.name.label("source_name"),
                Source.original_uri.label("source_url"),
                Document.id.label("document_id"),
                Summary.text,
            )
            .join(Document, Summary.target_id == Document.id)
            .join(Source, Document.source_id == Source.id)
            .join(Dataset, Summary.dataset_id == Dataset.id)
            .where(
                Summary.dataset_id.in_(dataset_ids),
                Summary.target_type == SummaryTargetType.DOCUMENT,
                Summary.level == 0,
                Summary.is_active.is_(True),
                Document.is_active.is_(True),
                Source.status == SourceStatus.ACTIVE,
                Dataset.status == DatasetStatus.ACTIVE,
                Summary.target_id == Document.id,
                Summary.dataset_id == Dataset.id,
                Document.dataset_id == Dataset.id,
                Source.dataset_id == Dataset.id,
                Summary.generation == Dataset.active_generation,
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

    async def _retrieved_document_summaries(
        self,
        statement: Executable,
    ) -> list[RetrievedDocumentSummary]:
        result = await self._session.execute(statement)
        return [
            RetrievedDocumentSummary(
                summary_id=row.id,
                dataset_id=row.dataset_id,
                source_id=row.source_id,
                source_name=row.source_name,
                source_url=row.source_url,
                document_id=row.document_id,
                text=row.text,
                score=1.0 - float(row.distance),
            )
            for row in result
        ]
