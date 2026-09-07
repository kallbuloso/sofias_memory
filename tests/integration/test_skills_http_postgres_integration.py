"""Real-HTTP, real-PostgreSQL proof of the SM-702 Skill management API's
status-code matrix and ``ErrorEnvelope``/``SuccessEnvelope`` shapes.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database (only the embedding
provider is faked, by monkeypatching ``OpenAIEmbeddingClient`` where
``sofias_memory.api.routes.skills`` looks it up). Service-level behavior
(transaction boundaries, concurrency, replay semantics) is already proven in
``test_skills_service_postgres_integration.py``; this file proves the wire
contract on top of it. Requires migrations already applied through 0014.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
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
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from tests.unit._app_factory import create_app

POSTGRES_SKILLS_HTTP_ENV = "SOFIAS_MEMORY_RUN_SKILLS_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
EMBEDDING_DIMENSIONS = 3072


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


class FailingEmbeddingClient:
    """Raises a plain, generic exception -- the same shape a real OpenAI SDK
    transport failure would surface -- to prove the route never leaks it
    raw and always translates it through the existing provider/dependency
    error contract."""

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


def _skills_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_HTTP_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_HTTP_ENV}=1 to run Skill HTTP PostgreSQL tests")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _skills_http_test_database_url(dict(os.environ))

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


async def cleanup_skills(session_factory: AsyncSessionFactory, skill_ids: set[Any]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
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


def build_app(
    session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding_client_factory: Any = lambda settings: FakeEmbeddingClient(),
) -> Any:
    monkeypatch.setattr(
        "sofias_memory.api.routes.skills.OpenAIEmbeddingClient",
        embedding_client_factory,
    )
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
    payload: dict[str, object] = {
        "name": unique_name("http-skill"),
        "description": "A Skill exercised over real HTTP.",
        "procedure": "Do the thing.",
    }
    payload.update(overrides)
    return payload


def revision_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "description": "A Skill exercised over real HTTP.",
        "procedure": "Do the thing, v2.",
    }
    payload.update(overrides)
    return payload


HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def test_skills_http_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _skills_http_test_database_url({})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_skill_returns_201_and_duplicate_name_returns_409(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    payload = create_payload()
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            response = await client.post("/api/v1/skills", json=payload, headers=HEADERS)
            assert response.status_code == 201
            body = response.json()
            assert body["data"]["name"] == payload["name"]
            assert body["data"]["current_revision"] == 1
            assert "procedure" not in body["data"]
            assert body["meta"]["request_id"]
            skill_id = body["data"]["skill_uuid"]

            duplicate = await client.post("/api/v1/skills", json=payload, headers=HEADERS)
            assert duplicate.status_code == 409
            error_body = duplicate.json()
            assert error_body["error"]["code"] == "INVALID_REQUEST"
            assert error_body["error"]["request_id"]
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_skill_returns_200_and_missing_returns_404(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            found = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            assert found.status_code == 200
            assert found.json()["data"]["skill_uuid"] == skill_id

            missing = await client.get(f"/api/v1/skills/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "INVALID_REQUEST"
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_skills_returns_200_with_pagination_envelope(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            listed = await client.get("/api/v1/skills", headers=HEADERS)
            assert listed.status_code == 200
            body = listed.json()["data"]
            assert "items" in body and "limit" in body and "offset" in body and "total" in body
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_revision_new_returns_201_replay_returns_200_missing_skill_returns_404(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            new_revision = await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json=revision_payload(),
                headers=HEADERS,
            )
            assert new_revision.status_code == 201
            assert new_revision.json()["data"]["revision"] == 2

            replay = await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json=revision_payload(),
                headers=HEADERS,
            )
            assert replay.status_code == 200
            assert replay.json()["data"]["revision"] == 2

            missing_skill = await client.post(
                f"/api/v1/skills/{uuid4()}/revisions",
                json=revision_payload(),
                headers=HEADERS,
            )
            assert missing_skill.status_code == 404
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_and_get_revisions_return_200_and_missing_revision_returns_404(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            listed = await client.get(f"/api/v1/skills/{skill_id}/revisions", headers=HEADERS)
            assert listed.status_code == 200
            assert listed.json()["data"]["total"] == 1

            found = await client.get(f"/api/v1/skills/{skill_id}/revisions/1", headers=HEADERS)
            assert found.status_code == 200
            assert found.json()["data"]["procedure"]

            missing = await client.get(f"/api/v1/skills/{skill_id}/revisions/99", headers=HEADERS)
            assert missing.status_code == 404
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_rollback_returns_200_invalid_target_returns_422_malformed_body_returns_422(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json=revision_payload(),
                headers=HEADERS,
            )

            rollback = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"current_revision": 1},
                headers=HEADERS,
            )
            assert rollback.status_code == 200
            assert rollback.json()["data"]["current_revision"] == 1

            invalid_target = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"current_revision": 99},
                headers=HEADERS,
            )
            assert invalid_target.status_code == 422

            malformed = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"current_revision": "not-an-int"},
                headers=HEADERS,
            )
            assert malformed.status_code == 422
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_and_restore_are_both_idempotent_200(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            archived = await client.post(f"/api/v1/skills/{skill_id}/archive", headers=HEADERS)
            assert archived.status_code == 200
            assert archived.json()["data"]["status"] == "archived"

            archived_again = await client.post(
                f"/api/v1/skills/{skill_id}/archive", headers=HEADERS
            )
            assert archived_again.status_code == 200
            assert archived_again.json()["data"]["status"] == "archived"

            restored = await client.post(f"/api/v1/skills/{skill_id}/restore", headers=HEADERS)
            assert restored.status_code == 200
            assert restored.json()["data"]["status"] == "active"

            restored_again = await client.post(
                f"/api/v1/skills/{skill_id}/restore", headers=HEADERS
            )
            assert restored_again.status_code == 200
            assert restored_again.json()["data"]["status"] == "active"

            missing_archive = await client.post(
                f"/api/v1/skills/{uuid4()}/archive", headers=HEADERS
            )
            assert missing_archive.status_code == 404
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_skill_provider_failure_returns_503_dependency_unavailable(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(
        postgres_session_factory,
        monkeypatch,
        embedding_client_factory=lambda settings: FailingEmbeddingClient(),
    )
    payload = create_payload()

    async with build_client(app) as client:
        response = await client.post("/api/v1/skills", json=payload, headers=HEADERS)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "ConnectionError" not in body["error"]["message"]
    assert body["error"]["request_id"]

    async with postgres_session_factory() as session:
        found = await session.scalar(select(Skill).where(Skill.name == payload["name"]))
        assert found is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_revision_provider_failure_returns_503_and_leaves_skill_unchanged(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            created_updated_at = created.json()["data"]["updated_at"]

        failing_app = build_app(
            postgres_session_factory,
            monkeypatch,
            embedding_client_factory=lambda settings: FailingEmbeddingClient(),
        )
        async with build_client(failing_app) as client:
            response = await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json=revision_payload(),
                headers=HEADERS,
            )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "ConnectionError" not in body["error"]["message"]

        async with build_client(app) as client:
            fetched = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            assert fetched.json()["data"]["current_revision"] == 1
            assert fetched.json()["data"]["updated_at"] == created_updated_at

            revisions = await client.get(f"/api/v1/skills/{skill_id}/revisions", headers=HEADERS)
            assert revisions.json()["data"]["total"] == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_create_skill_same_name_through_http_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    payload = create_payload()
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[client.post("/api/v1/skills", json=payload, headers=HEADERS) for _ in range(5)]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(201) == 1
        assert statuses.count(409) == 4
        assert statuses.count(500) == 0

        winner = next(response for response in responses if response.status_code == 201)
        skill_id = winner.json()["data"]["skill_uuid"]

        for response in responses:
            if response.status_code == 409:
                body = response.json()
                assert body["error"]["code"] == "INVALID_REQUEST"
                assert body["error"]["request_id"]

        async with postgres_session_factory() as session:
            skills_with_name = (
                await session.scalars(select(Skill).where(Skill.name == payload["name"]))
            ).all()
            assert len(skills_with_name) == 1

            revisions_for_winner = (
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skills_with_name[0].id)
                )
            ).all()
            assert len(revisions_for_winner) == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
