"""Real-PostgreSQL SM-705 concurrency hardening for Skills.

Re-proves, under real load (not just unit-level fakes), the concurrency
invariants the Feature Contract SS 6 freezes: duplicate-name creation
converges to exactly one winner through the real public HTTP API; two
concurrent revision-creation calls with different content get distinct
monotonic ordinals; N concurrent calls with identical content converge to
one persisted revision (safe replay for the rest); and Skill A's per-Skill
lock never blocks a concurrent, independent mutation of Skill B. The
rollback-vs-new-revision both-linearization-orders matrix (Feature Contract
SS 6.5) is already proven with real barrier-synchronized PostgreSQL
transactions in ``test_skills_service_postgres_integration.py``
(SM-702) -- this file does not duplicate it, only re-runs it as part of the
full regression. Requires migrations already applied through 0014.
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
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Skill, SkillRevision
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.skills import SkillCreateRequest, SkillRevisionCreateRequest
from sofias_memory.services.skills import SkillService
from tests.unit._app_factory import create_app

POSTGRES_SKILLS_CONCURRENCY_ENV = "SOFIAS_MEMORY_RUN_SKILLS_CONCURRENCY_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
EMBEDDING_DIMENSIONS = 3072
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _concurrency_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_CONCURRENCY_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_CONCURRENCY_ENV}=1 to run this suite")
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


def skill_service(session_factory: AsyncSessionFactory) -> SkillService:
    return SkillService(
        load_settings(), embedding_client=FakeEmbeddingClient(), session_factory=session_factory
    )


async def cleanup_skills(session_factory: AsyncSessionFactory, skill_ids: set[Any]) -> None:
    if not skill_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
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


# --- 14: duplicate Skill name -- real load, real HTTP -------------------------


async def _run_duplicate_name_load_test(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("concurrent-name")
    payload = {"name": name, "description": "d", "procedure": "p"}
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            responses = await asyncio.gather(
                *[
                    client.post("/api/v1/skills", json=payload, headers=HEADERS)
                    for _ in range(workers)
                ]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(201) == 1, statuses
        assert statuses.count(409) == workers - 1, statuses
        assert statuses.count(500) == 0, statuses

        winner = next(response for response in responses if response.status_code == 201)
        skill_id = winner.json()["data"]["skill_uuid"]
        for response in responses:
            if response.status_code == 409:
                assert response.json()["error"]["code"] == "INVALID_REQUEST"

        async with postgres_session_factory() as session:
            skills = (await session.scalars(select(Skill).where(Skill.name == name))).all()
            assert len(skills) == 1
            revisions = (
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skills[0].id)
                )
            ).all()
            assert len(revisions) == 1
            assert revisions[0].revision == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_name_load_test_converges_to_one_winner_run_1(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_duplicate_name_load_test(postgres_session_factory, monkeypatch, workers=8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_name_load_test_converges_to_one_winner_run_2(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_duplicate_name_load_test(postgres_session_factory, monkeypatch, workers=8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_name_load_test_converges_to_one_winner_run_3(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_duplicate_name_load_test(postgres_session_factory, monkeypatch, workers=8)


# --- 15: concurrent different-content revisions -------------------------------


async def _run_concurrent_different_revisions(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("concurrent-diff-rev")
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json={"name": name, "description": "d", "procedure": "p"},
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]

            responses = await asyncio.gather(
                client.post(
                    f"/api/v1/skills/{skill_id}/revisions",
                    json={"description": "writer-a", "procedure": "p"},
                    headers=HEADERS,
                ),
                client.post(
                    f"/api/v1/skills/{skill_id}/revisions",
                    json={"description": "writer-b", "procedure": "p"},
                    headers=HEADERS,
                ),
            )

        assert all(response.status_code == 201 for response in responses)
        ordinals = sorted(response.json()["data"]["revision"] for response in responses)
        assert ordinals == [2, 3]

        async with postgres_session_factory() as session:
            skill = await session.get(Skill, skill_id)
            assert skill is not None
            current = await session.get(SkillRevision, skill.current_revision_id)
            assert current is not None
            assert current.revision == 3
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_different_content_revisions_get_unique_ordinals_run_1(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_different_revisions(postgres_session_factory, monkeypatch)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_different_content_revisions_get_unique_ordinals_run_2(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_different_revisions(postgres_session_factory, monkeypatch)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_different_content_revisions_get_unique_ordinals_run_3(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_different_revisions(postgres_session_factory, monkeypatch)


# --- 16: concurrent identical-content revisions -------------------------------


async def _run_concurrent_identical_revisions(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch, *, workers: int
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("concurrent-same-rev")
    skill_id: object | None = None
    revision_payload = {"description": "identical", "procedure": "p"}

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json={"name": name, "description": "d", "procedure": "p"},
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]

            responses = await asyncio.gather(
                *[
                    client.post(
                        f"/api/v1/skills/{skill_id}/revisions",
                        json=revision_payload,
                        headers=HEADERS,
                    )
                    for _ in range(workers)
                ]
            )

        statuses = [response.status_code for response in responses]
        assert statuses.count(201) == 1, statuses
        assert statuses.count(200) == workers - 1, statuses
        revisions = {response.json()["data"]["revision"] for response in responses}
        assert revisions == {2}

        async with postgres_session_factory() as session:
            rows = (
                await session.scalars(
                    select(SkillRevision).where(
                        SkillRevision.skill_id == skill_id, SkillRevision.revision == 2
                    )
                )
            ).all()
            assert len(rows) == 1
            skill = await session.get(Skill, skill_id)
            assert skill is not None
            assert skill.current_revision_id == rows[0].id
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_content_revisions_converge_run_1(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_identical_revisions(postgres_session_factory, monkeypatch, workers=8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_content_revisions_converge_run_2(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_identical_revisions(postgres_session_factory, monkeypatch, workers=8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_content_revisions_converge_run_3(
    postgres_session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_concurrent_identical_revisions(postgres_session_factory, monkeypatch, workers=8)


# --- 18: different Skills must not serialize globally -------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_skills_do_not_serialize_on_a_global_lock(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    resolver = skill_service(postgres_session_factory)
    created_a = await resolver.create_skill(
        SkillCreateRequest(name=unique_name("lock-a"), description="d", procedure="p")  # type: ignore[call-arg]
    )
    created_b = await resolver.create_skill(
        SkillCreateRequest(name=unique_name("lock-b"), description="d", procedure="p")  # type: ignore[call-arg]
    )
    skill_ids = {created_a.skill_uuid, created_b.skill_uuid}

    try:
        holder_ready = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_lock_on_a() -> None:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                await uow.skills.get_by_id_for_update(created_a.skill_uuid)
                holder_ready.set()
                await release_holder.wait()
                await uow.commit()

        holder_task = asyncio.create_task(hold_lock_on_a())
        try:
            await asyncio.wait_for(holder_ready.wait(), timeout=5)

            # Skill B's own lock must be independently obtainable while
            # Skill A's row lock is held -- proves no accidental global lock.
            result = await asyncio.wait_for(
                resolver.create_revision(
                    created_b.skill_uuid,
                    SkillRevisionCreateRequest(description="d2", procedure="p2"),  # type: ignore[call-arg]
                ),
                timeout=2,
            )
            assert result[0].revision == 2
        finally:
            release_holder.set()
            await asyncio.wait_for(holder_task, timeout=5)
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)
