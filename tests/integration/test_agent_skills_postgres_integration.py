"""Real-PostgreSQL tests for the SM-803 Agent<->Skill association.

Proves ADR-0014 / v0.5.0 Agent Management Feature Contract: the composite
FK's structural guarantees (cross-Skill pin rejection, pinned-revision
delete rejection -- both proven by direct SQL/ORM operations that bypass
the service layer entirely, never only by application-level validation),
follow-current vs pinned semantics against a real rollback, re-pin/unpin/
replay `created_at` preservation, archived Agent/Skill interaction, real
concurrent PUT convergence, and zero `graph_outbox` involvement. Requires
migrations already applied through 0016 against the configured PostgreSQL
database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from sofias_memory.config import load_settings
from sofias_memory.domain import AgentStatus, SkillRevisionContent, SkillStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Agent, AgentSkill, Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.agent_skills import AgentSkillSetRequest
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.agent_skills import AgentSkillService
from sofias_memory.services.skills import create_skill_aggregate, create_skill_revision

POSTGRES_AGENT_SKILLS_ENV = "SOFIAS_MEMORY_RUN_AGENT_SKILLS_POSTGRES_TESTS"

FAKE_EMBEDDING = [0.0] * 3072


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_AGENT_SKILLS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENT_SKILLS_ENV}=1 to run PostgreSQL AgentSkill tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def build_content(**overrides: object) -> SkillRevisionContent:
    base: dict[str, object] = {
        "description": "A description.",
        "procedure": "Do the thing.",
        "license": None,
        "compatibility": None,
        "metadata": {},
        "tags": [],
        "declared_tools": [],
    }
    base.update(overrides)
    return SkillRevisionContent(**base)  # type: ignore[arg-type]


async def make_agent(
    session_factory: AsyncSessionFactory, *, status: AgentStatus = AgentStatus.ACTIVE
) -> Agent:
    async with PostgresUnitOfWork(session_factory) as uow:
        agent = Agent(
            id=uuid4(),
            name=unique_name("sm803-agent"),
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


async def make_skill(session_factory: AsyncSessionFactory) -> Skill:
    async with PostgresUnitOfWork(session_factory) as uow:
        skill = await create_skill_aggregate(
            uow,
            name=unique_name("sm803-skill"),
            content=build_content(),
            resolution_embedding=FAKE_EMBEDDING,
        )
        await uow.commit()
        return skill


async def add_revision(
    session_factory: AsyncSessionFactory, skill: Skill, *, description: str
) -> None:
    async with PostgresUnitOfWork(session_factory) as uow:
        await create_skill_revision(
            uow,
            skill_id=skill.id,
            name=skill.name,
            content=build_content(description=description),
            resolution_embedding=FAKE_EMBEDDING,
        )
        await uow.commit()


async def set_current_revision(
    session_factory: AsyncSessionFactory, skill_id: UUID, revision_id: UUID
) -> None:
    async with session_factory() as session:
        skill = await session.get(Skill, skill_id)
        assert skill is not None
        skill.current_revision_id = revision_id
        skill.updated_at = utc_now()
        await session.commit()


async def cleanup(
    session_factory: AsyncSessionFactory,
    *,
    agent_ids: set[UUID],
    skill_ids: set[UUID],
) -> None:
    async with session_factory() as session:
        if agent_ids:
            await session.execute(delete(AgentSkill).where(AgentSkill.agent_id.in_(agent_ids)))
            await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        if skill_ids:
            await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


def service_for(session_factory: AsyncSessionFactory) -> AgentSkillService:
    return AgentSkillService(session_factory=session_factory)


# --- follow-current / pinned behavior ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_follow_current_reflects_rollback_without_mutating_association(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        first = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())
        assert first.pinned_revision is None
        assert first.current_revision == 1
        assert first.effective_revision == 1

        await add_revision(postgres_session_factory, skill, description="Revision two.")

        listed = await service.list_skills(agent.id)
        item = listed.items[0]
        assert item.pinned_revision is None
        assert item.current_revision == 2
        assert item.effective_revision == 2
        assert item.association_created_at == first.association_created_at
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pin_survives_current_revision_changes(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        await add_revision(postgres_session_factory, skill, description="Revision two.")
        service = service_for(postgres_session_factory)

        pinned = await service.set_skill(
            agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1)
        )
        assert pinned.pinned_revision == 1
        assert pinned.current_revision == 2
        assert pinned.effective_revision == 1

        await add_revision(postgres_session_factory, skill, description="Revision three.")

        listed = await service.list_skills(agent.id)
        item = listed.items[0]
        assert item.pinned_revision == 1
        assert item.current_revision == 3
        assert item.effective_revision == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_effective_revision_uses_current_revision_id_never_max_revision(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Skill has revisions 1, 2, 3; current_revision_id is rolled back to
    point at revision 1. Follow-current must report current_revision=1, not
    MAX(revision)=3."""

    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        await add_revision(postgres_session_factory, skill, description="Revision two.")
        await add_revision(postgres_session_factory, skill, description="Revision three.")

        async with postgres_session_factory() as session:
            revision_1 = (
                await session.execute(
                    select(SkillRevision).where(
                        SkillRevision.skill_id == skill.id, SkillRevision.revision == 1
                    )
                )
            ).scalar_one()
        await set_current_revision(postgres_session_factory, skill.id, revision_1.id)

        service = service_for(postgres_session_factory)
        result = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

        assert result.current_revision == 1
        assert result.effective_revision == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- re-pin / unpin / replay / created_at -----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_re_pin_unpin_and_replay_preserve_created_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        await add_revision(postgres_session_factory, skill, description="Revision two.")
        service = service_for(postgres_session_factory)

        first = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
        re_pinned = await service.set_skill(
            agent.id, skill.id, AgentSkillSetRequest(pinned_revision=2)
        )
        assert re_pinned.pinned_revision == 2
        assert re_pinned.association_created_at == first.association_created_at

        replay = await service.set_skill(
            agent.id, skill.id, AgentSkillSetRequest(pinned_revision=2)
        )
        assert replay.pinned_revision == 2
        assert replay.association_created_at == first.association_created_at

        unpinned = await service.set_skill(
            agent.id, skill.id, AgentSkillSetRequest(pinned_revision=None)
        )
        assert unpinned.pinned_revision is None
        assert unpinned.effective_revision == unpinned.current_revision
        assert unpinned.association_created_at == first.association_created_at

        async with postgres_session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(AgentSkill).where(AgentSkill.agent_id == agent.id)
            )
            assert count == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_then_recreate_gets_new_created_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        first = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

        await service.remove_skill(agent.id, skill.id)
        second = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

        assert second.association_created_at != first.association_created_at
        assert second.association_created_at > first.association_created_at
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_is_idempotent(postgres_session_factory: AsyncSessionFactory) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

        await service.remove_skill(agent.id, skill.id)
        await service.remove_skill(agent.id, skill.id)  # replay, no error

        listed = await service.list_skills(agent.id)
        assert listed.items == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- archived Agent / archived Skill -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_archive_preserves_association_and_visibility(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())

        async with postgres_session_factory() as session:
            persisted_skill = await session.get(Skill, skill.id)
            assert persisted_skill is not None
            persisted_skill.status = SkillStatus.ARCHIVED
            persisted_skill.archived_at = utc_now()
            await session.commit()

        listed = await service.list_skills(agent.id)
        assert len(listed.items) == 1
        assert listed.items[0].status == SkillStatus.ARCHIVED
        assert listed.items[0].effective_revision == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_archive_allows_full_association_management(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory, status=AgentStatus.ARCHIVED)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)

        put_result = await service.set_skill(agent.id, skill.id, AgentSkillSetRequest())
        assert put_result.skill_uuid == skill.id

        listed = await service.list_skills(agent.id)
        assert len(listed.items) == 1

        await service.remove_skill(agent.id, skill.id)
        listed_after_delete = await service.list_skills(agent.id)
        assert listed_after_delete.items == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- structural FK proofs (bypass the service entirely) ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_skill_pinned_revision_is_rejected_by_composite_fk(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Direct ORM insert, bypassing AgentSkillService entirely: a revision
    belonging to Skill B can never be pinned on an association targeting
    Skill A -- the database itself must reject it."""

    agent = await make_agent(postgres_session_factory)
    skill_a = await make_skill(postgres_session_factory)
    skill_b = await make_skill(postgres_session_factory)
    try:
        async with postgres_session_factory() as session:
            skill_b_persisted = await session.get(Skill, skill_b.id)
            assert skill_b_persisted is not None
            revision_of_b = skill_b_persisted.current_revision_id

            session.add(
                AgentSkill(
                    agent_id=agent.id,
                    skill_id=skill_a.id,
                    pinned_revision_id=revision_of_b,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await cleanup(
            postgres_session_factory,
            agent_ids={agent.id},
            skill_ids={skill_a.id, skill_b.id},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleting_a_pinned_non_current_revision_is_rejected_by_fk(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Pin a HISTORICAL, non-current revision (never the one
    Skill.current_revision_id itself points at), then attempt a direct
    ORM/SQL delete of that exact revision row. Rejection here proves the
    NEW agent_skills composite FK is the protector -- not the pre-existing
    skills.current_revision_id FK, which would not be involved for a
    non-current revision (Feature Contract SS 53)."""

    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        await add_revision(postgres_session_factory, skill, description="Revision two.")

        async with postgres_session_factory() as session:
            revision_1 = (
                await session.execute(
                    select(SkillRevision).where(
                        SkillRevision.skill_id == skill.id, SkillRevision.revision == 1
                    )
                )
            ).scalar_one()
            revision_1_id = revision_1.id

        service = service_for(postgres_session_factory)
        pinned = await service.set_skill(
            agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1)
        )
        assert pinned.pinned_revision == 1

        # Confirm revision 1 is NOT the current revision (current is 2) --
        # this is what makes the proof specific to the new agent_skills FK.
        async with postgres_session_factory() as session:
            persisted_skill = await session.get(Skill, skill.id)
            assert persisted_skill is not None
            assert persisted_skill.current_revision_id != revision_1_id

        async with postgres_session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(SkillRevision).where(SkillRevision.id == revision_1_id)
                )
                await session.flush()

        # Pin is still intact -- never silently became NULL.
        async with postgres_session_factory() as session:
            row = await session.get(AgentSkill, (agent.id, skill.id))
            assert row is not None
            assert row.pinned_revision_id == revision_1_id
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_skills_table_has_no_standalone_pinned_revision_fk(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT con.conname, array_length(con.conkey, 1) AS column_count
                FROM pg_catalog.pg_constraint AS con
                JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
                WHERE rel.relname = 'agent_skills' AND con.contype = 'f'
                """
            )
        )
        fk_rows = {row.conname: row.column_count for row in result}

    assert set(fk_rows) == {
        "fk_agent_skills_agent_id_agents",
        "fk_agent_skills_skill_id_skills",
        "fk_agent_skills_skill_id_pinned_revision_id_skill_revisions",
    }
    assert fk_rows["fk_agent_skills_agent_id_agents"] == 1
    assert fk_rows["fk_agent_skills_skill_id_skills"] == 1
    assert fk_rows["fk_agent_skills_skill_id_pinned_revision_id_skill_revisions"] == 2


# --- concurrency --------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_put_converges_to_one_row_no_duplication(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)

        async def put(index: int) -> None:
            await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
            del index

        await asyncio.gather(*(put(i) for i in range(5)))

        async with postgres_session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(AgentSkill).where(AgentSkill.agent_id == agent.id)
            )
            assert count == 1

        listed = await service.list_skills(agent.id)
        assert len(listed.items) == 1
        assert listed.items[0].pinned_revision == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- graph_outbox non-involvement --------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_skill_operations_create_zero_graph_outbox_events(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        baseline = await session.scalar(text("SELECT count(*) FROM graph_outbox"))

    agent = await make_agent(postgres_session_factory)
    skill = await make_skill(postgres_session_factory)
    try:
        service = service_for(postgres_session_factory)
        await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=1))
        await service.set_skill(agent.id, skill.id, AgentSkillSetRequest(pinned_revision=None))
        await service.remove_skill(agent.id, skill.id)

        async with postgres_session_factory() as session:
            after = await session.scalar(text("SELECT count(*) FROM graph_outbox"))

        assert after == baseline
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})
