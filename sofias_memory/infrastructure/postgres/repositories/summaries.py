"""Summary-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus, SummaryTargetType
from sofias_memory.infrastructure.postgres.models import Dataset, Summary


@dataclass(frozen=True)
class RecalledDocumentSummary:
    """Active document summary snapshot for hybrid recall."""

    document_id: UUID
    text: str


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
