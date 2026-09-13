"""Real-PostgreSQL tests for the SM-1001 Native Cognitive Memory foundation
(ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory).

Proves the structural invariants that must be enforced by PostgreSQL itself,
not merely by application code: lifecycle-shape CHECK constraints, the 1:1
``memory_provenance`` relationship, self-supersession rejection, the
single-direct-predecessor lineage guarantee, and the
``cognitive_memory_idempotency`` ``UNIQUE(idempotency_key)`` race authority.

Requires migrations already applied through 0018 against the configured
PostgreSQL database. No public route, no embedding provider call, no Neo4j,
and no ``graph_outbox``/``PipelineRun`` involvement anywhere in this suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError, IntegrityError

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    MemoryItem,
    MemoryProvenance,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory

POSTGRES_COGNITIVE_MEMORY_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_COGNITIVE_MEMORY_TESTS"

FAKE_EMBEDDING = [0.0] * 3072


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_COGNITIVE_MEMORY_ENV) != "1":
        pytest.skip(
            f"set {POSTGRES_COGNITIVE_MEMORY_ENV}=1 to run PostgreSQL Cognitive Memory tests"
        )

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup(session_factory: AsyncSessionFactory, *, memory_ids: set[UUID]) -> None:
    if not memory_ids:
        return
    async with session_factory() as session:
        await session.execute(
            delete(CognitiveMemoryIdempotency).where(
                CognitiveMemoryIdempotency.target_memory_id.in_(memory_ids)
                | CognitiveMemoryIdempotency.result_memory_id.in_(memory_ids)
            )
        )
        await session.execute(delete(MemoryItem).where(MemoryItem.superseded_by.in_(memory_ids)))
        await session.execute(delete(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
        await session.commit()


def unique_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def flush_and_expect_integrity_error(session, obj: object) -> None:
    session.add(obj)
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.flush()
    await session.rollback()


# --- fresh upgrade / structural shape ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_valid_active_item_with_provenance(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            item = MemoryItem(
                memory_type="profile",
                scope="global",
                content="Prefers teal interfaces.",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(item)
            await session.flush()
            memory_ids.add(item.id)

            provenance = MemoryProvenance(
                memory_id=item.id,
                origin_kind="user_asserted",
                source_system="sofias-assistant",
                source_ref="conversation-1234",
            )
            session.add(provenance)
            await session.flush()
            await session.commit()

        async with postgres_session_factory() as session:
            persisted = await session.get(MemoryItem, item.id)
            assert persisted is not None
            assert persisted.lifecycle == "active"
            assert persisted.embedding is not None
            assert len(persisted.embedding) == 3072

            persisted_provenance = await session.get(MemoryProvenance, item.id)
            assert persisted_provenance is not None
            assert persisted_provenance.origin_kind == "user_asserted"
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_valid_superseded_item(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            old = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="Old fact.",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(old)
            await session.flush()
            memory_ids.add(old.id)

            replacement = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="Corrected fact.",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(replacement)
            await session.flush()
            memory_ids.add(replacement.id)

            old.lifecycle = "superseded"
            old.superseded_at = datetime.now(UTC)
            old.superseded_by = replacement.id
            await session.flush()
            await session.commit()

        async with postgres_session_factory() as session:
            persisted_old = await session.get(MemoryItem, old.id)
            assert persisted_old is not None
            assert persisted_old.lifecycle == "superseded"
            assert persisted_old.superseded_by == replacement.id
            # Historical content is preserved -- SUPERSEDED != FORGOTTEN.
            assert persisted_old.content == "Old fact."
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_valid_forgotten_tombstone(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            tombstone = MemoryItem(
                memory_type="semantic",
                lifecycle="forgotten",
                forgotten_at=datetime.now(UTC),
            )
            session.add(tombstone)
            await session.flush()
            memory_ids.add(tombstone.id)
            await session.commit()

        async with postgres_session_factory() as session:
            persisted = await session.get(MemoryItem, tombstone.id)
            assert persisted is not None
            assert persisted.lifecycle == "forgotten"
            assert persisted.scope is None
            assert persisted.content is None
            assert persisted.embedding is None
            assert persisted.confidence is None
            assert persisted.valid_from is None
            assert persisted.valid_until is None
            assert persisted.forgotten_at is not None
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


# --- rejected malformed lifecycle rows ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_active_missing_content(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            scope="global",
            content=None,
            embedding=FAKE_EMBEDDING,
            lifecycle="active",
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_active_with_forgotten_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            scope="global",
            content="x",
            embedding=FAKE_EMBEDDING,
            lifecycle="active",
            forgotten_at=datetime.now(UTC),
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_superseded_without_superseded_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            scope="global",
            content="x",
            embedding=FAKE_EMBEDDING,
            lifecycle="superseded",
            superseded_by=uuid4(),
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_superseded_without_superseded_by(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            scope="global",
            content="x",
            embedding=FAKE_EMBEDDING,
            lifecycle="superseded",
            superseded_at=datetime.now(UTC),
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_forgotten_retaining_content(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            content="leftover",
            lifecycle="forgotten",
            forgotten_at=datetime.now(UTC),
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_forgotten_retaining_embedding(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            embedding=FAKE_EMBEDDING,
            lifecycle="forgotten",
            forgotten_at=datetime.now(UTC),
        )
        await flush_and_expect_integrity_error(session, bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_forgotten_retaining_scope(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = MemoryItem(
            memory_type="semantic",
            scope="global",
            lifecycle="forgotten",
            forgotten_at=datetime.now(UTC),
        )
        await flush_and_expect_integrity_error(session, bad)


# --- lineage invariants ----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_self_supersession(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            item = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="x",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(item)
            await session.flush()
            memory_ids.add(item.id)
            await session.commit()

        async with postgres_session_factory() as session:
            persisted = await session.get(MemoryItem, item.id)
            assert persisted is not None
            persisted.lifecycle = "superseded"
            persisted.superseded_at = datetime.now(UTC)
            persisted.superseded_by = persisted.id
            with pytest.raises((IntegrityError, DBAPIError)):
                await session.flush()
            await session.rollback()
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_two_direct_predecessors_for_one_replacement(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            target = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="target",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(target)
            await session.flush()
            memory_ids.add(target.id)

            predecessor_a = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="a",
                embedding=FAKE_EMBEDDING,
                lifecycle="superseded",
                superseded_at=datetime.now(UTC),
                superseded_by=target.id,
            )
            session.add(predecessor_a)
            await session.flush()
            memory_ids.add(predecessor_a.id)
            await session.commit()

        async with postgres_session_factory() as session:
            predecessor_b = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="b",
                embedding=FAKE_EMBEDDING,
                lifecycle="superseded",
                superseded_at=datetime.now(UTC),
                superseded_by=target.id,
            )
            await flush_and_expect_integrity_error(session, predecessor_b)
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


# --- provenance 1:1 ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_is_one_to_one_with_memory_item(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            item = MemoryItem(
                memory_type="profile",
                scope="global",
                content="x",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(item)
            await session.flush()
            memory_ids.add(item.id)

            provenance = MemoryProvenance(
                memory_id=item.id,
                origin_kind="tool_observed",
                source_system="sofias-assistant",
                source_ref="tool-run-1",
                observed_at=datetime.now(UTC),
            )
            session.add(provenance)
            await session.flush()
            await session.commit()

        async with postgres_session_factory() as session:
            duplicate = MemoryProvenance(
                memory_id=item.id,
                origin_kind="imported",
                source_system="sofias-assistant",
                source_ref="dup",
            )
            await flush_and_expect_integrity_error(session, duplicate)
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)


# --- idempotency ledger race authority --------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_unique_constraint_is_authoritative(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = unique_key("sm1001-idempotency")
    try:
        async with postgres_session_factory() as session:
            first = CognitiveMemoryIdempotency(
                idempotency_key=key,
                operation="create",
                request_digest="a" * 64,
                target_memory_id=None,
            )
            session.add(first)
            await session.flush()
            await session.commit()

        async with postgres_session_factory() as session:
            second = CognitiveMemoryIdempotency(
                idempotency_key=key,
                operation="create",
                request_digest="b" * 64,
                target_memory_id=None,
            )
            await flush_and_expect_integrity_error(session, second)
    finally:
        async with postgres_session_factory() as session:
            await session.execute(
                delete(CognitiveMemoryIdempotency).where(
                    CognitiveMemoryIdempotency.idempotency_key == key
                )
            )
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_target_memory_id_matches_operation_shape(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        bad = CognitiveMemoryIdempotency(
            idempotency_key=unique_key("sm1001-shape"),
            operation="create",
            request_digest="c" * 64,
            target_memory_id=uuid4(),
        )
        await flush_and_expect_integrity_error(session, bad)


# --- no Neo4j / graph_outbox / PipelineRun coupling -------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_pipeline_run_or_graph_outbox_row_is_created(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Cognitive Memory writes never create PipelineRun/PipelineStep rows or
    graph_outbox events -- inserting a MemoryItem directly (no service layer
    yet exists in SM-1001) must not touch those tables at all."""

    from sqlalchemy import func, select

    from sofias_memory.infrastructure.postgres.models import GraphOutbox, PipelineRun

    memory_ids: set[UUID] = set()
    try:
        async with postgres_session_factory() as session:
            before_runs = await session.scalar(select(func.count()).select_from(PipelineRun))
            before_outbox = await session.scalar(select(func.count()).select_from(GraphOutbox))

            item = MemoryItem(
                memory_type="semantic",
                scope="global",
                content="isolated",
                embedding=FAKE_EMBEDDING,
                lifecycle="active",
            )
            session.add(item)
            await session.flush()
            memory_ids.add(item.id)
            await session.commit()

        async with postgres_session_factory() as session:
            after_runs = await session.scalar(select(func.count()).select_from(PipelineRun))
            after_outbox = await session.scalar(select(func.count()).select_from(GraphOutbox))

            assert after_runs == before_runs
            assert after_outbox == before_outbox
    finally:
        await cleanup(postgres_session_factory, memory_ids=memory_ids)
