"""Real-HTTP, real-PostgreSQL proof of the SM-803 Agent<->Skill association
API's wire contract and concurrency behavior.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database -- no embedding
provider, no Neo4j, no fake anywhere in the request path. Requires
migrations already applied through 0016. Service/repository-level behavior
(FK structural proofs, follow-current/pin semantics, `created_at`
preservation) is already proven in
``test_agent_skills_postgres_integration.py``; this file proves the wire
contract itself -- status codes, envelope shapes, and concurrency observed
through the actual HTTP surface -- mirroring
``test_agents_management_postgres_integration.py``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import AgentStatus, SkillRevisionContent
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Agent, AgentSkill, Skill
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.common import utc_now
from sofias_memory.services.skills import create_skill_aggregate, create_skill_revision
from tests.unit._app_factory import create_app

POSTGRES_AGENT_SKILLS_HTTP_ENV = "SOFIAS_MEMORY_RUN_AGENT_SKILLS_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}
FAKE_EMBEDDING = [0.0] * 3072


def _agent_skills_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENT_SKILLS_HTTP_ENV) != "1":
        pytest.skip(
            f"set {POSTGRES_AGENT_SKILLS_HTTP_ENV}=1 to run AgentSkill HTTP PostgreSQL tests"
        )
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _agent_skills_http_test_database_url(dict(os.environ))

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=database_url,
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        llm_api_key=LLM_API_KEY,
        app_env="test",
    )
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        llm_api_key=LLM_API_KEY,
        app_env="test",
    )


def build_app(session_factory: AsyncSessionFactory) -> Any:
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
    )


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


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
            name=unique_name("sm803-http-agent"),
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


async def make_skill_with_two_revisions(session_factory: AsyncSessionFactory) -> Skill:
    """Revision 1 is created atomically with the Skill; revision 2 is added
    (and becomes current) immediately after -- giving every test two
    distinct valid `pinned_revision` targets."""

    async with PostgresUnitOfWork(session_factory) as uow:
        skill = await create_skill_aggregate(
            uow,
            name=unique_name("sm803-http-skill"),
            content=build_content(),
            resolution_embedding=FAKE_EMBEDDING,
        )
        await uow.commit()

    async with PostgresUnitOfWork(session_factory) as uow:
        await create_skill_revision(
            uow,
            skill_id=skill.id,
            name=skill.name,
            content=build_content(description="Revision two."),
            resolution_embedding=FAKE_EMBEDDING,
        )
        await uow.commit()

    return skill


async def cleanup(
    session_factory: AsyncSessionFactory,
    *,
    agent_ids: set[Any],
    skill_ids: set[Any],
) -> None:
    async with session_factory() as session:
        if agent_ids:
            await session.execute(delete(AgentSkill).where(AgentSkill.agent_id.in_(agent_ids)))
            await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        if skill_ids:
            await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
        await session.commit()


def test_agent_skills_http_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _agent_skills_http_test_database_url({})


# --- wire semantics -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_valid_pin_returns_200(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            response = await client.put(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                json={"pinned_revision": 1},
                headers=HEADERS,
            )
            assert response.status_code == 200
            body = response.json()["data"]
            assert body["skill_uuid"] == str(skill.id)
            assert body["pinned_revision"] == 1
            assert body["current_revision"] == 2
            assert body["effective_revision"] == 1
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_invalid_target_revision_returns_422(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            response = await client.put(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                json={"pinned_revision": 99},
                headers=HEADERS,
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_list_returns_200(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            await client.put(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                json={"pinned_revision": None},
                headers=HEADERS,
            )
            response = await client.get(f"/api/v1/agents/{agent.id}/skills", headers=HEADERS)
            assert response.status_code == 200
            body = response.json()["data"]
            assert "items" in body
            assert len(body["items"]) == 1
            assert body["items"][0]["skill_uuid"] == str(skill.id)
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_existing_and_replay_both_return_204_with_empty_body(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            await client.put(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                json={},
                headers=HEADERS,
            )

            first_delete = await client.delete(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}", headers=HEADERS
            )
            assert first_delete.status_code == 204
            assert first_delete.content == b""

            replay_delete = await client.delete(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}", headers=HEADERS
            )
            assert replay_delete.status_code == 204
            assert replay_delete.content == b""

            listed = await client.get(f"/api/v1/agents/{agent.id}/skills", headers=HEADERS)
            assert listed.json()["data"]["items"] == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_agent_put_get_delete_all_succeed(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory, status=AgentStatus.ARCHIVED)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            put_response = await client.put(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                json={"pinned_revision": 1},
                headers=HEADERS,
            )
            assert put_response.status_code == 200

            get_response = await client.get(f"/api/v1/agents/{agent.id}/skills", headers=HEADERS)
            assert get_response.status_code == 200
            assert len(get_response.json()["data"]["items"]) == 1

            delete_response = await client.delete(
                f"/api/v1/agents/{agent.id}/skills/{skill.id}", headers=HEADERS
            )
            assert delete_response.status_code == 204
            assert delete_response.content == b""
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- concurrency: identical payload -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_put_via_http_converges_to_one_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[
                    client.put(
                        f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                        json={"pinned_revision": 1},
                        headers=HEADERS,
                    )
                    for _ in range(5)
                ]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(200) == 5
        assert statuses.count(409) == 0
        assert statuses.count(500) == 0
        assert all(status not in (409, 500) for status in statuses if status != 200)

        async with postgres_session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AgentSkill)
                .where(AgentSkill.agent_id == agent.id, AgentSkill.skill_id == skill.id)
            )
            assert count == 1
            row = await session.get(AgentSkill, (agent.id, skill.id))
            assert row is not None
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})


# --- concurrency: different valid payloads ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_different_valid_put_via_http_converges_to_one_valid_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """N=5 concurrent PUT requests through the real HTTP surface, alternating
    between two distinct valid payloads (pinned_revision=1 and =2) against
    the same Agent/Skill pair. Expected: every request succeeds (200), zero
    409, zero 500, exactly one `agent_skills` row survives, and its
    `pinned_revision` is one of the two values actually sent -- HTTP arrival
    order is never asserted as the determinant of the final state, only
    that the final state is a member of the successfully-requested set."""

    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    skill = await make_skill_with_two_revisions(postgres_session_factory)
    try:
        payloads = [{"pinned_revision": 1 if i % 2 == 0 else 2} for i in range(5)]

        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[
                    client.put(
                        f"/api/v1/agents/{agent.id}/skills/{skill.id}",
                        json=payload,
                        headers=HEADERS,
                    )
                    for payload in payloads
                ]
            )

        statuses = [response.status_code for response in responses]
        other_statuses = [status for status in statuses if status != 200]

        assert statuses.count(200) == 5
        assert statuses.count(409) == 0
        assert statuses.count(500) == 0
        assert other_statuses == []

        async with postgres_session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AgentSkill)
                .where(AgentSkill.agent_id == agent.id, AgentSkill.skill_id == skill.id)
            )
            assert count == 1

            row = await session.get(AgentSkill, (agent.id, skill.id))
            assert row is not None

        async with postgres_session_factory() as session:
            skill_revisions = await session.execute(
                select(func.count()).select_from(Skill).where(Skill.id == skill.id)
            )
            assert skill_revisions.scalar_one() == 1  # sanity: Skill itself untouched

        # Final public pinned_revision (via the HTTP GET surface) must equal
        # one of the two values actually sent -- 1 or 2, never anything else.
        async with build_client(app) as client:
            listed = await client.get(f"/api/v1/agents/{agent.id}/skills", headers=HEADERS)
            item = listed.json()["data"]["items"][0]
            assert item["pinned_revision"] in (1, 2)
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, skill_ids={skill.id})
