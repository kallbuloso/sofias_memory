"""Real-HTTP, real-PostgreSQL proof of the SM-804 Agent<->Session
association API's wire contract, admission-barrier non-bypass, and
concurrency behavior.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database -- no embedding
provider, no Neo4j, no fake anywhere in the request path. Requires
migrations already applied through 0017. Service/repository-level behavior
(FK structural proofs, M:N cardinality, `created_at` semantics, non-
attribution) is already proven in
``test_agent_sessions_postgres_integration.py``; this file proves the wire
contract itself -- status codes, envelope shapes, and concurrency observed
through the actual HTTP surface -- mirroring
``test_agent_skills_http_postgres_integration.py``.
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
from sqlalchemy import delete, func, select, text

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
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
from tests.unit._app_factory import create_app

POSTGRES_AGENT_SESSIONS_HTTP_ENV = "SOFIAS_MEMORY_RUN_AGENT_SESSIONS_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def _agent_sessions_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENT_SESSIONS_HTTP_ENV) != "1":
        pytest.skip(
            f"set {POSTGRES_AGENT_SESSIONS_HTTP_ENV}=1 to run AgentSession HTTP PostgreSQL tests"
        )
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _agent_sessions_http_test_database_url(dict(os.environ))

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


async def make_agent(
    session_factory: AsyncSessionFactory, *, status: AgentStatus = AgentStatus.ACTIVE
) -> Agent:
    async with PostgresUnitOfWork(session_factory) as uow:
        agent = Agent(
            id=uuid4(),
            name=unique_name("sm804-http-agent"),
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
            key=unique_name("sm804-http-session"),
            name="SM-804 HTTP Session",
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
    agent_ids: set[Any],
    session_ids: set[Any],
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


def test_agent_sessions_http_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _agent_sessions_http_test_database_url({})


# --- wire semantics -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_association_returns_200(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            response = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}",
                headers=HEADERS,
            )
            assert response.status_code == 200
            body = response.json()["data"]
            assert set(body) == {
                "session_uuid",
                "session_id",
                "name",
                "status",
                "association_created_at",
            }
            assert body["session_uuid"] == str(session.id)
            assert body["session_id"] == session.key
            assert body["status"] == "active"
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_replay_returns_200_unchanged_created_at(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            first = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            replay = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert first.status_code == 200
            assert replay.status_code == 200
            assert (
                replay.json()["data"]["association_created_at"]
                == first.json()["data"]["association_created_at"]
            )
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_list_returns_200(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            await client.put(f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS)
            response = await client.get(f"/api/v1/agents/{agent.id}/sessions", headers=HEADERS)
            assert response.status_code == 200
            body = response.json()["data"]
            assert "items" in body
            assert len(body["items"]) == 1
            assert body["items"][0]["session_uuid"] == str(session.id)
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_existing_and_replay_both_return_204_with_empty_body(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            await client.put(f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS)

            first_delete = await client.delete(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert first_delete.status_code == 204
            assert first_delete.content == b""

            replay_delete = await client.delete(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert replay_delete.status_code == 204
            assert replay_delete.content == b""

            listed = await client.get(f"/api/v1/agents/{agent.id}/sessions", headers=HEADERS)
            assert listed.json()["data"]["items"] == []
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_missing_agent_returns_404(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            response = await client.put(
                f"/api/v1/agents/{uuid4()}/sessions/{session.id}", headers=HEADERS
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup(postgres_session_factory, agent_ids=set(), session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_missing_session_returns_404(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    try:
        async with build_client(app) as client:
            response = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{uuid4()}", headers=HEADERS
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids=set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_missing_agent_returns_404(postgres_session_factory: AsyncSessionFactory) -> None:
    app = build_app(postgres_session_factory)
    async with build_client(app) as client:
        response = await client.get(f"/api/v1/agents/{uuid4()}/sessions", headers=HEADERS)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_missing_agent_returns_404(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    async with build_client(app) as client:
        response = await client.delete(
            f"/api/v1/agents/{uuid4()}/sessions/{uuid4()}", headers=HEADERS
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_agent_put_get_delete_all_succeed(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory, status=AgentStatus.ARCHIVED)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            put_response = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert put_response.status_code == 200

            get_response = await client.get(f"/api/v1/agents/{agent.id}/sessions", headers=HEADERS)
            assert get_response.status_code == 200
            assert len(get_response.json()["data"]["items"]) == 1

            delete_response = await client.delete(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert delete_response.status_code == 204
            assert delete_response.content == b""
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


# --- archived Session admission-barrier non-bypass wire proof ------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_session_put_returns_200_with_zero_activity(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Direct wire-level proof that PUT-ing an Agent onto an archived
    Session succeeds (200), stays visible via GET, and creates zero
    SessionEntry/Query/PipelineRun rows and zero Session timestamp churn --
    the ADR-0012 admission barrier is never bypassed by this management
    association."""

    app = build_app(postgres_session_factory)
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
            before_row = await db_session.get(Session, session.id)
            assert before_row is not None
            before_updated_at = before_row.updated_at
            before_archived_at = before_row.archived_at

        async with build_client(app) as client:
            put_response = await client.put(
                f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS
            )
            assert put_response.status_code == 200
            assert put_response.json()["data"]["status"] == "archived"

            get_response = await client.get(f"/api/v1/agents/{agent.id}/sessions", headers=HEADERS)
            assert get_response.status_code == 200
            items = get_response.json()["data"]["items"]
            assert len(items) == 1
            assert items[0]["status"] == "archived"

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
            after_row = await db_session.get(Session, session.id)
            assert after_row is not None

        assert after_entries == before_entries == 0
        assert after_queries == before_queries == 0
        assert after_runs == before_runs == 0
        assert after_row.updated_at == before_updated_at
        assert after_row.archived_at == before_archived_at
        assert after_row.status == SessionStatus.ARCHIVED
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


# --- graph_outbox non-involvement, via HTTP ------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_agent_session_operations_create_zero_graph_outbox_events(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as db_session:
        baseline = await db_session.scalar(text("SELECT count(*) FROM graph_outbox"))

    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            await client.put(f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS)
            await client.get(f"/api/v1/agents/{agent.id}/sessions", headers=HEADERS)
            await client.delete(f"/api/v1/agents/{agent.id}/sessions/{session.id}", headers=HEADERS)

        async with postgres_session_factory() as db_session:
            after = await db_session.scalar(text("SELECT count(*) FROM graph_outbox"))

        assert after == baseline
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


# --- concurrency: identical payload (no body at all) ---------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_put_via_http_converges_to_one_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[
                    client.put(
                        f"/api/v1/agents/{agent.id}/sessions/{session.id}",
                        headers=HEADERS,
                    )
                    for _ in range(5)
                ]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(200) == 5
        assert statuses.count(409) == 0
        assert statuses.count(500) == 0

        created_ats = {response.json()["data"]["association_created_at"] for response in responses}
        assert len(created_ats) == 1  # same created_at disclosed by every response

        async with postgres_session_factory() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.agent_id == agent.id, AgentSession.session_id == session.id)
            )
            assert count == 1
            row = await db_session.get(AgentSession, (agent.id, session.id))
            assert row is not None
    finally:
        await cleanup(postgres_session_factory, agent_ids={agent.id}, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_put_from_two_agents_via_http_produces_two_rows(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_a = await make_agent(postgres_session_factory)
    agent_b = await make_agent(postgres_session_factory)
    session = await make_session(postgres_session_factory)
    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                client.put(f"/api/v1/agents/{agent_a.id}/sessions/{session.id}", headers=HEADERS),
                client.put(f"/api/v1/agents/{agent_b.id}/sessions/{session.id}", headers=HEADERS),
            )

        assert [response.status_code for response in responses] == [200, 200]

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
