"""Real-PostgreSQL tests for the SM-804 Agent<->Session association.

Proves ADR-0014 / v0.5.0 Agent Management Feature Contract: `created_at` is
a current-association timestamp, never first-ever-participation (proven by
a real delete-then-recreate producing a strictly later value); the M:N
cardinality (Agent M:N Session, never ownership in either direction); the
archived-Session admission barrier (ADR-0012) is never bypassed by this
association (zero SessionEntry/Query/PipelineRun row-count deltas, and
unchanged Session.updated_at/archived_at); both FKs CASCADE structurally;
zero `graph_outbox` involvement; and the non-attribution invariant -- when
a Session has multiple associated Agents, no persisted row anywhere lets a
caller determine which Agent originated a given Query. Requires migrations
already applied through 0017 against the configured PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text

from sofias_memory.config import load_settings
from sofias_memory.domain import AgentStatus, SessionStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    Agent,
    AgentSession,
    PipelineRun,
    Query,
    Session,
    SessionEntry,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.agent_sessions import AgentSessionService

POSTGRES_AGENT_SESSIONS_ENV = "SOFIAS_MEMORY_RUN_AGENT_SESSIONS_POSTGRES_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_AGENT_SESSIONS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENT_SESSIONS_ENV}=1 to run PostgreSQL AgentSession tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def make_agent(
    session_factory: AsyncSessionFactory, *, status: AgentStatus = AgentStatus.ACTIVE
) -> Agent:
    async with PostgresUnitOfWork(session_factory) as uow:
        agent = Agent(
            id=uuid4(),
            name=unique_name("sm804-agent"),
            display_name=None,
            description=None,
            instructions=None,
            metadata_={},
            status=status,
            created_at=utc_now(),
            updated_at=utc_now(),
            archived_at=None,
        )
        agent = await uow.agents.add(agent)
        await uow.commit()
        return agent


async def make_session(
    session_factory: AsyncSessionFactory, *, status: SessionStatus = SessionStatus.ACTIVE
) -> Session:
    async with session_factory() as session:
        row = Session(
            id=uuid4(),
            key=unique_name("sm804-session"),
            name="SM-804 Session",
            status=status,
            metadata_={},
            created_at=utc_now(),
            updated_at=utc_now(),
            archived_at=utc_now() if status == SessionStatus.ARCHIVED else None,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def cleanup(
    session_factory: AsyncSessionFactory,
    *,
    agent_ids: set[UUID],
    session_ids: set[UUID],
) -> None:
    async with session_factory() as session:
        if agent_ids:
            await session.execute(delete(AgentSession).where(AgentSession.agent_id.in_(agent_ids)))
            await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        if session_ids:
            await session.execute(delete(Query).where(Query.session_id.in_(session_ids)))
            await session.execute(
                delete(PipelineRun).where(PipelineRun.session_id.in_(session_ids))
            )
            await session.execute(
                delete(SessionEntry).where(SessionEntry.session_id.in_(session_ids))
            )
            await session.execute(delete(Session).where(Session.id.in_(session_ids)))
        await session.commit()


def service_for(session_factory: AsyncSessionFactory) -> AgentSessionService:
    return AgentSessionService(session_factory=session_factory)


# --- created_at semantics -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_preserves_created_at(postgres_session_factory: AsyncSessionFactory) -> None:
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        first = await service.associate_session(agent.id, session.id)
        replay = await service.associate_session(agent.id, session.id)

        assert replay.association_created_at == first.association_created_at

        async with postgres_session_factory() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.agent_id == agent.id)
            )
            assert count == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_then_recreate_gets_new_created_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """The concrete proof that created_at != first-ever participation: a
    delete followed by a re-associate produces a strictly later timestamp,
    and no trace of the original created_at survives the delete."""

    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        first = await service.associate_session(agent.id, session.id)

        await service.remove_session(agent.id, session.id)

        async with postgres_session_factory() as db_session:
            row = await db_session.get(AgentSession, (agent.id, session.id))
            assert row is None

        second = await service.associate_session(agent.id, session.id)

        assert second.association_created_at != first.association_created_at
        assert second.association_created_at > first.association_created_at
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_is_idempotent(postgres_session_factory: AsyncSessionFactory) -> None:
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent.id, session.id)

        await service.remove_session(agent.id, session.id)
        await service.remove_session(agent.id, session.id)  # replay, no error

        listed = await service.list_sessions(agent.id)
        assert listed.items == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_missing_session_uuid_is_204_noop(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """No Session pre-check on DELETE: a session_uuid referencing nothing
    at all is simply "association absent", not an error."""

    agent = await make_agent(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.remove_session(agent.id, uuid4())  # must not raise
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids=set())


# --- M:N cardinality ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_n_cardinality_multiple_agents_and_sessions_coexist(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Agent A <-> Session X, Agent A <-> Session Y, Agent B <-> Session X
    coexist as three distinct rows -- Agent M:N Session, never ownership in
    either direction."""

    agent_a = await make_agent(postgres_session_factory)
    agent_b = await make_agent(postgres_session_factory)
    session_x = await make_session(postgres_session_factory)
    session_y = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent_a.id, session_x.id)
        await service.associate_session(agent_a.id, session_y.id)
        await service.associate_session(agent_b.id, session_x.id)

        listed_a = await service.list_sessions(agent_a.id)
        listed_b = await service.list_sessions(agent_b.id)
        assert {item.session_uuid for item in listed_a.items} == {session_x.id, session_y.id}
        assert {item.session_uuid for item in listed_b.items} == {session_x.id}

        async with postgres_session_factory() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.agent_id.in_({agent_a.id, agent_b.id}))
            )
            assert count == 3
    finally:
        await cleanup(
            postgres_session_factory,
            agent_ids={agent_a.id, agent_b.id},
            session_ids={session_x.id, session_y.id},
        )


# --- archived Agent / archived Session -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_agent_allows_full_association_management(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory, status=AgentStatus.ARCHIVED)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)

        put_result = await service.associate_session(agent.id, session.id)
        assert put_result.session_uuid == session.id

        listed = await service.list_sessions(agent.id)
        assert len(listed.items) == 1

        await service.remove_session(agent.id, session.id)
        listed_after_delete = await service.list_sessions(agent.id)
        assert listed_after_delete.items == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_session_put_causes_zero_session_activity(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Direct proof of the ADR-0012 admission barrier NOT being bypassed:
    associating an Agent with an archived Session must not create a
    SessionEntry/Query/PipelineRun, must not unarchive the Session, and
    must not touch Session.updated_at/archived_at."""

    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory, status=SessionStatus.ARCHIVED)
    try:
        async with postgres_session_factory() as db_session:
            before_entries = await db_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.id)
            )
            before_queries = await db_session.scalar(
                select(func.count()).select_from(Query).where(Query.session_id == session.id)
            )
            before_runs = await db_session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.session_id == session.id)
            )
            before_session = await db_session.get(Session, session.id)
            assert before_session is not None
            before_updated_at = before_session.updated_at
            before_archived_at = before_session.archived_at
            before_status = before_session.status

        service = service_for(postgres_session_factory)
        result = await service.associate_session(agent.id, session.id)
        assert result.status == SessionStatus.ARCHIVED

        async with postgres_session_factory() as db_session:
            after_entries = await db_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.id)
            )
            after_queries = await db_session.scalar(
                select(func.count()).select_from(Query).where(Query.session_id == session.id)
            )
            after_runs = await db_session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.session_id == session.id)
            )
            after_session = await db_session.get(Session, session.id)
            assert after_session is not None

        assert after_entries == before_entries == 0
        assert after_queries == before_queries == 0
        assert after_runs == before_runs == 0
        assert after_session.updated_at == before_updated_at
        assert after_session.archived_at == before_archived_at
        assert after_session.status == before_status == SessionStatus.ARCHIVED
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


# --- structural FK CASCADE proofs (bypass the service entirely) ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_delete_cascades_association(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent.id, session.id)

        async with postgres_session_factory() as db_session:
            await db_session.execute(delete(Agent).where(Agent.id == agent.id))
            await db_session.commit()

        async with postgres_session_factory() as db_session:
            row = await db_session.get(AgentSession, (agent.id, session.id))
            assert row is None
            still_there = await db_session.get(Session, session.id)
            assert still_there is not None
    finally:
        await cleanup(postgres_session_factory, agent_ids=set(), session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_delete_cascades_association(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent.id, session.id)

        async with postgres_session_factory() as db_session:
            await db_session.execute(delete(Session).where(Session.id == session.id))
            await db_session.commit()

        async with postgres_session_factory() as db_session:
            row = await db_session.get(AgentSession, (agent.id, session.id))
            assert row is None
            still_there = await db_session.get(Agent, agent.id)
            assert still_there is not None
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids=set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_sessions_table_has_exactly_two_cascade_fks(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT con.conname, con.confdeltype
                FROM pg_catalog.pg_constraint AS con
                JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
                WHERE rel.relname = 'agent_sessions' AND con.contype = 'f'
                """
            )
        )
        fk_rows = {row.conname: row.confdeltype.decode() for row in result}

    assert set(fk_rows) == {
        "fk_agent_sessions_agent_id_agents",
        "fk_agent_sessions_session_id_sessions",
    }
    # 'c' == CASCADE in pg_constraint.confdeltype -- both sides, deliberately.
    assert fk_rows["fk_agent_sessions_agent_id_agents"] == "c"
    assert fk_rows["fk_agent_sessions_session_id_sessions"] == "c"


# --- concurrency ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_put_converges_to_one_row_no_duplication(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)

        async def put(index: int) -> None:
            await service.associate_session(agent.id, session.id)
            del index

        await asyncio.gather(*(put(i) for i in range(5)))

        async with postgres_session_factory() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.agent_id == agent.id)
            )
            assert count == 1

        listed = await service.list_sessions(agent.id)
        assert len(listed.items) == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_put_from_two_agents_produces_two_rows(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent_a = await make_agent(postgres_session_factory)
    agent_b = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)

        await asyncio.gather(
            service.associate_session(agent_a.id, session.id),
            service.associate_session(agent_b.id, session.id),
        )

        async with postgres_session_factory() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.session_id == session.id)
            )
            assert count == 2
    finally:
        await cleanup(
            postgres_session_factory,
            agent_ids={agent_a.id, agent_b.id},
            session_ids={session.id},
        )


# --- graph_outbox non-involvement --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_session_operations_create_zero_graph_outbox_events(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        baseline = await session.scalar(text("SELECT count(*) FROM graph_outbox"))

    agent = await make_agent(postgres_session_factory)
    db_session = await make_session(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent.id, db_session.id)
        await service.list_sessions(agent.id)
        await service.remove_session(agent.id, db_session.id)

        async with postgres_session_factory() as session:
            after = await session.scalar(text("SELECT count(*) FROM graph_outbox"))

        assert after == baseline
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={db_session.id})


# --- non-attribution invariant -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_attribution_invariant_query_cannot_be_traced_to_one_agent(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Non-attribution invariant (ADR-0014): a Session with two associated
    Agents (A and B), and a legitimate Query recorded against that Session
    via the existing ADR-0012 provenance primitive, cannot be traced back to
    exactly one of the two Agents. The persisted Query row carries Session
    identity only -- no Agent identity column exists on it -- and the join
    Query -> Session -> agent_sessions structurally returns both candidate
    Agents with no per-operation discriminator between them. This is a
    property of the schema, not something the system "should" resolve."""

    agent_a = await make_agent(postgres_session_factory)
    agent_b = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    query_id = uuid4()
    try:
        service = service_for(postgres_session_factory)
        await service.associate_session(agent_a.id, session.id)
        await service.associate_session(agent_b.id, session.id)

        # A legitimate Query -> Session provenance row, created through the
        # existing ADR-0012 primitive (QueryRepository), never a new Agent-
        # runtime operation invented for this fixture.
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.queries.add(
                Query(
                    id=query_id,
                    query_text="What does the graph say?",
                    dataset_ids=[],
                    mode="chunks",
                    answer=None,
                    references={},
                    timings={},
                    model=None,
                    session_id=session.id,
                    session_context_entry_ids=[],
                    created_at=utc_now(),
                )
            )
            await uow.commit()

        async with postgres_session_factory() as db_session:
            persisted_query = await db_session.get(Query, query_id)
            assert persisted_query is not None
            # Structural proof: the Query row has Session identity but no
            # Agent identity column of any kind.
            assert not hasattr(persisted_query, "agent_id")
            assert persisted_query.session_id == session.id

            candidate_agents = (
                (
                    await db_session.execute(
                        select(AgentSession.agent_id)
                        .join(Query, Query.session_id == AgentSession.session_id)
                        .where(Query.id == query_id)
                    )
                )
                .scalars()
                .all()
            )

        # Both Agents are structurally returned as equally valid candidates
        # -- there is no discriminator that could single out either one as
        # "the" originator of this Query.
        assert set(candidate_agents) == {agent_a.id, agent_b.id}
    finally:
        async with postgres_session_factory() as db_session:
            await db_session.execute(delete(Query).where(Query.id == query_id))
            await db_session.commit()
        await cleanup(
            postgres_session_factory,
            agent_ids={agent_a.id, agent_b.id},
            session_ids={session.id},
        )
