from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
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
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.ports import (
    ProjectionCommand,
    entity_upsert_command,
    projection_command_from_payload,
    relation_upsert_command,
)
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import (
    DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
    GraphOutboxProcessor,
)

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


# =============================================================================
# SM-506 autonomous consumer / stale replay / coexistence scenarios (M-U)
# =============================================================================


class DelayedProjection:
    """Wraps a real :class:`GraphProjectionPort` to hold ``apply`` open for a
    controlled duration -- lets tests deterministically win/observe a real
    autonomous-vs-explicit claim race without a process-local mutex standing
    in for the PostgreSQL lease itself."""

    def __init__(self, inner: Neo4jProjection, *, delay_seconds: float) -> None:
        self._inner = inner
        self._delay_seconds = delay_seconds
        self.apply_calls: list[ProjectionCommand] = []

    async def apply(self, command: ProjectionCommand) -> None:
        self.apply_calls.append(command)
        await asyncio.sleep(self._delay_seconds)
        await self._inner.apply(command)


async def insert_entity_event(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: object,
    entity_id: object,
    name: str = "SM-506 Neo4j Scenario Entity",
) -> int:
    command = entity_upsert_command(
        entity_id=entity_id,
        dataset_id=dataset_id,
        name=name,
        entity_type="test",
        description="Created by SM-506 Neo4j scenario test.",
        importance_weight=0.5,
        generation=1,
    )
    async with PostgresUnitOfWork(session_factory) as uow:
        event = await uow.graph_outbox.add_projection_command(command)
        await uow.commit()
        return event.id


async def insert_relation_event(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: object,
    relation_id: object,
    source_entity_id: object,
    target_entity_id: object,
) -> int:
    command = relation_upsert_command(
        relation_id=relation_id,
        dataset_id=dataset_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate="relates_to",
        description="SM-506 Neo4j scenario relation.",
        confidence=0.9,
        importance_weight=0.5,
        generation=1,
    )
    async with PostgresUnitOfWork(session_factory) as uow:
        event = await uow.graph_outbox.add_projection_command(command)
        await uow.commit()
        return event.id


async def backdate_processing_started_at(
    session_factory: AsyncSessionFactory, outbox_id: int, *, seconds_ago: float
) -> None:
    async with PostgresUnitOfWork(session_factory) as uow:
        event = await uow.graph_outbox.get_by_id(outbox_id)
        assert event is not None
        event.processing_started_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        await uow.commit()


async def relation_count(resource: Neo4jResource, relation_id: str) -> int:
    result = await resource.driver.execute_query(
        "MATCH ()-[r:RELATES_TO {relation_id: $relation_id}]->() RETURN count(r) AS count",
        {"relation_id": relation_id},
        database_=resource.database,
    )
    return int(result_records(result)[0]["count"])


async def cleanup_events(session_factory: AsyncSessionFactory, event_ids: list[int]) -> None:
    if not event_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(GraphOutbox).where(GraphOutbox.id.in_(event_ids)))
        await session.commit()


# -- M: pending abandoned processed autonomously, no HTTP request ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_autonomous_consumer_processes_abandoned_pending_row(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-autonomous-m",
        )

        # No caller ever names event_id -- this is exactly the autonomous
        # safety-net entry point, never the explicit ``process(outbox_id)``.
        result = await processor.claim_and_process_one()

        assert result is not None
        assert result.outbox_id == event_id
        assert result.status == GraphOutboxStatus.DONE
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


# -- N: crash after Neo4j apply, before mark_done -> stale replay -> DONE -------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_crash_after_apply_before_mark_done_converges_via_stale_replay(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        # Simulate worker A: claims (attempt=1), applies to Neo4j for real,
        # then "crashes" -- mark_done is never called, row stays PROCESSING.
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                event_id,
                worker_id="wk-crashed",
                stale_after_seconds=DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
                max_attempts=5,
            )
            await uow.commit()
        assert claimed is not None
        assert claimed.attempt == 1

        command = projection_command_from_payload(claimed.payload)
        await Neo4jProjection(neo4j_resource).apply(command)
        assert await entity_count(neo4j_resource, str(entity_id)) == 1

        # Age the lease so it becomes reclaimable -- worker A never returns.
        await backdate_processing_started_at(
            postgres_session_factory,
            event_id,
            seconds_ago=DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS + 5,
        )

        # Worker B: a brand-new autonomous claim, replays the same command.
        processor_b = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-recovery",
        )
        result = await processor_b.claim_and_process_one()

        assert result is not None
        assert result.attempt == 2
        assert result.status == GraphOutboxStatus.DONE
        # ADR-0008 MERGE-equivalent idempotency: replay converges, no
        # duplicate Entity node.
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
        assert await entity_name(neo4j_resource, str(entity_id)) == "SM-506 Neo4j Scenario Entity"
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


# -- O: duplicate relationship replay does not duplicate the edge ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_duplicate_relationship_replay_does_not_duplicate_edge(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    source_id = uuid4()
    target_id = uuid4()
    relation_id = uuid4()
    event_ids: list[int] = []
    try:
        source_event_id = await insert_entity_event(
            postgres_session_factory, dataset_id=dataset_id, entity_id=source_id, name="Source"
        )
        target_event_id = await insert_entity_event(
            postgres_session_factory, dataset_id=dataset_id, entity_id=target_id, name="Target"
        )
        event_ids += [source_event_id, target_event_id]

        processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-o",
        )
        assert (await processor.claim_and_process_one()) is not None
        assert (await processor.claim_and_process_one()) is not None

        relation_event_id_1 = await insert_relation_event(
            postgres_session_factory,
            dataset_id=dataset_id,
            relation_id=relation_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
        )
        relation_event_id_2 = await insert_relation_event(
            postgres_session_factory,
            dataset_id=dataset_id,
            relation_id=relation_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
        )
        event_ids += [relation_event_id_1, relation_event_id_2]

        first = await processor.process(relation_event_id_1)
        second = await processor.process(relation_event_id_2)

        assert first.status == GraphOutboxStatus.DONE
        assert second.status == GraphOutboxStatus.DONE
        assert await relation_count(neo4j_resource, str(relation_id)) == 1
    finally:
        await cleanup_events(postgres_session_factory, event_ids)
        await cleanup_entity(neo4j_resource, str(source_id))
        await cleanup_entity(neo4j_resource, str(target_id))


# -- P: external Neo4j data is preserved -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p_external_neo4j_data_is_preserved(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    external_entity_id = uuid4()
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id: int | None = None
    try:
        # Pre-existing, unrelated Neo4j data this test never touches on
        # purpose -- SM-506 machinery must never scan/wipe/rebuild globally.
        await neo4j_resource.driver.execute_query(
            "CREATE (n:Entity {id: $id, dataset_id: $dataset_id, name: $name, "
            "entity_type: 'external', description: '', importance_weight: 1.0, "
            "generation: 1})",
            {"id": str(external_entity_id), "dataset_id": str(uuid4()), "name": "External"},
            database_=neo4j_resource.database,
        )

        event_id = await insert_entity_event(
            postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
        )
        processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-p",
        )
        assert (await processor.claim_and_process_one()) is not None

        assert await entity_count(neo4j_resource, str(external_entity_id)) == 1
        assert await entity_name(neo4j_resource, str(external_entity_id)) == "External"
    finally:
        if event_id is not None:
            await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))
        await cleanup_entity(neo4j_resource, str(external_entity_id))


# -- Q: relationship with a missing endpoint fails safely, no placeholder -------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q_missing_relationship_endpoint_fails_safely_without_placeholder(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    source_id = uuid4()  # never projected -- no Entity node exists for it
    target_id = uuid4()  # never projected either
    relation_id = uuid4()

    event_id = await insert_relation_event(
        postgres_session_factory,
        dataset_id=dataset_id,
        relation_id=relation_id,
        source_entity_id=source_id,
        target_entity_id=target_id,
    )
    try:
        processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-q",
        )

        with pytest.raises(Exception):  # noqa: B017, PT011 - port-level failure, not a stable type
            await processor.process(event_id)

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.FAILED
        assert persisted.attempt == 1  # retryable -- below the ceiling
        assert await relation_count(neo4j_resource, str(relation_id)) == 0
        assert await entity_count(neo4j_resource, str(source_id)) == 0
        assert await entity_count(neo4j_resource, str(target_id)) == 0
    finally:
        await cleanup_events(postgres_session_factory, [event_id])


# -- R: autonomous x explicit coexistence, both race orders ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r1_autonomous_wins_race_explicit_observes_and_returns_done(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        slow_projection = DelayedProjection(Neo4jProjection(neo4j_resource), delay_seconds=0.5)
        autonomous = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=slow_projection,
            worker_id="wk-autonomous-r1",
        )
        explicit = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-explicit-r1",
            explicit_observe_interval_seconds=0.02,
        )

        autonomous_task = asyncio.ensure_future(autonomous.claim_and_process_one())
        await asyncio.sleep(0.1)  # let autonomous win the claim well before explicit starts
        explicit_task = asyncio.ensure_future(explicit.process(event_id))

        autonomous_result, explicit_result = await asyncio.gather(autonomous_task, explicit_task)

        assert autonomous_result is not None
        assert autonomous_result.attempt == 1
        assert explicit_result.attempt == 1
        assert explicit_result.already_done is True  # explicit never applied in parallel
        assert len(slow_projection.apply_calls) == 1  # exactly one owner ever applied
        assert await entity_count(neo4j_resource, str(entity_id)) == 1

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.worker_id == "wk-autonomous-r1"  # ownership never stolen
        assert persisted.attempt == 1
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r2_explicit_wins_race_autonomous_does_not_steal(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        slow_projection = DelayedProjection(Neo4jProjection(neo4j_resource), delay_seconds=0.5)
        explicit = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=slow_projection,
            worker_id="wk-explicit-r2",
        )
        autonomous = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-autonomous-r2",
        )

        explicit_task = asyncio.ensure_future(explicit.process(event_id))
        await asyncio.sleep(0.1)  # let explicit win the claim before autonomous polls

        autonomous_attempts_while_live: list[object] = []
        for _ in range(3):
            autonomous_attempts_while_live.append(await autonomous.claim_and_process_one())
            await asyncio.sleep(0.05)

        explicit_result = await explicit_task
        # A poll after explicit's lease resolved: the row is DONE, so there
        # is nothing left for the autonomous consumer to claim.
        autonomous_after = await autonomous.claim_and_process_one()

        assert all(result is None for result in autonomous_attempts_while_live)
        assert explicit_result.status == GraphOutboxStatus.DONE
        assert explicit_result.attempt == 1
        assert autonomous_after is None
        assert len(slow_projection.apply_calls) == 1
        assert await entity_count(neo4j_resource, str(entity_id)) == 1

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.worker_id == "wk-explicit-r2"
        assert persisted.attempt == 1
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


# =============================================================================
# Backlog review round 3: the REAL B4 explicit path is
# GraphOutboxBatchProcessor.process_dataset(dataset_id), which snapshots ids
# via list_processable_ids_for_dataset -- not a directly-named
# GraphOutboxProcessor.process(id) call. These scenarios prove the
# coexistence guarantees through that exact real path.
# =============================================================================


async def wait_until_status(
    session_factory: AsyncSessionFactory,
    event_id: int,
    status: GraphOutboxStatus,
    *,
    timeout: float = 5.0,
) -> None:
    async def _poll() -> None:
        while True:
            event = await load_event(session_factory, event_id)
            if event is not None and event.status == status:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r1b_batch_drain_observes_autonomous_owned_row_until_done(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    """The exact B4 race: autonomous wins the claim, then the *real*
    ``GraphOutboxBatchProcessor.process_dataset`` (not a bare
    ``process(id)`` call) must still converge -- never skip the row because
    it wasn't ``PENDING``/``FAILED`` at snapshot time, and never return
    before it reaches ``DONE`` (backlog review round 3, SS 1-3, 5)."""

    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        slow_projection = DelayedProjection(Neo4jProjection(neo4j_resource), delay_seconds=0.6)
        autonomous = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=slow_projection,
            worker_id="wk-autonomous-r1b",
        )
        explicit_processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-explicit-r1b",
            explicit_observe_interval_seconds=0.02,
        )
        drain = GraphOutboxBatchProcessor(
            session_factory=postgres_session_factory, processor=explicit_processor
        )

        autonomous_task = asyncio.ensure_future(autonomous.claim_and_process_one())
        await wait_until_status(postgres_session_factory, event_id, GraphOutboxStatus.PROCESSING)

        started = asyncio.get_event_loop().time()
        result = await drain.process_dataset(dataset_id)
        elapsed = asyncio.get_event_loop().time() - started

        autonomous_result = await autonomous_task

        assert result.processed == 1  # snapshot included the PROCESSING row
        assert elapsed >= 0.4  # did not return before the lease resolved
        assert autonomous_result is not None
        assert autonomous_result.attempt == 1
        assert len(slow_projection.apply_calls) == 1  # never double-applied

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.DONE
        assert persisted.worker_id == "wk-autonomous-r1b"  # ownership never stolen
        assert persisted.attempt == 1
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r2b_batch_drain_wins_race_autonomous_does_not_steal(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    """Inverse of R1b: the real batch drain wins the claim first; the
    autonomous poll loop must never steal a live, non-stale lease from it
    (backlog review round 3, SS 6)."""

    dataset_id = uuid4()
    entity_id = uuid4()
    event_id = await insert_entity_event(
        postgres_session_factory, dataset_id=dataset_id, entity_id=entity_id
    )
    try:
        slow_projection = DelayedProjection(Neo4jProjection(neo4j_resource), delay_seconds=0.6)
        explicit_processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=slow_projection,
            worker_id="wk-explicit-r2b",
        )
        drain = GraphOutboxBatchProcessor(
            session_factory=postgres_session_factory, processor=explicit_processor
        )
        autonomous = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-autonomous-r2b",
        )

        drain_task = asyncio.ensure_future(drain.process_dataset(dataset_id))
        await wait_until_status(postgres_session_factory, event_id, GraphOutboxStatus.PROCESSING)

        autonomous_attempts_while_live: list[object] = []
        for _ in range(3):
            autonomous_attempts_while_live.append(await autonomous.claim_and_process_one())
            await asyncio.sleep(0.05)

        drain_result = await drain_task
        autonomous_after = await autonomous.claim_and_process_one()

        assert all(result is None for result in autonomous_attempts_while_live)
        assert drain_result.processed == 1
        assert autonomous_after is None
        assert len(slow_projection.apply_calls) == 1

        persisted = await load_event(postgres_session_factory, event_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.DONE
        assert persisted.worker_id == "wk-explicit-r2b"
        assert persisted.attempt == 1
        assert await entity_count(neo4j_resource, str(entity_id)) == 1
    finally:
        await cleanup_events(postgres_session_factory, [event_id])
        await cleanup_entity(neo4j_resource, str(entity_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_row_batch_snapshot_includes_processing_and_pending(
    postgres_session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource,
) -> None:
    """Backlog review round 3, SS 7: one dataset, two rows -- one already
    ``PROCESSING`` under the autonomous consumer, one still ``PENDING``.
    The real batch drain must converge both, skip neither, and never
    double-apply either."""

    dataset_id = uuid4()
    processing_entity_id = uuid4()
    pending_entity_id = uuid4()
    processing_event_id = await insert_entity_event(
        postgres_session_factory,
        dataset_id=dataset_id,
        entity_id=processing_entity_id,
        name="Processing Row Entity",
    )
    pending_event_id = await insert_entity_event(
        postgres_session_factory,
        dataset_id=dataset_id,
        entity_id=pending_entity_id,
        name="Pending Row Entity",
    )
    try:
        slow_projection = DelayedProjection(Neo4jProjection(neo4j_resource), delay_seconds=0.4)
        autonomous = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=slow_projection,
            worker_id="wk-autonomous-multi",
        )
        explicit_processor = GraphOutboxProcessor(
            session_factory=postgres_session_factory,
            projection=Neo4jProjection(neo4j_resource),
            worker_id="wk-explicit-multi",
            explicit_observe_interval_seconds=0.02,
        )
        drain = GraphOutboxBatchProcessor(
            session_factory=postgres_session_factory, processor=explicit_processor
        )

        autonomous_task = asyncio.ensure_future(autonomous.claim_and_process_one())
        await wait_until_status(
            postgres_session_factory, processing_event_id, GraphOutboxStatus.PROCESSING
        )

        result = await drain.process_dataset(dataset_id)
        autonomous_result = await autonomous_task

        assert result.processed == 2
        assert autonomous_result is not None
        assert autonomous_result.attempt == 1
        assert len(slow_projection.apply_calls) == 1  # the PROCESSING row, applied once

        processing_persisted = await load_event(postgres_session_factory, processing_event_id)
        pending_persisted = await load_event(postgres_session_factory, pending_event_id)
        assert processing_persisted is not None
        assert pending_persisted is not None
        assert processing_persisted.status == GraphOutboxStatus.DONE
        assert processing_persisted.worker_id == "wk-autonomous-multi"
        assert pending_persisted.status == GraphOutboxStatus.DONE
        assert pending_persisted.worker_id == "wk-explicit-multi"

        assert await entity_count(neo4j_resource, str(processing_entity_id)) == 1
        assert await entity_count(neo4j_resource, str(pending_entity_id)) == 1
    finally:
        await cleanup_events(postgres_session_factory, [processing_event_id, pending_event_id])
        await cleanup_entity(neo4j_resource, str(processing_entity_id))
        await cleanup_entity(neo4j_resource, str(pending_entity_id))
