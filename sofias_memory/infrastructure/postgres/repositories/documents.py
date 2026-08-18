"""Document-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus, SourceStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Document, Source


@dataclass(frozen=True)
class DocumentSummaryRebuildCandidate:
    """Detached active document snapshot for summary rebuild."""

    id: UUID
    dataset_id: UUID
    source_id: UUID
    generation: int
    metadata: dict[str, object]


class DocumentRepository:
    """Persistence operations for normalized documents."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: Document) -> Document:
        self._session.add(document)
        await self._session.flush()
        return document

    async def get_by_id(self, document_id: UUID) -> Document | None:
        statement = select(Document).where(Document.id == document_id)
        result = await self._session.scalar(statement)
        return cast(Document | None, result)

    async def list_for_source(self, source_id: UUID) -> list[Document]:
        statement = (
            select(Document)
            .where(Document.source_id == source_id)
            .order_by(Document.generation, Document.created_at, Document.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Document]:
        statement = (
            select(Document)
            .where(
                Document.source_id == source_id,
                Document.generation == generation,
            )
            .order_by(Document.created_at, Document.id)
        )
        if active_only:
            statement = statement.where(Document.is_active.is_(True))
        result = await self._session.scalars(statement)
        return list(result)

    async def get_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
    ) -> Document | None:
        statement = (
            select(Document)
            .where(
                Document.source_id == source_id,
                Document.generation == generation,
                Document.is_active.is_(True),
            )
            .order_by(Document.created_at.desc(), Document.id)
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(Document | None, result)

    async def list_active_current_for_dataset(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> list[Document]:
        """All authoritative documents for a dataset forget/everything mutation.

        Scoped only by ``dataset_id``/``generation``: dataset-scoped forget
        touches every source in the dataset, so no per-source or per-entity
        orphan detection is needed here, unlike source-scoped forget.
        """

        statement = (
            select(Document)
            .where(
                Document.dataset_id == dataset_id,
                Document.generation == generation,
                Document.is_active.is_(True),
            )
            .order_by(Document.source_id, Document.created_at, Document.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> list[DocumentSummaryRebuildCandidate]:
        statement = (
            select(Document)
            .join(Source, Document.source_id == Source.id)
            .join(Dataset, Document.dataset_id == Dataset.id)
            .where(
                Document.dataset_id == dataset_id,
                Document.generation == generation,
                Document.is_active.is_(True),
                Source.dataset_id == dataset_id,
                Source.status == SourceStatus.ACTIVE,
                Dataset.id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Dataset.active_generation == generation,
            )
            .order_by(Document.created_at, Document.id)
        )
        result = await self._session.scalars(statement)
        return [_document_summary_rebuild_candidate(document) for document in result]

    async def get_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        document_id: UUID,
    ) -> Document | None:
        statement = (
            select(Document)
            .join(Source, Document.source_id == Source.id)
            .join(Dataset, Document.dataset_id == Dataset.id)
            .where(
                Document.id == document_id,
                Document.dataset_id == dataset_id,
                Document.generation == generation,
                Document.is_active.is_(True),
                Source.dataset_id == dataset_id,
                Source.status == SourceStatus.ACTIVE,
                Dataset.id == dataset_id,
                Dataset.status == DatasetStatus.ACTIVE,
                Dataset.active_generation == generation,
            )
            .limit(1)
        )
        result = await self._session.scalar(statement)
        return cast(Document | None, result)


def _document_summary_rebuild_candidate(document: Document) -> DocumentSummaryRebuildCandidate:
    return DocumentSummaryRebuildCandidate(
        id=document.id,
        dataset_id=document.dataset_id,
        source_id=document.source_id,
        generation=document.generation,
        metadata=dict(document.metadata_),
    )
