from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.config import load_settings
from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    Neo4jResource,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.repositories import GraphOutboxRepository
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor

GRAPH_OUTBOX_PROCESSOR_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_OUTBOX_PROCESSOR_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(GRAPH_OUTBOX_PROCESSOR_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_OUTBOX_PROCESSOR_TESTS_ENV}=1 to run graph outbox processor tests")

    engine = create_async_engine_from_settings(load_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


@pytest_asyncio.fixture()
async def neo4j_resource() -> AsyncIterator[Neo4jResource]:
    if os.environ.get(GRAPH_OUTBOX_PROCESSOR_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_OUTBOX_PROCESSOR_TESTS_ENV}=1 to run graph outbox processor tests")

    resource = create_neo4j_resource_from_settings(load_settings())
    try:
        yield resource
    finally:
        await resource.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_outbox_processor_projects_one_entity_event(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id: int | None = None

    try:
        event = GraphOutbox(
            dataset_id=dataset_id,
            aggregate_type="entity",
            aggregate_id=entity_id,
            operation=GraphOutboxOperation.UPSERT,
            payload={
                "schema_version": 1,
                "aggregate_type": "entity",
                "operation": "upsert",
                "dataset_id": str(dataset_id),
                "aggregate_id": str(entity_id),
                "identity": {"id": str(entity_id)},
                "properties": {
                    "id": str(entity_id),
                    "dataset_id": str(dataset_id),
                    "name": "Processor Integration Entity",
                    "entity_type": "test",
                    "description": "Created by graph outbox processor integration test.",
                    "importance_weight": 0.5,
                    "generation": 1,
                },
            },
            status=GraphOutboxStatus.PENDING,
            attempt=0,
        )
        async with postgres_session_factory() as session:
            await GraphOutboxRepository(session).add(event)
            await session.commit()
            event_id = event.id

        processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
        )

        await processor.process(event_id)

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.DONE
        assert persisted.attempt == 1
        assert persisted.processed_at is not None
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
        assert await entity_name(neo4j_resource, str(entity_id)) == "Processor Integration Entity"

        await processor.process(event_id)

        replayed = await load_event(postgres_session_factory, event_id)
        assert replayed is not None
        assert replayed.status == GraphOutboxStatus.DONE
        assert replayed.attempt == 1
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
    finally:
        if event_id is not None:
            await cleanup_event(postgres_session_factory, event_id)
        await cleanup_entity(neo4j_resource, str(entity_id))


async def load_event(
    session_factory: AsyncSessionFactory,
    event_id: int,
) -> GraphOutbox | None:
    async with session_factory() as session:
        return await GraphOutboxRepository(session).get_by_id(event_id)


async def cleanup_event(session_factory: AsyncSessionFactory, event_id: int) -> None:
    async with session_factory() as session:
        await session.execute(delete(GraphOutbox).where(GraphOutbox.id == event_id))
        await session.commit()


async def cleanup_entity(resource: Neo4jResource, entity_id: str) -> None:
    await resource.driver.execute_query(
        "MATCH (n:Entity {id: $entity_id}) DETACH DELETE n",
        {"entity_id": entity_id},
        database_=resource.database,
    )


async def entity_count(resource: Neo4jResource, entity_id: str) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n:Entity {id: $entity_id}) RETURN count(n) AS count",
        {"entity_id": entity_id},
        database_=resource.database,
    )
    return int(result_records(result)[0]["count"])


async def entity_name(resource: Neo4jResource, entity_id: str) -> str:
    result = await resource.driver.execute_query(
        "MATCH (n:Entity {id: $entity_id}) RETURN n.name AS name",
        {"entity_id": entity_id},
        database_=resource.database,
    )
    return str(result_records(result)[0]["name"])


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]
