"""Dataset-specific PostgreSQL repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Dataset


class DatasetRepository:
    """Persistence operations for dataset roots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, dataset: Dataset) -> Dataset:
        self._session.add(dataset)
        await self._session.flush()
        return dataset

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        statement = select(Dataset).where(Dataset.id == dataset_id)
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)

    async def get_by_slug(self, slug: str) -> Dataset | None:
        statement = select(Dataset).where(Dataset.slug == slug)
        result = await self._session.scalar(statement)
        return cast(Dataset | None, result)
