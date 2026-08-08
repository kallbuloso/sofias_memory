"""Document-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Document


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
