"""Real-HTTP, real-PostgreSQL proof of the SM-802 Agent management API's
status-code matrix, ``ErrorEnvelope``/``SuccessEnvelope`` shapes, lifecycle
timestamp exactness, and duplicate-name concurrency.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database -- no embedding
provider, no Neo4j, no fake anywhere in the request path (Agent management
has no external dependency at all). Requires migrations already applied
through 0015. Service-level behavior (row locking, translation of
IntegrityError) is already proven in ``test_agents_management.py`` with a
fake Unit of Work; this file proves the wire contract and real-database
concurrency on top of it, mirroring
``test_skills_http_postgres_integration.py``.
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
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Agent
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from tests.unit._app_factory import create_app

POSTGRES_AGENTS_MANAGEMENT_ENV = "SOFIAS_MEMORY_RUN_AGENTS_MANAGEMENT_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def _agents_management_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_AGENTS_MANAGEMENT_ENV) != "1":
        pytest.skip(
            f"set {POSTGRES_AGENTS_MANAGEMENT_ENV}=1 to run Agent management HTTP PostgreSQL tests"
        )
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _agents_management_http_test_database_url(dict(os.environ))

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


async def cleanup_agents(session_factory: AsyncSessionFactory, agent_ids: set[Any]) -> None:
    if not agent_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        await session.commit()


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


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": unique_name("http-agent")}
    payload.update(overrides)
    return payload


def test_agents_management_http_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _agents_management_http_test_database_url({})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_agent_returns_201_and_duplicate_name_returns_409(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    payload = create_payload(display_name="Research Agent")
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            response = await client.post("/api/v1/agents", json=payload, headers=HEADERS)
            assert response.status_code == 201
            body = response.json()
            assert body["data"]["name"] == payload["name"]
            assert body["data"]["display_name"] == "Research Agent"
            assert body["data"]["status"] == "active"
            assert body["data"]["created_at"] == body["data"]["updated_at"]
            assert body["meta"]["request_id"]
            agent_id = body["data"]["agent_uuid"]

            duplicate = await client.post("/api/v1/agents", json=payload, headers=HEADERS)
            assert duplicate.status_code == 409
            error_body = duplicate.json()
            assert error_body["error"]["code"] == "INVALID_REQUEST"
            assert error_body["error"]["request_id"]
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_agent_returns_200_and_missing_returns_404(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/agents", json=create_payload(), headers=HEADERS)
            agent_id = created.json()["data"]["agent_uuid"]

            found = await client.get(f"/api/v1/agents/{agent_id}", headers=HEADERS)
            assert found.status_code == 200
            assert found.json()["data"]["agent_uuid"] == agent_id

            missing = await client.get(f"/api/v1/agents/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_agents_defaults_to_active_only(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    active_id: object | None = None
    archived_id: object | None = None

    try:
        async with build_client(app) as client:
            active_created = await client.post(
                "/api/v1/agents", json=create_payload(), headers=HEADERS
            )
            active_id = active_created.json()["data"]["agent_uuid"]

            archived_created = await client.post(
                "/api/v1/agents", json=create_payload(), headers=HEADERS
            )
            archived_id = archived_created.json()["data"]["agent_uuid"]
            await client.post(f"/api/v1/agents/{archived_id}/archive", headers=HEADERS)

            default_listed = await client.get("/api/v1/agents", headers=HEADERS)
            assert default_listed.status_code == 200
            default_body = default_listed.json()["data"]
            assert "items" in default_body
            default_ids = {item["agent_uuid"] for item in default_body["items"]}
            assert active_id in default_ids
            assert archived_id not in default_ids

            active_filtered = await client.get(
                "/api/v1/agents", params={"status": "active"}, headers=HEADERS
            )
            active_ids = {item["agent_uuid"] for item in active_filtered.json()["data"]["items"]}
            assert active_id in active_ids
            assert archived_id not in active_ids

            archived_filtered = await client.get(
                "/api/v1/agents", params={"status": "archived"}, headers=HEADERS
            )
            archived_ids = {
                item["agent_uuid"] for item in archived_filtered.json()["data"]["items"]
            }
            assert archived_id in archived_ids
            assert active_id not in archived_ids
    finally:
        await cleanup_agents(
            postgres_session_factory,
            {id_ for id_ in (active_id, archived_id) if id_ is not None},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_agent_item_never_includes_instructions_or_metadata(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/agents",
                json=create_payload(
                    instructions="sentinel secret-ish procedural text",
                    metadata={"sentinel": "value"},
                ),
                headers=HEADERS,
            )
            agent_id = created.json()["data"]["agent_uuid"]

            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=HEADERS)
            assert "sentinel secret-ish procedural text" in detail.json()["data"]["instructions"]
            assert detail.json()["data"]["metadata"] == {"sentinel": "value"}

            listed = await client.get("/api/v1/agents", headers=HEADERS)
            item = next(
                item for item in listed.json()["data"]["items"] if item["agent_uuid"] == agent_id
            )
            assert "instructions" not in item
            assert "metadata" not in item
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_updates_fields_rejects_name_and_null_metadata(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/agents",
                json=create_payload(metadata={"a": 1, "b": 2}),
                headers=HEADERS,
            )
            agent_id = created.json()["data"]["agent_uuid"]

            patched = await client.patch(
                f"/api/v1/agents/{agent_id}",
                json={"display_name": "New name", "metadata": {"c": 3}},
                headers=HEADERS,
            )
            assert patched.status_code == 200
            body = patched.json()["data"]
            assert body["display_name"] == "New name"
            assert body["metadata"] == {"c": 3}  # wholesale replace, never merged

            name_rejected = await client.patch(
                f"/api/v1/agents/{agent_id}",
                json={"name": "new-name"},
                headers=HEADERS,
            )
            assert name_rejected.status_code == 422

            null_metadata_rejected = await client.patch(
                f"/api/v1/agents/{agent_id}",
                json={"metadata": None},
                headers=HEADERS,
            )
            assert null_metadata_rejected.status_code == 422

            empty_patch_rejected = await client.patch(
                f"/api/v1/agents/{agent_id}", json={}, headers=HEADERS
            )
            assert empty_patch_rejected.status_code == 422

            missing = await client.patch(
                f"/api/v1/agents/{uuid4()}",
                json={"display_name": "X"},
                headers=HEADERS,
            )
            assert missing.status_code == 404
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_and_restore_are_idempotent_with_exact_timestamp_replay(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/agents", json=create_payload(), headers=HEADERS)
            agent_id = created.json()["data"]["agent_uuid"]

            archived = await client.post(f"/api/v1/agents/{agent_id}/archive", headers=HEADERS)
            assert archived.status_code == 200
            archived_body = archived.json()["data"]
            assert archived_body["status"] == "archived"
            assert archived_body["archived_at"] is not None
            assert archived_body["archived_at"] == archived_body["updated_at"]

            archived_again = await client.post(
                f"/api/v1/agents/{agent_id}/archive", headers=HEADERS
            )
            assert archived_again.status_code == 200
            replay_body = archived_again.json()["data"]
            assert replay_body["status"] == "archived"
            assert replay_body["archived_at"] == archived_body["archived_at"]
            assert replay_body["updated_at"] == archived_body["updated_at"]

            # Archived management remains fully allowed -- discovery filter,
            # never an admission barrier.
            patched_while_archived = await client.patch(
                f"/api/v1/agents/{agent_id}",
                json={"description": "Still editable while archived."},
                headers=HEADERS,
            )
            assert patched_while_archived.status_code == 200
            assert patched_while_archived.json()["data"]["status"] == "archived"

            restored = await client.post(f"/api/v1/agents/{agent_id}/restore", headers=HEADERS)
            assert restored.status_code == 200
            restored_body = restored.json()["data"]
            assert restored_body["status"] == "active"
            assert restored_body["archived_at"] is None

            restored_again = await client.post(
                f"/api/v1/agents/{agent_id}/restore", headers=HEADERS
            )
            assert restored_again.status_code == 200
            restore_replay_body = restored_again.json()["data"]
            assert restore_replay_body["status"] == "active"
            assert restore_replay_body["archived_at"] is None
            assert restore_replay_body["updated_at"] == restored_body["updated_at"]

            missing_archive = await client.post(
                f"/api/v1/agents/{uuid4()}/archive", headers=HEADERS
            )
            assert missing_archive.status_code == 404
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_create_agent_same_name_through_http_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    app = build_app(postgres_session_factory)
    payload = create_payload()
    agent_id: object | None = None

    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[client.post("/api/v1/agents", json=payload, headers=HEADERS) for _ in range(5)]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(201) == 1
        assert statuses.count(409) == 4
        assert statuses.count(500) == 0

        winner = next(response for response in responses if response.status_code == 201)
        agent_id = winner.json()["data"]["agent_uuid"]

        for response in responses:
            if response.status_code == 409:
                body = response.json()
                assert body["error"]["code"] == "INVALID_REQUEST"
                assert body["error"]["request_id"]

        async with postgres_session_factory() as session:
            agents_with_name = (
                await session.scalars(select(Agent).where(Agent.name == payload["name"]))
            ).all()
            assert len(agents_with_name) == 1
            assert str(agents_with_name[0].id) == agent_id
    finally:
        await cleanup_agents(postgres_session_factory, {agent_id} if agent_id else set())
