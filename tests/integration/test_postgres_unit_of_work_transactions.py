from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.config import load_settings
from sofias_memory.domain import DatasetStatus, GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Dataset, GraphOutbox
from sofias_memory.infrastructure.postgres.repositories import (
    DatasetRepository,
    GraphOutboxRepository,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory

POSTGRES_UOW_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_UOW_TESTS"


class ExpectedTestError(Exception):
    """Raised to verify Unit of Work exception rollback behavior."""


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_UOW_ENV) != "1":
        pytest.skip(f"set {POSTGRES_UOW_ENV}=1 to run PostgreSQL Unit of Work tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def build_dataset() -> Dataset:
    token = uuid4().hex
    return Dataset(
        id=uuid4(),
        name=f"SM-213 integration {token}",
        slug=f"sm-213-integration-{token}",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )


def build_graph_event(dataset: Dataset) -> GraphOutbox:
    return GraphOutbox(
        dataset_id=dataset.id,
        aggregate_type="dataset",
        aggregate_id=dataset.id,
        operation=GraphOutboxOperation.UPSERT,
        payload={"dataset_id": str(dataset.id)},
        status=GraphOutboxStatus.PENDING,
        attempt=0,
    )


async def cleanup_records(
    session_factory: AsyncSessionFactory,
    *,
    dataset_ids: set[UUID],
    graph_outbox_ids: set[int],
) -> None:
    async with session_factory() as session:
        if graph_outbox_ids:
            await session.execute(delete(GraphOutbox).where(GraphOutbox.id.in_(graph_outbox_ids)))
        if dataset_ids:
            await session.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
        await session.commit()


async def load_dataset_and_event(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: UUID,
    event_id: int,
) -> tuple[Dataset | None, GraphOutbox | None]:
    async with session_factory() as session:
        dataset = await DatasetRepository(session).get_by_id(dataset_id)
        event = await GraphOutboxRepository(session).get_by_id(event_id)
        return dataset, event


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unit_of_work_commit_persists_domain_change_and_graph_outbox(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dataset = build_dataset()
    event = build_graph_event(dataset)
    graph_outbox_ids: set[int] = set()

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.datasets.add(dataset)
            await uow.graph_outbox.add(event)
            await uow.commit()
            graph_outbox_ids.add(event.id)

        persisted_dataset, persisted_event = await load_dataset_and_event(
            postgres_session_factory,
            dataset_id=dataset.id,
            event_id=event.id,
        )

        assert persisted_dataset is not None
        assert persisted_event is not None
        assert persisted_event.dataset_id == dataset.id
        assert persisted_event.aggregate_id == dataset.id
    finally:
        await cleanup_records(
            postgres_session_factory,
            dataset_ids={dataset.id},
            graph_outbox_ids=graph_outbox_ids,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unit_of_work_explicit_rollback_discards_domain_and_outbox_changes(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dataset = build_dataset()
    event = build_graph_event(dataset)

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        await uow.datasets.add(dataset)
        await uow.graph_outbox.add(event)
        event_id = event.id
        await uow.rollback()

    persisted_dataset, persisted_event = await load_dataset_and_event(
        postgres_session_factory,
        dataset_id=dataset.id,
        event_id=event_id,
    )

    assert persisted_dataset is None
    assert persisted_event is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unit_of_work_exception_rollback_discards_domain_and_outbox_changes(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dataset = build_dataset()
    event = build_graph_event(dataset)

    with pytest.raises(ExpectedTestError):
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.datasets.add(dataset)
            await uow.graph_outbox.add(event)
            event_id = event.id
            raise ExpectedTestError

    persisted_dataset, persisted_event = await load_dataset_and_event(
        postgres_session_factory,
        dataset_id=dataset.id,
        event_id=event_id,
    )

    assert persisted_dataset is None
    assert persisted_event is None
