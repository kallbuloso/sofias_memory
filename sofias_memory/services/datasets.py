"""Synchronous dataset management service."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.repositories.datasets import DatasetStatsSnapshot
from sofias_memory.infrastructure.postgres.repositories.sources import SourceListItem
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, JSONValue, utc_now
from sofias_memory.schemas.datasets import (
    DatasetChunksStats,
    DatasetCreateRequest,
    DatasetDocumentsStats,
    DatasetEntitiesStats,
    DatasetListResult,
    DatasetRelationsStats,
    DatasetRenameRequest,
    DatasetResult,
    DatasetSourceResult,
    DatasetSourcesResult,
    DatasetSourcesStats,
    DatasetStatsResult,
    DatasetSummariesStats,
)

DEFAULT_DATASET_SLUG = "main"
SLUG_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")


class DatasetRepositoryForDatasets(Protocol):
    async def add(self, dataset: Dataset) -> Dataset: ...
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...
    async def get_by_slug(self, slug: str) -> Dataset | None: ...
    async def get_by_name(self, name: str) -> Dataset | None: ...
    async def list_paginated(self, *, limit: int, offset: int) -> tuple[list[Dataset], int]: ...
    async def stats_for_dataset(self, dataset: Dataset) -> DatasetStatsSnapshot: ...


class SourceRepositoryForDatasets(Protocol):
    async def list_for_dataset_paginated(
        self,
        *,
        dataset_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[SourceListItem], int]: ...


class DatasetUnitOfWork(Protocol):
    datasets: DatasetRepositoryForDatasets
    sources: SourceRepositoryForDatasets

    async def __aenter__(self) -> DatasetUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], DatasetUnitOfWork]


class DatasetService:
    """Manage dataset namespaces without graph side effects."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def create_dataset(self, request: DatasetCreateRequest) -> DatasetResult:
        slug = normalize_dataset_slug(request.slug or request.name)
        if slug == DEFAULT_DATASET_SLUG:
            raise reserved_main_error()

        now = utc_now()
        dataset = Dataset(
            id=uuid4(),
            name=request.name,
            slug=slug,
            description=request.description,
            status=DatasetStatus.ACTIVE,
            active_generation=0,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._unit_of_work_factory() as uow:
                await ensure_dataset_name_and_slug_available(
                    uow,
                    name=request.name,
                    slug=slug,
                    current_dataset_id=None,
                )
                dataset = await uow.datasets.add(dataset)
                result = dataset_result(dataset)
                await uow.commit()
                return result
        except IntegrityError as exc:
            raise dataset_conflict_error() from exc

    async def list_datasets(self, *, limit: int, offset: int) -> DatasetListResult:
        async with self._unit_of_work_factory() as uow:
            datasets, total = await uow.datasets.list_paginated(limit=limit, offset=offset)
            return DatasetListResult(
                items=[dataset_result(dataset) for dataset in datasets],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_dataset(self, dataset_id: UUID) -> DatasetResult:
        async with self._unit_of_work_factory() as uow:
            dataset = await require_dataset(uow, dataset_id)
            return dataset_result(dataset)

    async def rename_dataset(
        self,
        dataset_id: UUID,
        request: DatasetRenameRequest,
    ) -> DatasetResult:
        try:
            async with self._unit_of_work_factory() as uow:
                dataset = await require_dataset(uow, dataset_id)
                if dataset.status != DatasetStatus.ACTIVE:
                    raise SofiasMemoryError(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.BAD_REQUEST,
                        message="Dataset is not active.",
                        details={"dataset_id": str(dataset_id), "status": dataset.status.value},
                    )
                await ensure_dataset_name_and_slug_available(
                    uow,
                    name=request.name,
                    slug=dataset.slug,
                    current_dataset_id=dataset.id,
                )
                dataset.name = request.name
                dataset.updated_at = utc_now()
                result = dataset_result(dataset)
                await uow.commit()
                return result
        except IntegrityError as exc:
            raise dataset_conflict_error() from exc

    async def list_sources(
        self,
        *,
        dataset_id: UUID,
        limit: int,
        offset: int,
    ) -> DatasetSourcesResult:
        async with self._unit_of_work_factory() as uow:
            dataset = await require_dataset(uow, dataset_id)
            sources, total = await uow.sources.list_for_dataset_paginated(
                dataset_id=dataset.id,
                limit=limit,
                offset=offset,
            )
            return DatasetSourcesResult(
                items=[source_result(source) for source in sources],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_stats(self, dataset_id: UUID) -> DatasetStatsResult:
        async with self._unit_of_work_factory() as uow:
            dataset = await require_dataset(uow, dataset_id)
            stats = await uow.datasets.stats_for_dataset(dataset)
            return stats_result(dataset, stats)


def normalize_dataset_slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value.strip())
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = SLUG_SEPARATOR_PATTERN.sub("-", ascii_value).strip("-")
    if not slug:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Dataset slug is invalid.",
        )
    return slug


async def ensure_dataset_name_and_slug_available(
    uow: DatasetUnitOfWork,
    *,
    name: str,
    slug: str,
    current_dataset_id: UUID | None,
) -> None:
    existing_name = await uow.datasets.get_by_name(name)
    if existing_name is not None and existing_name.id != current_dataset_id:
        raise dataset_conflict_error()

    existing_slug = await uow.datasets.get_by_slug(slug)
    if existing_slug is not None and existing_slug.id != current_dataset_id:
        raise dataset_conflict_error()


async def require_dataset(uow: DatasetUnitOfWork, dataset_id: UUID) -> Dataset:
    dataset = await uow.datasets.get_by_id(dataset_id)
    if dataset is None:
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.NOT_FOUND,
            message="Dataset does not exist.",
            details={"dataset_id": str(dataset_id)},
        )
    return dataset


def dataset_result(dataset: Dataset) -> DatasetResult:
    return DatasetResult(
        dataset_id=dataset.id,
        name=dataset.name,
        slug=dataset.slug,
        description=dataset.description,
        status=dataset.status,
        active_generation=dataset.active_generation,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def source_result(source: SourceListItem) -> DatasetSourceResult:
    return DatasetSourceResult(
        source_id=source.id,
        dataset_id=source.dataset_id,
        kind=source.kind,
        name=source.name,
        mime_type=source.mime_type,
        original_uri=source.original_uri,
        content_sha256=source.content_sha256,
        normalized_sha256=source.normalized_sha256,
        byte_size=source.byte_size,
        metadata=cast(dict[str, JSONValue], source.metadata),
        status=source.status,
        version=source.version,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def stats_result(dataset: Dataset, stats: DatasetStatsSnapshot) -> DatasetStatsResult:
    return DatasetStatsResult(
        dataset_id=dataset.id,
        active_generation=dataset.active_generation,
        sources=DatasetSourcesStats(total=stats.sources_total, active=stats.sources_active),
        documents=DatasetDocumentsStats(total=stats.documents_total, active=stats.documents_active),
        chunks=DatasetChunksStats(total=stats.chunks_total, active=stats.chunks_active),
        entities=DatasetEntitiesStats(
            active_current_generation=stats.entities_active_current_generation
        ),
        relations=DatasetRelationsStats(
            active_current_generation=stats.relations_active_current_generation
        ),
        summaries=DatasetSummariesStats(total=stats.summaries_total),
    )


def reserved_main_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.BAD_REQUEST,
        message="Dataset slug 'main' is reserved.",
        details={"slug": DEFAULT_DATASET_SLUG},
    )


def dataset_conflict_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.CONFLICT,
        message="Dataset name or slug already exists.",
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> DatasetUnitOfWork:
        return cast(DatasetUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
