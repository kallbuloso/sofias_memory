"""Real-HTTP, real-PostgreSQL proof of the SM-704 ``POST /skills/resolve``
route's status-code matrix, progressive-disclosure JSON shape, and
provider-boundary/failure translation.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database (only the embedding
provider is faked, by monkeypatching ``OpenAIEmbeddingClient`` where
``sofias_memory.api.routes.skills`` looks it up). Ranking correctness,
active-only filtering, and the current-revision pointer are already proven
in ``test_skills_resolve_postgres_integration.py``; this file proves the
wire contract on top of it. Requires migrations already applied through
0014.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Skill
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from tests.unit._app_factory import create_app

POSTGRES_SKILLS_RESOLVE_HTTP_ENV = "SOFIAS_MEMORY_RUN_SKILLS_RESOLVE_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
EMBEDDING_DIMENSIONS = 3072


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


class CountingEmbeddingClient:
    """Records how many times ``embed_texts`` was called -- lets a test
    prove a rejected request never reached the embedding provider at all."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _skills_resolve_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_RESOLVE_HTTP_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_RESOLVE_HTTP_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _skills_resolve_http_test_database_url(dict(os.environ))

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
        "name": unique_name("resolve-http-skill"),
        "description": "A Skill exercised over real HTTP.",
        "procedure": "Do the thing.",
    }
    payload.update(overrides)
    return payload


HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def test_skills_resolve_http_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _skills_resolve_http_test_database_url({})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_returns_200_ranked_result(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            response = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 5},
                headers=HEADERS,
            )
        assert response.status_code == 200
        matches = response.json()["data"]["matches"]
        assert any(match["skill_uuid"] == skill_id for match in matches)
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_returns_200_empty_matches_when_no_active_skills(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            await client.post(f"/api/v1/skills/{skill_id}/archive", headers=HEADERS)

            response = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 5},
                headers=HEADERS,
            )
        assert response.status_code == 200
        matches = response.json()["data"]["matches"]
        assert skill_id not in [m["skill_uuid"] for m in matches]
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_invalid_query_and_top_k_return_422_no_provider_call(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counting_client = CountingEmbeddingClient()
    app = build_app(
        postgres_session_factory,
        monkeypatch,
        embedding_client_factory=lambda settings: counting_client,
    )
    async with build_client(app) as client:
        missing_query = await client.post(
            "/api/v1/skills/resolve", json={"top_k": 5}, headers=HEADERS
        )
        whitespace_query = await client.post(
            "/api/v1/skills/resolve", json={"query": "   "}, headers=HEADERS
        )
        top_k_zero = await client.post(
            "/api/v1/skills/resolve", json={"query": "q", "top_k": 0}, headers=HEADERS
        )
        top_k_too_high = await client.post(
            "/api/v1/skills/resolve", json={"query": "q", "top_k": 21}, headers=HEADERS
        )

    for response in (missing_query, whitespace_query, top_k_zero, top_k_too_high):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert counting_client.calls == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_provider_failure_returns_503(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(
        postgres_session_factory,
        monkeypatch,
        embedding_client_factory=lambda settings: FailingEmbeddingClient(),
    )
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/skills/resolve",
            json={"query": "anything", "top_k": 5},
            headers=HEADERS,
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_response_shape_is_progressive_disclosure_only(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json=create_payload(tags=["deploy"], declared_tools=["Read"]),
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]

            response = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 20},
                headers=HEADERS,
            )
        body_text = response.text
        assert "procedure" not in body_text
        assert "metadata" not in body_text
        assert "license" not in body_text
        assert "resolution_embedding" not in body_text
        assert "content_sha256" not in body_text

        match = next(m for m in response.json()["data"]["matches"] if m["skill_uuid"] == skill_id)
        assert set(match) == {
            "skill_uuid",
            "name",
            "description",
            "current_revision",
            "tags",
            "declared_tools",
            "compatibility",
            "score",
        }
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
