"""Real-PostgreSQL tests for the SM-801 Agent persistence foundation.

Proves ADR-0014 / v0.5.0 Agent Management Feature Contract: the single-UUID
identity (no ``agent_uuid`` column), default field values, unique ``name``
creation concurrency, and the database-level CHECK constraints on
``name``/``display_name``/``description``/``instructions`` -- proven as
authoritative defenses, not merely mirrors of domain-layer checks that could
be skipped by a future direct-SQL caller. Requires migrations already
applied through 0015 against the configured PostgreSQL database. No public
route, no embedding provider call, no Neo4j, and no ``graph_outbox``
involvement anywhere in this suite -- SM-801 does not implement the public
API (SM-802) or any association table (SM-803/SM-804).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from sofias_memory.config import load_settings
from sofias_memory.domain import AgentStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Agent
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory

POSTGRES_AGENTS_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_AGENTS_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_AGENTS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENTS_ENV}=1 to run PostgreSQL Agent tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup_agents(session_factory: AsyncSessionFactory, *, agent_ids: set[UUID]) -> None:
    if not agent_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        await session.commit()


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def build_agent(*, name: str, **overrides: object) -> Agent:
    base: dict[str, object] = {
        "name": name,
        "display_name": None,
        "description": None,
        "instructions": None,
    }
    base.update(overrides)
    return Agent(**base)  # type: ignore[arg-type]


# --- persistence and defaults ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_agent_persists_with_defaults(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm801-defaults")
    agent_id = uuid4()

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            agent = await uow.agents.add(build_agent(name=name))
            agent.id = agent_id  # ensure a known id for the direct read below
            await uow.commit()

        async with postgres_session_factory() as session:
            persisted = await session.get(Agent, agent_id)
            assert persisted is not None
            assert persisted.name == name
            assert persisted.display_name is None
            assert persisted.description is None
            assert persisted.instructions is None
            assert persisted.metadata_ == {}
            assert persisted.status == AgentStatus.ACTIVE
            assert persisted.archived_at is None
            assert persisted.created_at is not None
            assert persisted.updated_at is not None
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids={agent_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_by_id_and_get_by_name_return_the_same_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm801-lookup")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        agent = await uow.agents.add(build_agent(name=name, display_name="Research Agent"))
        await uow.commit()
        agent_id = agent.id

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            by_id = await uow.agents.get_by_id(agent_id)
            by_name = await uow.agents.get_by_name(name)

            assert by_id is not None
            assert by_name is not None
            assert by_id.id == by_name.id == agent_id
            assert by_id.display_name == "Research Agent"
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids={agent_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_by_id_for_update_locks_a_single_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm801-lock")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        agent = await uow.agents.add(build_agent(name=name))
        await uow.commit()
        agent_id = agent.id

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            locked = await uow.agents.get_by_id_for_update(agent_id)
            assert locked is not None
            assert locked.id == agent_id
            await uow.commit()
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids={agent_id})


# --- single UUID identity -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agents_table_has_single_uuid_identity_no_agent_uuid_column(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        columns_result = await session.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agents'
                """
            )
        )
        columns = {str(row.column_name) for row in columns_result}

        pk_result = await session.execute(
            text(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'agents'::regclass AND i.indisprimary
                """
            )
        )
        primary_key_columns = {str(row.attname) for row in pk_result}

    assert "id" in columns
    assert "agent_uuid" not in columns
    assert primary_key_columns == {"id"}


# --- unique name --------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_by_database(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    name = unique_name("sm801-duplicate")

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        first = await uow.agents.add(build_agent(name=name))
        await uow.commit()
        first_id = first.id

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            # add() flushes immediately (AgentRepository.add), so the
            # unique-violation surfaces here, not at a later commit().
            with pytest.raises(IntegrityError):
                await uow.agents.add(build_agent(name=name))
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids={first_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_agent_creation_with_same_name_converges_to_one_committed_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """SM-801's own concurrency gate (backlog SS 22): with no public API and
    no name-conflict service translation yet (that arrives in SM-802), the
    primitive database/repository safety is proven directly -- N concurrent
    writers, exactly one commits, the rest fail on the unique constraint,
    and the final row count for the name is 1. No HTTP status is asserted
    here; that belongs to SM-802."""

    name = unique_name("sm801-concurrent")

    async def attempt(index: int) -> UUID | IntegrityError:
        try:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                agent = await uow.agents.add(
                    build_agent(name=name, description=f"Candidate {index}.")
                )
                await uow.commit()
                return agent.id
        except IntegrityError as exc:
            return exc

    results = await asyncio.gather(*(attempt(i) for i in range(5)))

    winners = [r for r in results if isinstance(r, UUID)]
    conflicts = [r for r in results if isinstance(r, IntegrityError)]

    try:
        assert len(winners) == 1
        assert len(conflicts) == 4

        async with postgres_session_factory() as session:
            agents_with_name = list(await session.scalars(select(Agent).where(Agent.name == name)))
            assert len(agents_with_name) == 1
            assert agents_with_name[0].id == winners[0]
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids={winners[0]})


# --- database CHECK defenses: name --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_name",
    [
        "Agent",  # uppercase
        "-agent",  # leading hyphen
        "agent-",  # trailing hyphen
        "agent--worker",  # doubled hyphen
        "a" * 65,  # over max length
    ],
)
async def test_direct_insert_with_invalid_name_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
    invalid_name: str,
) -> None:
    async with postgres_session_factory() as session:
        session.add(build_agent(name=invalid_name))
        with pytest.raises(IntegrityError):
            await session.flush()


# --- database CHECK defenses: display_name/description/instructions ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_insert_with_display_name_over_max_length_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        session.add(build_agent(name=unique_name("sm801-dn"), display_name="a" * 121))
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_insert_with_description_over_max_length_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        session.add(build_agent(name=unique_name("sm801-desc"), description="a" * 1025))
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_insert_with_instructions_whitespace_only_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        session.add(build_agent(name=unique_name("sm801-instr-blank"), instructions="   \n\t "))
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_insert_with_instructions_over_max_length_is_rejected_by_db(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        session.add(build_agent(name=unique_name("sm801-instr-long"), instructions="a" * 65_537))
        with pytest.raises(IntegrityError):
            await session.flush()
