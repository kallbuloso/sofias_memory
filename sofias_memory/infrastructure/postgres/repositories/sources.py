"""Source-specific PostgreSQL repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres.models import Source


@dataclass(frozen=True)
class SourceListItem:
    """Detached source metadata for dataset management responses."""

    id: UUID
    dataset_id: UUID
    kind: SourceKind
    name: str
    mime_type: str
    original_uri: str | None
    content_sha256: str
    normalized_sha256: str | None
    byte_size: int
    metadata: dict[str, object]
    status: SourceStatus
    version: int
    created_at: datetime
    updated_at: datetime


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

    async def get_by_id_for_update(self, source_id: UUID) -> Source | None:
        statement = select(Source).where(Source.id == source_id).with_for_update()
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

    async def list_pending_for_dataset(self, dataset_id: UUID) -> list[Source]:
        statement = (
            select(Source)
            .where(
                Source.dataset_id == dataset_id,
                Source.status == SourceStatus.PENDING,
            )
            .order_by(Source.created_at, Source.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_for_cognify(self, dataset_id: UUID) -> list[Source]:
        statement = (
            select(Source)
            .where(
                Source.dataset_id == dataset_id,
                Source.status.in_((SourceStatus.PENDING, SourceStatus.ACTIVE)),
            )
            .order_by(Source.created_at, Source.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_for_dataset_not_deleted(self, dataset_id: UUID) -> list[Source]:
        """Plain, unlocked read of every non-terminal source of a dataset.

        Used for read-only concurrency checks and storage cleanup where taking
        a row lock would be unnecessary; the authoritative mutation and the
        finalize step use :meth:`list_for_dataset_for_update` instead.
        """

        statement = (
            select(Source)
            .where(
                Source.dataset_id == dataset_id,
                Source.status != SourceStatus.DELETED,
            )
            .order_by(Source.id)
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_for_dataset_for_update(self, dataset_id: UUID) -> list[Source]:
        """Lock every non-terminal source of a dataset, in deterministic id order.

        Sources already ``deleted`` are excluded: they carry no authoritative
        content or storage left to forget, and locking them would only widen
        the deadlock surface of dataset/everything forget without any benefit.
        """

        statement = (
            select(Source)
            .where(
                Source.dataset_id == dataset_id,
                Source.status != SourceStatus.DELETED,
            )
            .order_by(Source.id)
            .with_for_update()
        )
        result = await self._session.scalars(statement)
        return list(result)

    async def list_for_dataset_paginated(
        self,
        *,
        dataset_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[SourceListItem], int]:
        statement = (
            select(Source)
            .where(Source.dataset_id == dataset_id)
            .order_by(Source.created_at, Source.id)
            .limit(limit)
            .offset(offset)
        )
        total_statement = (
            select(func.count()).select_from(Source).where(Source.dataset_id == dataset_id)
        )
        result = await self._session.scalars(statement)
        total = await self._session.scalar(total_statement)
        return [_source_list_item(source) for source in result], int(total or 0)


def _source_list_item(source: Source) -> SourceListItem:
    return SourceListItem(
        id=source.id,
        dataset_id=source.dataset_id,
        kind=source.kind,
        name=source.name,
        mime_type=source.mime_type,
        original_uri=source.original_uri,
        content_sha256=source.content_sha256,
        normalized_sha256=source.normalized_sha256,
        byte_size=source.byte_size,
        metadata=dict(source.metadata_),
        status=source.status,
        version=source.version,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )
