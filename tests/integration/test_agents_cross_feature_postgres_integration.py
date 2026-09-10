"""Real-PostgreSQL SM-805 cross-feature hardening for Agent Management.

Proves structural and behavioral isolation between Agent Management
(Agent/AgentSkill/AgentSession, ADR-0014) and the rest of Sofias Memory, and
proves the composed lifecycle invariants SM-801..SM-804 did not exercise
together: the full archived-Agent management matrix (management + Skill
association + Session association) via the real public HTTP API; the
discovery-filter-vs-admission-barrier distinction on `GET /agents`; Skill
archive/restore and Session archive/restore each preserving their
association untouched; the archived-Session admission barrier (ADR-0012)
never bypassed by association; a comprehensive Agent/AgentSkill/AgentSession
operations matrix never writing to `graph_outbox`; no forbidden
ownership column or cross-aggregate FK on `agents`/`agent_skills`/
`agent_sessions`; and full timestamp isolation between Agent, Skill, and
Session under association mutation. Requires migrations already applied
through 0017.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text

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
    GraphOutbox,
    PipelineRun,
    Query,
    Session,
    SessionEntry,
    Skill,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.agent_skills import AgentSkillSetRequest
from sofias_memory.schemas.agents import AgentCreateRequest, AgentUpdateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest
from sofias_memory.schemas.skills import SkillCreateRequest, SkillRevisionCreateRequest
from sofias_memory.services.agent_sessions import AgentSessionService
from sofias_memory.services.agent_skills import AgentSkillService
from sofias_memory.services.agents import AgentService
from sofias_memory.services.sessions import SessionService
from sofias_memory.services.skills import (
    SkillService,
    create_skill_aggregate,
    create_skill_revision,
)
from tests.unit._app_factory import create_app

POSTGRES_AGENTS_CROSS_FEATURE_ENV = "SOFIAS_MEMORY_RUN_AGENTS_CROSS_FEATURE_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
EMBEDDING_DIMENSIONS = 3072
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}

FORBIDDEN_OWNERSHIP_COLUMNS = frozenset(
    {
        "dataset_id",
        "session_id",
        "source_id",
        "document_id",
        "owner_id",
        "tenant_id",
        "user_id",
    }
)
OTHER_AGGREGATE_TABLES = ("datasets", "sources", "documents", "queries", "pipeline_runs")


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _cross_feature_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENTS_CROSS_FEATURE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_AGENTS_CROSS_FEATURE_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _cross_feature_test_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def skill_service(session_factory: AsyncSessionFactory) -> SkillService:
    return SkillService(
        load_settings(), embedding_client=FakeEmbeddingClient(), session_factory=session_factory
    )


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


def make_http_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        llm_api_key=LLM_API_KEY,
        app_env="test",
    )


def build_app(session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "sofias_memory.api.routes.skills.OpenAIEmbeddingClient",
        lambda settings: FakeEmbeddingClient(),
    )
    return create_app(
        make_http_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
    )


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def create_skill_with_two_revisions_via_http(client: httpx.AsyncClient, *, name: str) -> str:
    created = await client.post(
        "/api/v1/skills",
        json={"name": name, "description": "d1", "procedure": "p1"},
        headers=HEADERS,
    )
    assert created.status_code == 201
    skill_id: str = created.json()["data"]["skill_uuid"]
    revised = await client.post(
        f"/api/v1/skills/{skill_id}/revisions",
        json={"description": "d2", "procedure": "p2"},
        headers=HEADERS,
    )
    assert revised.status_code == 201
    return skill_id


async def create_session_via_http(client: httpx.AsyncClient) -> str:
    created = await client.post("/api/v1/sessions", json={}, headers=HEADERS)
    assert created.status_code == 201
    session_id: str = created.json()["data"]["session_uuid"]
    return session_id


# --- 5/6/14: archived-Agent full management matrix, discovery vs barrier ----


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_agent_full_management_matrix_via_real_http(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive is a discovery/availability filter, never a management
    admission barrier (Feature Contract SS 2.4/7). With the Agent archived,
    every management, Agent<->Skill, and Agent<->Session operation must
    continue to succeed -- including creating the associations for the
    first time while already archived."""

    app = build_app(postgres_session_factory, monkeypatch)
    agent_id: object | None = None
    skill_id: object | None = None
    session_id: object | None = None
    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/agents",
                json={"name": unique_name("archive-matrix"), "description": "D0"},
                headers=HEADERS,
            )
            agent_id = created.json()["data"]["agent_uuid"]

            skill_id = await create_skill_with_two_revisions_via_http(
                client, name=unique_name("archive-matrix-skill")
            )
            session_id = await create_session_via_http(client)

            archived = await client.post(f"/api/v1/agents/{agent_id}/archive", headers=HEADERS)
            assert archived.status_code == 200
            assert archived.json()["data"]["status"] == "archived"

            # GET Agent detail.
            get_agent = await client.get(f"/api/v1/agents/{agent_id}", headers=HEADERS)
            assert get_agent.status_code == 200

            # PATCH Agent.
            patched = await client.patch(
                f"/api/v1/agents/{agent_id}", json={"description": "D1"}, headers=HEADERS
            )
            assert patched.status_code == 200
            assert patched.json()["data"]["status"] == "archived"

            # POST archive replay (idempotent, no error).
            archive_replay = await client.post(
                f"/api/v1/agents/{agent_id}/archive", headers=HEADERS
            )
            assert archive_replay.status_code == 200
            assert archive_replay.json()["data"]["status"] == "archived"

            # GET Agent Skills (empty so far).
            list_skills_empty = await client.get(
                f"/api/v1/agents/{agent_id}/skills", headers=HEADERS
            )
            assert list_skills_empty.status_code == 200
            assert list_skills_empty.json()["data"]["items"] == []

            # PUT Agent Skill -- creating a NEW association while archived.
            put_skill = await client.put(
                f"/api/v1/agents/{agent_id}/skills/{skill_id}",
                json={"pinned_revision": 1},
                headers=HEADERS,
            )
            assert put_skill.status_code == 200

            get_skills = await client.get(f"/api/v1/agents/{agent_id}/skills", headers=HEADERS)
            assert get_skills.status_code == 200
            assert len(get_skills.json()["data"]["items"]) == 1

            # DELETE Agent Skill.
            delete_skill = await client.delete(
                f"/api/v1/agents/{agent_id}/skills/{skill_id}", headers=HEADERS
            )
            assert delete_skill.status_code == 204

            # GET Agent Sessions (empty so far).
            list_sessions_empty = await client.get(
                f"/api/v1/agents/{agent_id}/sessions", headers=HEADERS
            )
            assert list_sessions_empty.status_code == 200
            assert list_sessions_empty.json()["data"]["items"] == []

            # PUT Agent Session -- creating a NEW association while archived.
            put_session = await client.put(
                f"/api/v1/agents/{agent_id}/sessions/{session_id}", headers=HEADERS
            )
            assert put_session.status_code == 200

            get_sessions = await client.get(f"/api/v1/agents/{agent_id}/sessions", headers=HEADERS)
            assert get_sessions.status_code == 200
            assert len(get_sessions.json()["data"]["items"]) == 1

            # DELETE Agent Session.
            delete_session = await client.delete(
                f"/api/v1/agents/{agent_id}/sessions/{session_id}", headers=HEADERS
            )
            assert delete_session.status_code == 204

            # Default GET /agents (no filter) must exclude the archived Agent.
            default_list = await client.get("/api/v1/agents", headers=HEADERS)
            assert agent_id not in {
                item["agent_uuid"] for item in default_list.json()["data"]["items"]
            }

            # status=archived filter must include it.
            archived_list = await client.get(
                "/api/v1/agents", params={"status": "archived"}, headers=HEADERS
            )
            assert agent_id in {
                item["agent_uuid"] for item in archived_list.json()["data"]["items"]
            }

            # POST restore.
            restored = await client.post(f"/api/v1/agents/{agent_id}/restore", headers=HEADERS)
            assert restored.status_code == 200
            assert restored.json()["data"]["status"] == "active"
            assert restored.json()["data"]["description"] == "D1"

            restored_default_list = await client.get("/api/v1/agents", headers=HEADERS)
            assert agent_id in {
                item["agent_uuid"] for item in restored_default_list.json()["data"]["items"]
            }
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
        await cleanup_sessions(postgres_session_factory, {session_id} if session_id else set())


# --- 38: security boundary ----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_endpoints_reject_missing_api_key(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Representative sample of the 12 Agent operations, all still gated by
    the single global `X-API-Key` mechanism -- never an Agent-specific
    credential/permission/ACL."""

    app = build_app(postgres_session_factory, monkeypatch)
    fake_uuid = uuid4()
    async with build_client(app) as client:
        for method, path, json_body in (
            ("POST", "/api/v1/agents", {"name": "x"}),
            ("GET", "/api/v1/agents", None),
            ("GET", f"/api/v1/agents/{fake_uuid}", None),
            ("PATCH", f"/api/v1/agents/{fake_uuid}", {}),
            ("POST", f"/api/v1/agents/{fake_uuid}/archive", None),
            ("POST", f"/api/v1/agents/{fake_uuid}/restore", None),
            ("GET", f"/api/v1/agents/{fake_uuid}/skills", None),
            ("PUT", f"/api/v1/agents/{fake_uuid}/skills/{fake_uuid}", {}),
            ("DELETE", f"/api/v1/agents/{fake_uuid}/skills/{fake_uuid}", None),
            ("GET", f"/api/v1/agents/{fake_uuid}/sessions", None),
            ("PUT", f"/api/v1/agents/{fake_uuid}/sessions/{fake_uuid}", None),
            ("DELETE", f"/api/v1/agents/{fake_uuid}/sessions/{fake_uuid}", None),
        ):
            response = await client.request(method, path, json=json_body)
            assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


# --- 13: Skill archive/restore preserves AgentSkill --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_archive_restore_preserves_agent_skill_association(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent_service = AgentService(session_factory=postgres_session_factory)
    agent_skill_service = AgentSkillService(session_factory=postgres_session_factory)
    resolver = skill_service(postgres_session_factory)
    agent_id: object | None = None
    skill_id: object | None = None
    try:
        agent = await agent_service.create_agent(
            AgentCreateRequest(name=unique_name("skill-archive-agent"))
        )
        agent_id = agent.agent_uuid

        created_skill = await resolver.create_skill(
            SkillCreateRequest(
                name=unique_name("skill-archive-skill"), description="d", procedure="p"
            )  # type: ignore[call-arg]
        )
        skill_id = created_skill.skill_uuid
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]

        put_result = await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=1)
        )
        before_created_at = put_result.association_created_at
        before_pin = put_result.pinned_revision
        before_effective = put_result.effective_revision

        await resolver.archive_skill(skill_id)

        listed_after_archive = await agent_skill_service.list_skills(agent_id)
        assert len(listed_after_archive.items) == 1
        item = listed_after_archive.items[0]
        assert item.skill_uuid == skill_id
        assert item.status.value == "archived"
        assert item.pinned_revision == before_pin
        assert item.effective_revision == before_effective
        assert item.association_created_at == before_created_at

        await resolver.restore_skill(skill_id)

        listed_after_restore = await agent_skill_service.list_skills(agent_id)
        assert len(listed_after_restore.items) == 1
        restored_item = listed_after_restore.items[0]
        assert restored_item.status.value == "active"
        assert restored_item.pinned_revision == before_pin
        assert restored_item.effective_revision == before_effective
        assert restored_item.association_created_at == before_created_at
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- 17/18/19: Session archive/restore preserves AgentSession, no activity --


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_archive_restore_preserves_agent_session_association(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent_service = AgentService(session_factory=postgres_session_factory)
    agent_session_service = AgentSessionService(session_factory=postgres_session_factory)
    session_svc = SessionService(session_factory=postgres_session_factory)
    agent_id: object | None = None
    session_id: object | None = None
    try:
        agent = await agent_service.create_agent(
            AgentCreateRequest(name=unique_name("session-archive-agent"))
        )
        agent_id = agent.agent_uuid

        session_result = await session_svc.create_session(SessionCreateRequest())
        session_id = session_result.session_uuid

        put_result = await agent_session_service.associate_session(agent_id, session_id)
        before_created_at = put_result.association_created_at

        await session_svc.archive_session(session_id)

        listed_after_archive = await agent_session_service.list_sessions(agent_id)
        assert len(listed_after_archive.items) == 1
        item = listed_after_archive.items[0]
        assert item.session_uuid == session_id
        assert item.status.value == "archived"
        assert item.association_created_at == before_created_at

        # PUT on the already-archived Session must still succeed (200) and
        # never create Session activity of any kind.
        async with postgres_session_factory() as db_session:
            before_entries = await db_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session_id)
            )
            before_queries = await db_session.scalar(
                select(func.count()).select_from(Query).where(Query.session_id == session_id)
            )
            before_runs = await db_session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.session_id == session_id)
            )
            before_session_row = await db_session.get(Session, session_id)
            assert before_session_row is not None
            before_updated_at = before_session_row.updated_at
            before_archived_at = before_session_row.archived_at

        replay_put = await agent_session_service.associate_session(agent_id, session_id)
        assert replay_put.association_created_at == before_created_at

        async with postgres_session_factory() as db_session:
            after_entries = await db_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session_id)
            )
            after_queries = await db_session.scalar(
                select(func.count()).select_from(Query).where(Query.session_id == session_id)
            )
            after_runs = await db_session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.session_id == session_id)
            )
            after_session_row = await db_session.get(Session, session_id)
            assert after_session_row is not None

        assert after_entries == before_entries == 0
        assert after_queries == before_queries == 0
        assert after_runs == before_runs == 0
        assert after_session_row.updated_at == before_updated_at
        assert after_session_row.archived_at == before_archived_at

        # DELETE while archived must still succeed.
        await agent_session_service.remove_session(agent_id, session_id)
        listed_after_delete = await agent_session_service.list_sessions(agent_id)
        assert listed_after_delete.items == []

        await session_svc.restore_session(session_id)

        # Re-associate after restore -- new created_at, association intact.
        re_put = await agent_session_service.associate_session(agent_id, session_id)
        assert re_put.association_created_at != before_created_at
        assert re_put.status.value == "active"
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_sessions(postgres_session_factory, {session_id} if session_id else set())


# --- 30: comprehensive Agent operations graph_outbox matrix -------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_outbox_delta_zero_across_full_agent_operations_matrix(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent_service = AgentService(session_factory=postgres_session_factory)
    agent_skill_service = AgentSkillService(session_factory=postgres_session_factory)
    agent_session_service = AgentSessionService(session_factory=postgres_session_factory)
    session_svc = SessionService(session_factory=postgres_session_factory)
    agent_id: object | None = None
    skill_id: object | None = None
    session_id: object | None = None

    async def outbox_count() -> int:
        async with postgres_session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(GraphOutbox)) or 0)

    try:
        before = await outbox_count()

        agent = await agent_service.create_agent(
            AgentCreateRequest(name=unique_name("outbox-matrix-agent"))
        )
        agent_id = agent.agent_uuid
        await agent_service.update_agent(agent_id, AgentUpdateRequest(description="D1"))
        await agent_service.archive_agent(agent_id)
        await agent_service.restore_agent(agent_id)
        await agent_service.list_agents(limit=10, offset=0, status=None)  # type: ignore[arg-type]
        await agent_service.get_agent(agent_id)

        fake_embedding = [0.0] * EMBEDDING_DIMENSIONS
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            skill = await create_skill_aggregate(
                uow,
                name=unique_name("outbox-matrix-skill"),
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

        await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=1)
        )
        await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=2)
        )
        await agent_skill_service.set_skill(agent_id, skill_id, AgentSkillSetRequest())
        await agent_skill_service.list_skills(agent_id)
        await agent_skill_service.remove_skill(agent_id, skill_id)

        session_result = await session_svc.create_session(SessionCreateRequest())
        session_id = session_result.session_uuid
        await agent_session_service.associate_session(agent_id, session_id)
        await agent_session_service.associate_session(agent_id, session_id)  # replay
        await agent_session_service.list_sessions(agent_id)
        await agent_session_service.remove_session(agent_id, session_id)

        after = await outbox_count()
        assert after - before == 0
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
        await cleanup_sessions(postgres_session_factory, {session_id} if session_id else set())


# --- 40: cross-feature database constraint audit ------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_tables_have_no_forbidden_ownership_columns_or_fks(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    # `agent_sessions.session_id` is the association's own frozen identity
    # column (SM-804, ADR-0014), not a forbidden ownership leak -- excluded
    # per-table rather than dropped from the shared forbidden-set constant.
    legitimate_association_columns = {("agent_sessions", "session_id")}

    async with postgres_session_factory() as session:
        columns = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('agents', 'agent_skills', 'agent_sessions')"
            )
        )
        found_columns = {
            (row.table_name, row.column_name)
            for row in columns.all()
            if row.column_name in FORBIDDEN_OWNERSHIP_COLUMNS
        }
        forbidden_present = found_columns - legitimate_association_columns
        assert forbidden_present == set(), forbidden_present

        fks_from_agents = await session.execute(
            text(
                "SELECT tc.table_name, ccu.table_name AS foreign_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND tc.table_name IN ('agents', 'agent_skills', 'agent_sessions') "
                f"AND ccu.table_name IN {OTHER_AGGREGATE_TABLES!r}"
            )
        )
        assert fks_from_agents.all() == []

        fks_to_agents = await session.execute(
            text(
                "SELECT tc.table_name, ccu.table_name AS foreign_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                f"AND tc.table_name IN {OTHER_AGGREGATE_TABLES!r} "
                "AND ccu.table_name IN ('agents', 'agent_skills', 'agent_sessions')"
            )
        )
        assert fks_to_agents.all() == []


# --- 40/41/42/43: full cross-feature lifecycle timestamp isolation -----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_family_and_dataset_scoped_lifecycles_do_not_affect_each_other(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    agent_service = AgentService(session_factory=postgres_session_factory)
    agent_skill_service = AgentSkillService(session_factory=postgres_session_factory)
    agent_session_service = AgentSessionService(session_factory=postgres_session_factory)
    session_svc = SessionService(session_factory=postgres_session_factory)
    resolver = skill_service(postgres_session_factory)
    agent_id: object | None = None
    skill_id: object | None = None
    session_id: object | None = None
    try:
        agent = await agent_service.create_agent(
            AgentCreateRequest(name=unique_name("isolation-agent"))
        )
        agent_id = agent.agent_uuid

        created_skill = await resolver.create_skill(
            SkillCreateRequest(name=unique_name("isolation-skill"), description="d", procedure="p")  # type: ignore[call-arg]
        )
        skill_id = created_skill.skill_uuid
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]

        session_result = await session_svc.create_session(SessionCreateRequest())
        session_id = session_result.session_uuid

        async def agent_snapshot() -> tuple[object, ...]:
            async with postgres_session_factory() as session:
                agent_row = await session.get(Agent, agent_id)
                assert agent_row is not None
                return (agent_row.status, agent_row.updated_at, agent_row.archived_at)

        async def skill_snapshot() -> tuple[object, ...]:
            async with postgres_session_factory() as session:
                skill_row = await session.get(Skill, skill_id)
                assert skill_row is not None
                return (skill_row.status, skill_row.current_revision_id, skill_row.updated_at)

        async def session_snapshot() -> tuple[object, ...]:
            async with postgres_session_factory() as session:
                session_row = await session.get(Session, session_id)
                assert session_row is not None
                return (session_row.status, session_row.updated_at, session_row.archived_at)

        before_agent = await agent_snapshot()

        # Full AgentSkill/AgentSession lifecycle: create, re-pin, unpin,
        # delete, re-create -- must never touch Agent.updated_at/archived_at.
        await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=1)
        )
        await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=2)
        )
        await agent_skill_service.set_skill(agent_id, skill_id, AgentSkillSetRequest())
        await agent_skill_service.remove_skill(agent_id, skill_id)
        await agent_session_service.associate_session(agent_id, session_id)
        await agent_session_service.remove_session(agent_id, session_id)
        await agent_session_service.associate_session(agent_id, session_id)

        after_agent = await agent_snapshot()
        assert before_agent == after_agent

        # AgentSkill mutation must never touch Skill.updated_at/status/
        # current_revision_id.
        before_skill = await skill_snapshot()
        await agent_skill_service.set_skill(
            agent_id, skill_id, AgentSkillSetRequest(pinned_revision=1)
        )
        await agent_skill_service.set_skill(agent_id, skill_id, AgentSkillSetRequest())
        await agent_skill_service.remove_skill(agent_id, skill_id)
        after_skill = await skill_snapshot()
        assert before_skill == after_skill

        # AgentSession mutation must never touch
        # Session.updated_at/status/archived_at.
        before_session = await session_snapshot()
        await agent_session_service.remove_session(agent_id, session_id)
        await agent_session_service.associate_session(agent_id, session_id)
        after_session = await session_snapshot()
        assert before_session == after_session

        # And the reverse direction: Skill/Session's own lifecycle must
        # never touch Agent.
        before_agent_2 = await agent_snapshot()
        await resolver.archive_skill(skill_id)
        await resolver.restore_skill(skill_id)
        await session_svc.archive_session(session_id)
        await session_svc.restore_session(session_id)
        after_agent_2 = await agent_snapshot()
        assert before_agent_2 == after_agent_2
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
        await cleanup_sessions(postgres_session_factory, {session_id} if session_id else set())
