"""Real-Neo4j SM-805 proof that Agent Management never touches the graph.

Executes a representative matrix of Agent/AgentSkill/AgentSession write and
read operations (create, PATCH, archive, restore, associate/re-pin/unpin/
remove Skill, associate/remove Session) against real PostgreSQL only, then
proves Neo4j's total node/relationship counts are unchanged and that no
Agent-shaped label, relationship type, or identity-bearing property was
ever created -- Agent Management is never projected (ADR-0014). Mirrors
``test_skills_cross_feature_neo4j_integration.py`` (SM-705). Requires
migrations already applied through 0017 and a real, reachable Neo4j
instance.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.config import load_settings
from sofias_memory.domain import SkillRevisionContent
from sofias_memory.infrastructure.neo4j import Neo4jResource, create_neo4j_resource_from_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Agent, Session, Skill
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agent_skills import AgentSkillSetRequest
from sofias_memory.schemas.agents import AgentCreateRequest, AgentUpdateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest
from sofias_memory.services.agent_sessions import AgentSessionService
from sofias_memory.services.agent_skills import AgentSkillService
from sofias_memory.services.agents import AgentService
from sofias_memory.services.sessions import SessionService
from sofias_memory.services.skills import create_skill_aggregate, create_skill_revision

NEO4J_AGENTS_TESTS_ENV = "SOFIAS_MEMORY_RUN_AGENTS_NEO4J_TESTS"
POSTGRES_AGENTS_CROSS_FEATURE_ENV = "SOFIAS_MEMORY_RUN_AGENTS_CROSS_FEATURE_POSTGRES_TESTS"
EMBEDDING_DIMENSIONS = 3072


def _require_real_neo4j() -> None:
    if os.environ.get(NEO4J_AGENTS_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_AGENTS_TESTS_ENV}=1 to run real-Neo4j Agent isolation tests")


def _postgres_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENTS_CROSS_FEATURE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENTS_CROSS_FEATURE_ENV}=1 to run this suite")
    if not env.get("DATABASE_URL", "").strip():
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return env["DATABASE_URL"]


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _postgres_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def cleanup_agents(session_factory: AsyncSessionFactory, agent_ids: set[Any]) -> None:
    if not agent_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        await session.commit()


async def cleanup_skills(session_factory: AsyncSessionFactory, skill_ids: set[Any]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


async def cleanup_sessions(session_factory: AsyncSessionFactory, session_ids: set[Any]) -> None:
    if not session_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Session).where(Session.id.in_(session_ids)))
        await session.commit()


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]


async def total_node_count(resource: Neo4jResource) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n) RETURN count(n) AS count", {}, database_=resource.database
    )
    return int(result_records(result)[0]["count"])


async def total_relationship_count(resource: Neo4jResource) -> int:
    result = await resource.driver.execute_query(
        "MATCH ()-[r]->() RETURN count(r) AS count", {}, database_=resource.database
    )
    return int(result_records(result)[0]["count"])


async def nodes_with_property_value(resource: Neo4jResource, value: str) -> int:
    result = await resource.driver.execute_query(
        "MATCH (n) WHERE any(k IN keys(n) WHERE n[k] = $value) RETURN count(n) AS count",
        {"value": value},
        database_=resource.database,
    )
    return int(result_records(result)[0]["count"])


async def labels_matching_agent(resource: Neo4jResource) -> list[str]:
    result = await resource.driver.execute_query(
        "CALL db.labels() YIELD label WHERE toLower(label) CONTAINS 'agent' RETURN label",
        {},
        database_=resource.database,
    )
    return [str(record["label"]) for record in result_records(result)]


async def relationship_types_matching_agent(resource: Neo4jResource) -> list[str]:
    result = await resource.driver.execute_query(
        "CALL db.relationshipTypes() YIELD relationshipType "
        "WHERE toLower(relationshipType) CONTAINS 'agent' RETURN relationshipType",
        {},
        database_=resource.database,
    )
    return [str(record["relationshipType"]) for record in result_records(result)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_agent_operations_matrix_never_projects_to_neo4j(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    _require_real_neo4j()
    resource = create_neo4j_resource_from_settings(load_settings())

    agent_service = AgentService(session_factory=postgres_session_factory)
    agent_skill_service = AgentSkillService(session_factory=postgres_session_factory)
    agent_session_service = AgentSessionService(session_factory=postgres_session_factory)
    session_service = SessionService(session_factory=postgres_session_factory)

    agent_ids: set[Any] = set()
    skill_ids: set[Any] = set()
    session_ids: set[Any] = set()
    name = unique_name("neo4j-isolation-agent")

    try:
        nodes_before = await total_node_count(resource)
        relationships_before = await total_relationship_count(resource)

        agent = await agent_service.create_agent(AgentCreateRequest(name=name))
        agent_ids.add(agent.agent_uuid)
        agent_id = agent.agent_uuid

        await agent_service.update_agent(agent_id, AgentUpdateRequest(description="D1"))
        await agent_service.archive_agent(agent_id)
        await agent_service.restore_agent(agent_id)
        await agent_service.list_agents(limit=10, offset=0, status=None)  # type: ignore[arg-type]
        await agent_service.get_agent(agent_id)

        fake_embedding = [0.0] * EMBEDDING_DIMENSIONS

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

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            skill = await create_skill_aggregate(
                uow,
                name=unique_name("neo4j-isolation-skill"),
                content=build_content(),
                resolution_embedding=fake_embedding,
            )
            await uow.commit()
        skill_ids.add(skill.id)
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await create_skill_revision(
                uow,
                skill_id=skill.id,
                name=skill.name,
                content=build_content(description="Revision two."),
                resolution_embedding=fake_embedding,
            )
            await uow.commit()

        await agent_skill_service.set_skill(
            agent_id, skill.id, AgentSkillSetRequest(pinned_revision=1)
        )
        await agent_skill_service.set_skill(
            agent_id, skill.id, AgentSkillSetRequest(pinned_revision=2)
        )
        await agent_skill_service.set_skill(agent_id, skill.id, AgentSkillSetRequest())
        await agent_skill_service.list_skills(agent_id)
        await agent_skill_service.remove_skill(agent_id, skill.id)

        session_result = await session_service.create_session(SessionCreateRequest())
        session_ids.add(session_result.session_uuid)
        await agent_session_service.associate_session(agent_id, session_result.session_uuid)
        await agent_session_service.associate_session(agent_id, session_result.session_uuid)
        await agent_session_service.list_sessions(agent_id)
        await agent_session_service.remove_session(agent_id, session_result.session_uuid)

        nodes_after = await total_node_count(resource)
        relationships_after = await total_relationship_count(resource)

        assert nodes_after == nodes_before
        assert relationships_after == relationships_before
        assert await labels_matching_agent(resource) == []
        assert await relationship_types_matching_agent(resource) == []
        assert await nodes_with_property_value(resource, str(agent_id)) == 0
        assert await nodes_with_property_value(resource, name) == 0
        assert await nodes_with_property_value(resource, str(skill.id)) == 0
        assert await nodes_with_property_value(resource, str(session_result.session_uuid)) == 0
    finally:
        await cleanup_agents(postgres_session_factory, agent_ids)
        await cleanup_skills(postgres_session_factory, skill_ids)
        await cleanup_sessions(postgres_session_factory, session_ids)
        await resource.close()
