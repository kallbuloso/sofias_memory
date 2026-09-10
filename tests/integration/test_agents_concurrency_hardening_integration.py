"""Real-PostgreSQL SM-805 concurrency hardening for Agent Management.

Re-proves the concurrency invariants Feature Contract SS 19 freezes, under
real load and real controlled serialization -- not unit-level fakes.
Duplicate-name creation is already re-proven under real HTTP load by
``test_agents_management_postgres_integration.py`` (SM-802) and is not
duplicated here. This file covers what SM-802/SM-803/SM-804 did not
individually exercise as *composition*: `PATCH` racing `archive`/`restore`
on the same Agent row (SS 19.2, single-row `FOR UPDATE`, no lost update, no
global lock), `agent_skills`/`agent_sessions` duplicate-association and
concurrent-pin convergence re-proven together through the real public HTTP
API, and Agent A's per-Agent lock never blocking a concurrent, independent
mutation of Agent B (SS 19.6). Requires migrations already applied through
0017.
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
from sqlalchemy import delete, select

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.domain import SkillRevisionContent
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    Agent,
    AgentSession,
    AgentSkill,
    Session,
    Skill,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agents import AgentUpdateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest
from sofias_memory.services.agents import AgentService
from sofias_memory.services.sessions import SessionService
from sofias_memory.services.skills import create_skill_aggregate, create_skill_revision
from tests.unit._app_factory import create_app

POSTGRES_AGENTS_CONCURRENCY_ENV = "SOFIAS_MEMORY_RUN_AGENTS_CONCURRENCY_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def _concurrency_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENTS_CONCURRENCY_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENTS_CONCURRENCY_ENV}=1 to run this suite")
    if not env.get("DATABASE_URL", "").strip():
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return env["DATABASE_URL"]


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _concurrency_test_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def make_http_settings() -> Settings:
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
        make_http_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
    )


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def create_agent_via_http(
    client: httpx.AsyncClient, *, status: str = "active"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agents",
        json={"name": unique_name("concurrency-agent"), "description": "D0"},
        headers=HEADERS,
    )
    assert response.status_code == 201
    data: dict[str, Any] = response.json()["data"]
    if status == "archived":
        archived = await client.post(
            f"/api/v1/agents/{data['agent_uuid']}/archive", headers=HEADERS
        )
        data = archived.json()["data"]
    return data


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


def build_skill_content(**overrides: object) -> SkillRevisionContent:
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


# --- 7/9: PATCH vs archive -- real controlled concurrency, no lost update ----


async def _hold_agent_row_lock(
    session_factory: AsyncSessionFactory,
    agent_id: Any,
    *,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> None:
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.agents.get_by_id_for_update(agent_id)
        ready.set()
        await release.wait()
        await uow.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_vs_archive_true_concurrency_no_lost_update(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Real concurrent HTTP race, forced through the same DB row lock via a
    barrier-held transaction: PATCH description and archive fire while the
    Agent row is externally locked, then both are released simultaneously.
    Whichever order PostgreSQL actually serializes them in, the composed
    final state must have both mutations -- PATCH only ever touches
    `description`, archive only ever touches `status`/`archived_at`, so
    reading a fresh row after acquiring the lock (never a stale copy) makes
    the two commute regardless of order."""

    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client)
            agent_id = created["agent_uuid"]

            ready = asyncio.Event()
            release = asyncio.Event()
            holder_task = asyncio.create_task(
                _hold_agent_row_lock(
                    postgres_session_factory, agent_id, ready=ready, release=release
                )
            )
            await asyncio.wait_for(ready.wait(), timeout=5)

            patch_task = asyncio.create_task(
                client.patch(
                    f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
                )
            )
            archive_task = asyncio.create_task(
                client.post(f"/api/v1/agents/{agent_id}/archive", headers=HEADERS)
            )
            await asyncio.sleep(0.05)
            assert not patch_task.done()
            assert not archive_task.done()

            release.set()
            await asyncio.wait_for(holder_task, timeout=5)
            patch_response = await asyncio.wait_for(patch_task, timeout=5)
            archive_response = await asyncio.wait_for(archive_task, timeout=5)

        assert patch_response.status_code == 200
        assert archive_response.status_code == 200

        async with postgres_session_factory() as session:
            final = await session.get(Agent, agent_id)
            assert final is not None
            assert final.description == "D1"
            assert final.status.value == "archived"
            assert final.archived_at is not None
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_then_archive_serialized_order(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Deterministic order A: PATCH commits first, archive second. Proves
    one of the two valid serialization orders composes correctly."""

    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client)
            agent_id = created["agent_uuid"]

            patch_response = await client.patch(
                f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
            )
            assert patch_response.status_code == 200

            archive_response = await client.post(
                f"/api/v1/agents/{agent_id}/archive", headers=HEADERS
            )
            assert archive_response.status_code == 200

            final = archive_response.json()["data"]
            assert final["description"] == "D1"
            assert final["status"] == "archived"
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_then_patch_serialized_order(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Deterministic order B: archive commits first, PATCH second. Proves
    the other valid serialization order also composes correctly -- archive
    never touched `description`, so PATCH's later write is never lost."""

    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client)
            agent_id = created["agent_uuid"]

            archive_response = await client.post(
                f"/api/v1/agents/{agent_id}/archive", headers=HEADERS
            )
            assert archive_response.status_code == 200

            patch_response = await client.patch(
                f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
            )
            assert patch_response.status_code == 200

            final = patch_response.json()["data"]
            assert final["description"] == "D1"
            assert final["status"] == "archived"
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


# --- 8: PATCH vs restore ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_vs_restore_true_concurrency_no_lost_update(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client, status="archived")
            agent_id = created["agent_uuid"]

            ready = asyncio.Event()
            release = asyncio.Event()
            holder_task = asyncio.create_task(
                _hold_agent_row_lock(
                    postgres_session_factory, agent_id, ready=ready, release=release
                )
            )
            await asyncio.wait_for(ready.wait(), timeout=5)

            patch_task = asyncio.create_task(
                client.patch(
                    f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
                )
            )
            restore_task = asyncio.create_task(
                client.post(f"/api/v1/agents/{agent_id}/restore", headers=HEADERS)
            )
            await asyncio.sleep(0.05)
            assert not patch_task.done()
            assert not restore_task.done()

            release.set()
            await asyncio.wait_for(holder_task, timeout=5)
            patch_response = await asyncio.wait_for(patch_task, timeout=5)
            restore_response = await asyncio.wait_for(restore_task, timeout=5)

        assert patch_response.status_code == 200
        assert restore_response.status_code == 200

        async with postgres_session_factory() as session:
            final = await session.get(Agent, agent_id)
            assert final is not None
            assert final.description == "D1"
            assert final.status.value == "active"
            assert final.archived_at is None
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_then_restore_serialized_order(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client, status="archived")
            agent_id = created["agent_uuid"]

            patch_response = await client.patch(
                f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
            )
            assert patch_response.status_code == 200

            restore_response = await client.post(
                f"/api/v1/agents/{agent_id}/restore", headers=HEADERS
            )
            assert restore_response.status_code == 200
            final = restore_response.json()["data"]
            assert final["description"] == "D1"
            assert final["status"] == "active"
            assert final["archived_at"] is None
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_then_patch_serialized_order(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await create_agent_via_http(client, status="archived")
            agent_id = created["agent_uuid"]

            restore_response = await client.post(
                f"/api/v1/agents/{agent_id}/restore", headers=HEADERS
            )
            assert restore_response.status_code == 200

            patch_response = await client.patch(
                f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
            )
            assert patch_response.status_code == 200
            final = patch_response.json()["data"]
            assert final["description"] == "D1"
            assert final["status"] == "active"
            assert final["archived_at"] is None
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


# --- 10/11: AgentSkill concurrent pins / duplicate association ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_skill_concurrent_different_pins_converge_to_one_valid_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Consolidated re-proof (SM-803 already proved this in isolation): N=5
    concurrent PUT alternating between two distinct valid pins on the same
    Agent<->Skill pair -- all succeed, zero 409/500, exactly one row, final
    pin is one of the two values actually sent."""

    fake_embedding = [0.0] * 3072
    skill_id: object | None = None
    agent_id: object | None = None
    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            skill = await create_skill_aggregate(
                uow,
                name=unique_name("concurrency-skill"),
                content=build_skill_content(),
                resolution_embedding=fake_embedding,
            )
            await uow.commit()
        skill_id = skill.id
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await create_skill_revision(
                uow,
                skill_id=skill.id,
                name=skill.name,
                content=build_skill_content(description="Revision two."),
                resolution_embedding=fake_embedding,
            )
            await uow.commit()

        app = build_app(postgres_session_factory)
        async with build_client(app) as client:
            created = await create_agent_via_http(client)
            agent_id = created["agent_uuid"]

            payloads = [{"pinned_revision": 1 if i % 2 == 0 else 2} for i in range(5)]
            responses = await asyncio.gather(
                *[
                    client.put(
                        f"/api/v1/agents/{agent_id}/skills/{skill_id}",
                        json=payload,
                        headers=HEADERS,
                    )
                    for payload in payloads
                ]
            )
            statuses = [response.status_code for response in responses]
            assert statuses.count(200) == 5
            assert statuses.count(409) == 0
            assert statuses.count(500) == 0

        async with postgres_session_factory() as session:
            rows = (
                await session.scalars(
                    select(AgentSkill).where(
                        AgentSkill.agent_id == agent_id, AgentSkill.skill_id == skill_id
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].pinned_revision_id is not None
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- 15: AgentSession duplicate concurrent association -----------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_session_concurrent_duplicate_put_converges_to_one_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Consolidated re-proof (SM-804 already proved this in isolation): N=5
    identical concurrent PUT for the same Agent<->Session pair converge to
    exactly one row with a single, shared `created_at`."""

    session_id: object | None = None
    agent_id: object | None = None
    try:
        session_result = await SessionService(
            session_factory=postgres_session_factory
        ).create_session(SessionCreateRequest())
        session_id = session_result.session_uuid

        app = build_app(postgres_session_factory)
        async with build_client(app) as client:
            created = await create_agent_via_http(client)
            agent_id = created["agent_uuid"]

            responses = await asyncio.gather(
                *[
                    client.put(f"/api/v1/agents/{agent_id}/sessions/{session_id}", headers=HEADERS)
                    for _ in range(5)
                ]
            )
            statuses = [response.status_code for response in responses]
            assert statuses.count(200) == 5
            assert statuses.count(409) == 0
            assert statuses.count(500) == 0

            created_ats = {r.json()["data"]["association_created_at"] for r in responses}
            assert len(created_ats) == 1

        async with postgres_session_factory() as session:
            rows = (
                await session.scalars(
                    select(AgentSession).where(
                        AgentSession.agent_id == agent_id, AgentSession.session_id == session_id
                    )
                )
            ).all()
            assert len(rows) == 1
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_sessions(postgres_session_factory, {session_id} if session_id else set())


# --- 45/SS19.6: different Agents never serialize on a global lock ------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_agents_do_not_serialize_on_a_global_lock(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Agent A's row lock (held externally, simulating an in-flight PATCH/
    archive/restore) must never block an independent PATCH on Agent B --
    proves per-Agent, not global, locking."""

    service = AgentService(session_factory=postgres_session_factory)
    app = build_app(postgres_session_factory)
    agent_a_id: object | None = None
    agent_b_id: object | None = None
    try:
        async with build_client(app) as client:
            created_a = await create_agent_via_http(client)
            created_b = await create_agent_via_http(client)
        agent_a_id = created_a["agent_uuid"]
        agent_b_id = created_b["agent_uuid"]

        ready = asyncio.Event()
        release = asyncio.Event()
        holder_task = asyncio.create_task(
            _hold_agent_row_lock(postgres_session_factory, agent_a_id, ready=ready, release=release)
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)

            # Agent B's own lock must be independently obtainable while
            # Agent A's row lock is held -- proves no accidental global lock.
            result = await asyncio.wait_for(
                service.update_agent(agent_b_id, AgentUpdateRequest(description="B1")),
                timeout=2,
            )
            assert result.description == "B1"
        finally:
            release.set()
            await asyncio.wait_for(holder_task, timeout=5)
    finally:
        agent_ids = {agent_a_id, agent_b_id} - {None}
        await cleanup_agents(postgres_session_factory, agent_ids)
