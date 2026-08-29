"""Real-PostgreSQL tests for the SM-506 graph_outbox lease/claim mechanism
(ADR-0009 SS V): claim eligibility, concurrent claim exclusivity, stale
reclaim, legacy-NULL-lease recovery, the FAILED attempt ceiling, old-owner
finalization fencing, DB-time authority, claim-commit visibility, and
retry-not-busy-spinning.

Exercises :class:`GraphOutboxRepository` directly (no Neo4j, no
:class:`GraphOutboxProcessor`) -- this file proves the PostgreSQL-only lease
mechanics; ``test_graph_outbox_processor_integration.py`` proves the
Neo4j-dependent replay/coexistence scenarios on top of it.

Requires a dedicated, discardable PostgreSQL database with migrations
already applied through 0009.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import GraphOutbox
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.graph_outbox_processor import (
    DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
)

GRAPH_OUTBOX_WORKER_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_OUTBOX_WORKER_POSTGRES_TESTS"
GRAPH_OUTBOX_WORKER_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_GRAPH_OUTBOX_WORKER_TEST_DATABASE_URL"
GRAPH_OUTBOX_WORKER_TEST_DATABASE_NAME = "sofias_memory_graph_outbox_worker_test"


def graph_outbox_worker_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(GRAPH_OUTBOX_WORKER_TESTS_ENV) != "1":
        pytest.skip(f"set {GRAPH_OUTBOX_WORKER_TESTS_ENV}=1 to run graph outbox worker tests")

    database_url = env.get(GRAPH_OUTBOX_WORKER_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {GRAPH_OUTBOX_WORKER_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_database_url(database_url)
    return database_url


def _validate_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("graph outbox worker PostgreSQL test database URL is invalid")

    if parsed_url.database != GRAPH_OUTBOX_WORKER_TEST_DATABASE_NAME:
        pytest.skip(
            "graph outbox worker PostgreSQL tests require the exact dedicated database "
            f"{GRAPH_OUTBOX_WORKER_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = graph_outbox_worker_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_dedicated_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_dedicated_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != GRAPH_OUTBOX_WORKER_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated graph outbox worker test database"
        )


def test_graph_outbox_worker_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_outbox_worker_test_database_url({})


def test_graph_outbox_worker_postgres_tests_skip_on_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        graph_outbox_worker_test_database_url(
            {
                GRAPH_OUTBOX_WORKER_TESTS_ENV: "1",
                GRAPH_OUTBOX_WORKER_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# -- fixtures / helpers -------------------------------------------------------


def entity_upsert_payload(dataset_id: object, entity_id: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "aggregate_type": "entity",
        "operation": "upsert",
        "dataset_id": str(dataset_id),
        "aggregate_id": str(entity_id),
        "identity": {"id": str(entity_id)},
        "properties": {
            "id": str(entity_id),
            "dataset_id": str(dataset_id),
            "name": "Worker Integration Entity",
            "entity_type": "test",
            "description": "Created by graph outbox worker integration test.",
            "importance_weight": 0.5,
            "generation": 1,
        },
    }


async def insert_event(
    session_factory: AsyncSessionFactory,
    *,
    status: GraphOutboxStatus = GraphOutboxStatus.PENDING,
    attempt: int = 0,
    processing_started_at: datetime | None = None,
    worker_id: str | None = None,
) -> int:
    dataset_id = uuid4()
    entity_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as uow:
        event = GraphOutbox(
            dataset_id=dataset_id,
            aggregate_type="entity",
            aggregate_id=entity_id,
            operation=GraphOutboxOperation.UPSERT,
            payload=entity_upsert_payload(dataset_id, entity_id),
            status=status,
            attempt=attempt,
            processed_at=None,
            processing_started_at=processing_started_at,
            worker_id=worker_id,
        )
        await uow.graph_outbox.add(event)
        await uow.commit()
        return event.id


async def insert_dataset_scoped_event(
    session_factory: AsyncSessionFactory,
    *,
    dataset_id: object,
    aggregate_type: str,
    operation: GraphOutboxOperation,
    status: GraphOutboxStatus = GraphOutboxStatus.PENDING,
    attempt: int = 0,
) -> int:
    """Insert one minimal, schema-valid outbox row for a specific dataset,
    aggregate type and operation -- used to reproduce a mixed dataset-wide
    snapshot (stale UPSERTs alongside a fresh administrative delete's
    DELETEs) without requiring the full producer pipelines that normally
    create such rows."""

    aggregate_id = uuid4()
    if aggregate_type in ("entity", "chunk"):
        identity: dict[str, object] = {"id": str(aggregate_id)}
        endpoints: dict[str, object] = {}
    elif aggregate_type == "relation":
        identity = {"relation_id": str(aggregate_id)}
        endpoints = {"source_entity_id": str(uuid4()), "target_entity_id": str(uuid4())}
    elif aggregate_type == "entity_mention":
        identity = {"mention_id": str(aggregate_id)}
        endpoints = {"entity_id": str(uuid4()), "chunk_id": str(uuid4())}
    else:
        raise ValueError(f"unsupported aggregate_type for this helper: {aggregate_type}")

    payload: dict[str, object] = {
        "schema_version": 1,
        "aggregate_type": aggregate_type,
        "operation": operation.value,
        "dataset_id": str(dataset_id),
        "aggregate_id": str(aggregate_id),
        "identity": identity,
        "endpoints": endpoints,
        "properties": {},
    }
    async with PostgresUnitOfWork(session_factory) as uow:
        event = GraphOutbox(
            dataset_id=dataset_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            operation=operation,
            payload=payload,
            status=status,
            attempt=attempt,
            processed_at=None,
        )
        await uow.graph_outbox.add(event)
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


@dataclass(frozen=True)
class EventSnapshot:
    """Plain data read out of the ORM row *inside* its owning session --
    :class:`GraphOutbox` instances become detached (attribute access raises)
    once the session that loaded them closes, so tests must never hold onto
    the ORM instance itself past its ``async with PostgresUnitOfWork`` block.
    """

    status: GraphOutboxStatus
    attempt: int
    worker_id: str | None
    processing_started_at: datetime | None
    processed_at: datetime | None


async def load_event(session_factory: AsyncSessionFactory, outbox_id: int) -> EventSnapshot | None:
    async with PostgresUnitOfWork(session_factory) as uow:
        event = await uow.graph_outbox.get_by_id(outbox_id)
        if event is None:
            return None
        return EventSnapshot(
            status=event.status,
            attempt=event.attempt,
            worker_id=event.worker_id,
            processing_started_at=event.processing_started_at,
            processed_at=event.processed_at,
        )


async def cleanup_event(session_factory: AsyncSessionFactory, outbox_id: int) -> None:
    async with session_factory() as session:
        await session.execute(delete(GraphOutbox).where(GraphOutbox.id == outbox_id))
        await session.commit()


CLAIM_KWARGS = {
    "stale_after_seconds": DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS,
    "max_attempts": DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
}


# -- A: pending claim ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pending_claim_sets_lease_and_attempt(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()

        assert claimed is not None
        assert claimed.worker_id == "wk-a"
        assert claimed.attempt == 1
        assert claimed.processing_started_at is not None

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.PROCESSING
        assert persisted.worker_id == "wk-a"
        assert persisted.attempt == 1
        assert persisted.processing_started_at is not None
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- B: concurrent claim exclusivity --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_two_concurrent_claimers_only_one_wins(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:

        async def try_claim(worker_id: str) -> object | None:
            async with PostgresUnitOfWork(session_factory) as uow:
                claimed = await uow.graph_outbox.claim_one(
                    outbox_id, worker_id=worker_id, **CLAIM_KWARGS
                )
                await uow.commit()
                return claimed

        results = await asyncio.gather(try_claim("wk-1"), try_claim("wk-2"))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.attempt == 1
        assert persisted.worker_id == winners[0].worker_id  # type: ignore[union-attr]
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- C: non-stale PROCESSING is not stolen --------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c_non_stale_processing_is_not_stolen(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            first = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert first is not None

        async with PostgresUnitOfWork(session_factory) as uow:
            second = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-b", **CLAIM_KWARGS)
            await uow.commit()
        assert second is None

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.worker_id == "wk-a"
        assert persisted.attempt == 1
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- D: stale PROCESSING is reclaimed --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_stale_processing_is_reclaimed(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            first = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert first is not None

        await backdate_processing_started_at(
            session_factory, outbox_id, seconds_ago=DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS + 5
        )

        async with PostgresUnitOfWork(session_factory) as uow:
            second = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-b", **CLAIM_KWARGS)
            await uow.commit()

        assert second is not None
        assert second.worker_id == "wk-b"
        assert second.attempt == 2
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- E: legacy NULL lease is recovered -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_legacy_null_lease_processing_row_is_recovered(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(
        session_factory,
        status=GraphOutboxStatus.PROCESSING,
        attempt=1,
        processing_started_at=None,
        worker_id=None,
    )
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                outbox_id, worker_id="wk-recovery", **CLAIM_KWARGS
            )
            await uow.commit()

        assert claimed is not None
        assert claimed.worker_id == "wk-recovery"
        assert claimed.attempt == 2
        assert claimed.processing_started_at is not None
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- F/G: FAILED attempt ceiling -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_failed_below_ceiling_is_claimable(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(
        session_factory,
        status=GraphOutboxStatus.FAILED,
        attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS - 1,
    )
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()

        assert claimed is not None
        assert claimed.attempt == DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
    finally:
        await cleanup_event(session_factory, outbox_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_failed_at_ceiling_is_not_claimable(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(
        session_factory,
        status=GraphOutboxStatus.FAILED,
        attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()

        assert claimed is None

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.FAILED
        assert persisted.attempt == DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- H: stale reclaim finalization fencing ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_h_old_owner_cannot_finalize_after_stale_reclaim(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            owner_a = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert owner_a is not None
        assert owner_a.attempt == 1

        await backdate_processing_started_at(
            session_factory, outbox_id, seconds_ago=DEFAULT_GRAPH_OUTBOX_STALE_AFTER_SECONDS + 5
        )

        async with PostgresUnitOfWork(session_factory) as uow:
            owner_b = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-b", **CLAIM_KWARGS)
            await uow.commit()
        assert owner_b is not None
        assert owner_b.attempt == 2

        async with PostgresUnitOfWork(session_factory) as uow:
            stale_owner_done = await uow.graph_outbox.mark_done_if_owned(
                outbox_id, worker_id="wk-a", attempt=1
            )
            await uow.commit()
        assert stale_owner_done is False

        async with PostgresUnitOfWork(session_factory) as uow:
            stale_owner_failed = await uow.graph_outbox.mark_failed_if_owned(
                outbox_id, worker_id="wk-a", attempt=1
            )
            await uow.commit()
        assert stale_owner_failed is False

        persisted_still_processing = await load_event(session_factory, outbox_id)
        assert persisted_still_processing is not None
        assert persisted_still_processing.status == GraphOutboxStatus.PROCESSING
        assert persisted_still_processing.worker_id == "wk-b"

        async with PostgresUnitOfWork(session_factory) as uow:
            current_owner_done = await uow.graph_outbox.mark_done_if_owned(
                outbox_id, worker_id="wk-b", attempt=2
            )
            await uow.commit()
        assert current_owner_done is True

        persisted_done = await load_event(session_factory, outbox_id)
        assert persisted_done is not None
        assert persisted_done.status == GraphOutboxStatus.DONE
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- I: DONE is never reclaimed ---------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_i_done_row_is_never_reclaimed(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert claimed is not None

        async with PostgresUnitOfWork(session_factory) as uow:
            done = await uow.graph_outbox.mark_done_if_owned(outbox_id, worker_id="wk-a", attempt=1)
            await uow.commit()
        assert done is True

        async with PostgresUnitOfWork(session_factory) as uow:
            reclaim_attempt = await uow.graph_outbox.claim_one(
                outbox_id, worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert reclaim_attempt is None
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- J: DB-time authority -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_processing_started_at_and_processed_at_are_db_authoritative(
    postgres_engine: AsyncEngine,
) -> None:
    """Brackets against PostgreSQL's own ``now()`` on both sides, never the
    test process's host clock -- a container/host clock offset must never
    make this test flaky, and the whole point of the assertion is that
    these columns come from the database's clock, not the application's."""

    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            before_claim = await uow.graph_outbox.get_database_now()
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            after_claim = await uow.graph_outbox.get_database_now()
            await uow.commit()

        assert claimed is not None
        assert before_claim - timedelta(seconds=2) <= claimed.processing_started_at
        assert claimed.processing_started_at <= after_claim + timedelta(seconds=2)

        async with PostgresUnitOfWork(session_factory) as uow:
            await uow.graph_outbox.mark_done_if_owned(outbox_id, worker_id="wk-a", attempt=1)
            await uow.commit()

        async with PostgresUnitOfWork(session_factory) as uow:
            after_done = await uow.graph_outbox.get_database_now()
            await uow.commit()

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.processed_at is not None
        assert before_claim - timedelta(seconds=2) <= persisted.processed_at
        assert persisted.processed_at <= after_done + timedelta(seconds=2)
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- K: claim commit visible before apply/finalize ------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_claim_commit_is_visible_to_other_sessions_before_finalize(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert claimed is not None

        # A brand-new session/connection must already observe PROCESSING --
        # the claim transaction committed before any "apply" work began.
        other_session_view = await load_event(session_factory, outbox_id)
        assert other_session_view is not None
        assert other_session_view.status == GraphOutboxStatus.PROCESSING
        assert other_session_view.worker_id == "wk-a"

        async with postgres_engine.connect() as connection:
            other_dataset = await connection.execute(
                text("SELECT count(*) FROM graph_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
            assert other_dataset.scalar() == 1
    finally:
        await cleanup_event(session_factory, outbox_id)


# -- M: dataset-wide drain snapshot orders UPSERTs before DELETEs --------------
#
# Regression for a real Easypanel production incident: a Dataset delete's
# fresh entity/chunk DELETEs were scheduled ahead of an earlier, still-
# unconverged Cognify run's entity_mention/relation UPSERTs (ordering was by
# aggregate_type alone), so the UPSERTs failed with
# ProjectionEndpointMissingError once their Entity/Chunk endpoint had
# already been deleted. See ``list_processable_ids_for_dataset``'s docstring
# for the fix (two-phase ``(operation, aggregate_type)`` ordering).


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_dataset_snapshot_orders_every_upsert_before_any_delete(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    dataset_id = uuid4()
    outbox_ids: dict[str, int] = {}
    try:
        # Stale upserts left over from an earlier, still-converging pipeline
        # run -- inserted in a deliberately scrambled order so a passing
        # test cannot be an artifact of insertion order.
        outbox_ids["relation_upsert"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="relation",
            operation=GraphOutboxOperation.UPSERT,
            status=GraphOutboxStatus.FAILED,
            attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS - 1,
        )
        outbox_ids["chunk_upsert"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="chunk",
            operation=GraphOutboxOperation.UPSERT,
        )
        outbox_ids["entity_mention_upsert"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity_mention",
            operation=GraphOutboxOperation.UPSERT,
        )
        outbox_ids["entity_upsert"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity",
            operation=GraphOutboxOperation.UPSERT,
        )

        # A fresh administrative Dataset delete's own DELETEs, enqueued
        # after the stale upserts above.
        outbox_ids["entity_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity",
            operation=GraphOutboxOperation.DELETE,
        )
        outbox_ids["entity_mention_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity_mention",
            operation=GraphOutboxOperation.DELETE,
        )
        outbox_ids["chunk_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="chunk",
            operation=GraphOutboxOperation.DELETE,
        )
        outbox_ids["relation_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="relation",
            operation=GraphOutboxOperation.DELETE,
        )

        async with PostgresUnitOfWork(session_factory) as uow:
            ordered_ids = await uow.graph_outbox.list_processable_ids_for_dataset(
                dataset_id, max_attempts=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
            )
            await uow.commit()

        expected_order = [
            outbox_ids["entity_upsert"],
            outbox_ids["chunk_upsert"],
            outbox_ids["entity_mention_upsert"],
            outbox_ids["relation_upsert"],
            outbox_ids["relation_delete"],
            outbox_ids["entity_mention_delete"],
            outbox_ids["chunk_delete"],
            outbox_ids["entity_delete"],
        ]
        assert ordered_ids == expected_order

        upsert_ids = set(expected_order[:4])
        delete_ids = set(expected_order[4:])
        last_upsert_position = max(ordered_ids.index(i) for i in upsert_ids)
        first_delete_position = min(ordered_ids.index(i) for i in delete_ids)
        assert last_upsert_position < first_delete_position
    finally:
        for outbox_id in outbox_ids.values():
            await cleanup_event(session_factory, outbox_id)


# -- N: cross-row fence -- DELETE unclaimable while a sibling UPSERT is live ----
#
# Regression for backlog review round 2's BLOCKER: round 1's ordering fix
# (test M above) only orders one dataset-wide snapshot processed by ONE
# sequential drain call. Row-level claim-or-observe only stops two workers
# from claiming the SAME row. Neither stops an autonomous worker from
# independently discovering and claiming a DELETE row for dataset X while a
# DIFFERENT, older UPSERT row for the same dataset is still PENDING,
# PROCESSING under a live lease (owned by any worker, autonomous or
# explicit), or FAILED-but-retryable -- list_claimable_ids' global scan has
# no per-dataset ordering at all. This proves the durable, PostgreSQL-
# authoritative cross-row fence added to list_claimable_ids/claim_one closes
# that gap without any in-memory mutex and without holding a lock on the
# blocking row.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_delete_is_unclaimable_while_dataset_has_a_live_upsert(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    dataset_id = uuid4()
    outbox_ids: dict[str, int] = {}
    try:
        # Step 2 (spec): a stale relation UPSERT, claimed and PROCESSING
        # under a live (non-stale) lease -- standing in for "worker A
        # claimed it and paused before its projection apply completed."
        outbox_ids["relation_upsert"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="relation",
            operation=GraphOutboxOperation.UPSERT,
            status=GraphOutboxStatus.PROCESSING,
            attempt=1,
        )
        async with PostgresUnitOfWork(session_factory) as uow:
            live_lease_now = await uow.graph_outbox.get_database_now()
            event = await uow.graph_outbox.get_by_id(outbox_ids["relation_upsert"])
            assert event is not None
            event.worker_id = "wk-a"
            event.processing_started_at = live_lease_now
            await uow.commit()

        # Step 4 (spec): DATASET_DELETE's own DELETE rows, PENDING.
        outbox_ids["entity_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity",
            operation=GraphOutboxOperation.DELETE,
        )
        outbox_ids["relation_delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="relation",
            operation=GraphOutboxOperation.DELETE,
        )

        # Step 5 (spec): worker B's autonomous discovery must not even
        # surface either DELETE row as a candidate while the UPSERT is live.
        async with PostgresUnitOfWork(session_factory) as uow:
            claimable = await uow.graph_outbox.list_claimable_ids(**CLAIM_KWARGS, limit=100)
            await uow.commit()
        assert outbox_ids["entity_delete"] not in claimable
        assert outbox_ids["relation_delete"] not in claimable

        # Step 6 (spec): even a direct claim-by-id (the atomic re-check,
        # closing the discovery/claim TOCTOU window) must refuse both
        # DELETE rows while the UPSERT remains unresolved.
        async with PostgresUnitOfWork(session_factory) as uow:
            blocked_entity_delete = await uow.graph_outbox.claim_one(
                outbox_ids["entity_delete"], worker_id="wk-b", **CLAIM_KWARGS
            )
            blocked_relation_delete = await uow.graph_outbox.claim_one(
                outbox_ids["relation_delete"], worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert blocked_entity_delete is None
        assert blocked_relation_delete is None

        persisted_deletes_still_pending = await load_event(
            session_factory, outbox_ids["entity_delete"]
        )
        assert persisted_deletes_still_pending is not None
        assert persisted_deletes_still_pending.status == GraphOutboxStatus.PENDING

        # Step 7 (spec): release A -- the UPSERT reaches DONE.
        async with PostgresUnitOfWork(session_factory) as uow:
            resolved = await uow.graph_outbox.mark_done_if_owned(
                outbox_ids["relation_upsert"], worker_id="wk-a", attempt=1
            )
            await uow.commit()
        assert resolved is True

        # Steps 8-9 (spec): the DELETEs are now claimable and processable.
        async with PostgresUnitOfWork(session_factory) as uow:
            claimable_after = await uow.graph_outbox.list_claimable_ids(**CLAIM_KWARGS, limit=100)
            await uow.commit()
        assert outbox_ids["entity_delete"] in claimable_after
        assert outbox_ids["relation_delete"] in claimable_after

        async with PostgresUnitOfWork(session_factory) as uow:
            claimed_entity_delete = await uow.graph_outbox.claim_one(
                outbox_ids["entity_delete"], worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert claimed_entity_delete is not None
    finally:
        for outbox_id in outbox_ids.values():
            await cleanup_event(session_factory, outbox_id)


# -- O: claim_one is the authoritative fence, independent of discovery ---------
#
# Freezes the invariant behind the round-2 fence: list_claimable_ids is only
# an optimization (it may skip candidates it already knows are blocked), but
# claim_one is what actually enforces the safety property. These tests never
# call list_claimable_ids at all -- they exercise claim_one directly against
# a DELETE row, parametrized over each state _blocking_upsert_status_predicate
# treats as "still relevant" for a sibling UPSERT in the same dataset.


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_state",
    ["pending", "processing_live", "failed_retryable"],
)
async def test_o_claim_one_is_the_authoritative_fence_independent_of_discovery(
    postgres_engine: AsyncEngine, blocking_state: str
) -> None:
    session_factory = create_session_factory(postgres_engine)
    dataset_id = uuid4()
    outbox_ids: dict[str, int] = {}
    try:
        # A: an UPSERT for dataset X in the blocking state under test.
        if blocking_state == "pending":
            outbox_ids["upsert"] = await insert_dataset_scoped_event(
                session_factory,
                dataset_id=dataset_id,
                aggregate_type="entity",
                operation=GraphOutboxOperation.UPSERT,
                status=GraphOutboxStatus.PENDING,
            )
        elif blocking_state == "processing_live":
            outbox_ids["upsert"] = await insert_dataset_scoped_event(
                session_factory,
                dataset_id=dataset_id,
                aggregate_type="entity",
                operation=GraphOutboxOperation.UPSERT,
                status=GraphOutboxStatus.PROCESSING,
                attempt=1,
            )
            async with PostgresUnitOfWork(session_factory) as uow:
                live_lease_now = await uow.graph_outbox.get_database_now()
                event = await uow.graph_outbox.get_by_id(outbox_ids["upsert"])
                assert event is not None
                event.worker_id = "wk-blocking"
                event.processing_started_at = live_lease_now
                await uow.commit()
        else:
            assert blocking_state == "failed_retryable"
            outbox_ids["upsert"] = await insert_dataset_scoped_event(
                session_factory,
                dataset_id=dataset_id,
                aggregate_type="entity",
                operation=GraphOutboxOperation.UPSERT,
                status=GraphOutboxStatus.FAILED,
                attempt=DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS - 1,
            )

        # B: a DELETE row for the same dataset X.
        outbox_ids["delete"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_id,
            aggregate_type="entity",
            operation=GraphOutboxOperation.DELETE,
        )

        # C/D: claim_one called directly -- list_claimable_ids is never
        # invoked in this test at all.
        async with PostgresUnitOfWork(session_factory) as uow:
            blocked = await uow.graph_outbox.claim_one(
                outbox_ids["delete"], worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert blocked is None

        # E: the DELETE row is untouched -- still PENDING, not PROCESSING.
        delete_after_block = await load_event(session_factory, outbox_ids["delete"])
        assert delete_after_block is not None
        assert delete_after_block.status == GraphOutboxStatus.PENDING

        # F: resolve the blocking UPSERT -- DONE for pending/live-processing,
        # FAILED-at-ceiling (terminal, no resurrection path) for the failed case.
        async with PostgresUnitOfWork(session_factory) as uow:
            upsert_event = await uow.graph_outbox.get_by_id(outbox_ids["upsert"])
            assert upsert_event is not None
            if blocking_state == "failed_retryable":
                upsert_event.status = GraphOutboxStatus.FAILED
                upsert_event.attempt = DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS
            else:
                upsert_event.status = GraphOutboxStatus.DONE
                upsert_event.processed_at = await uow.graph_outbox.get_database_now()
            await uow.commit()

        # G/H: claim_one now succeeds.
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                outbox_ids["delete"], worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert claimed is not None
        assert claimed.outbox_id == outbox_ids["delete"]
    finally:
        for outbox_id in outbox_ids.values():
            await cleanup_event(session_factory, outbox_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_blocking_upsert_in_dataset_x_does_not_block_delete_in_dataset_y(
    postgres_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(postgres_engine)
    dataset_x = uuid4()
    dataset_y = uuid4()
    outbox_ids: dict[str, int] = {}
    try:
        outbox_ids["upsert_x"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_x,
            aggregate_type="entity",
            operation=GraphOutboxOperation.UPSERT,
            status=GraphOutboxStatus.PENDING,
        )
        outbox_ids["delete_y"] = await insert_dataset_scoped_event(
            session_factory,
            dataset_id=dataset_y,
            aggregate_type="entity",
            operation=GraphOutboxOperation.DELETE,
        )

        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(
                outbox_ids["delete_y"], worker_id="wk-b", **CLAIM_KWARGS
            )
            await uow.commit()
        assert claimed is not None
        assert claimed.outbox_id == outbox_ids["delete_y"]
    finally:
        for outbox_id in outbox_ids.values():
            await cleanup_event(session_factory, outbox_id)


# -- L: a failure does not busy-spin the attempt budget in one tick -------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_l_single_claim_only_consumes_one_attempt(postgres_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(postgres_engine)
    outbox_id = await insert_event(session_factory)
    try:
        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.graph_outbox.claim_one(outbox_id, worker_id="wk-a", **CLAIM_KWARGS)
            await uow.commit()
        assert claimed is not None
        assert claimed.attempt == 1

        async with PostgresUnitOfWork(session_factory) as uow:
            failed = await uow.graph_outbox.mark_failed_if_owned(
                outbox_id, worker_id="wk-a", attempt=1
            )
            await uow.commit()
        assert failed is True

        persisted = await load_event(session_factory, outbox_id)
        assert persisted is not None
        assert persisted.status == GraphOutboxStatus.FAILED
        # Exactly the one attempt this single claim consumed -- a caller
        # must poll again (a later tick) to earn attempt 2, never loop
        # claim-fail-claim against the same row within one burst.
        assert persisted.attempt == 1
    finally:
        await cleanup_event(session_factory, outbox_id)
