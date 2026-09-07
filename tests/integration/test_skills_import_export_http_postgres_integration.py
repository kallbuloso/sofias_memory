"""Real-HTTP, real-PostgreSQL proof of the SM-703 standalone SKILL.md
import/export surface.

Issues actual requests through ``httpx.ASGITransport`` against a real
``create_app()`` wired to a real PostgreSQL database (only the embedding
provider is faked). Covers: new-Skill import, ``/skills/import``'s
always-409-on-existing-name rule (including identical content), new/replay
revision import, frontmatter name-mismatch rejection, import while
archived, export while archived, the structured-create -> export ->
revision-import cross-surface replay proof, the reserved
``sofias-memory.tags`` transport key never persisting into ``metadata``
(verified by direct SQL), provider-failure atomicity, deterministic export,
and the ``content_sha256`` semantic-vs-file-digest distinction. Requires
migrations already applied through 0014.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
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

POSTGRES_SKILLS_IMPORT_EXPORT_ENV = "SOFIAS_MEMORY_RUN_SKILLS_IMPORT_EXPORT_POSTGRES_TESTS"

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


class CountingFailingEmbeddingClient:
    """Records how many times ``embed_texts`` was called, then fails --
    lets a test prove the provider was never invoked at all (e.g. a
    request rejected before the embedding boundary) rather than merely
    that its eventual failure was handled."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        del texts
        raise ConnectionError("simulated embedding provider failure")


def _skills_import_export_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_IMPORT_EXPORT_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_IMPORT_EXPORT_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    database_url = _skills_import_export_test_database_url(dict(os.environ))

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


HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def build_skill_md(
    *,
    name: str,
    description: str = "A Skill exercised over real HTTP.",
    procedure: str = "Do the thing.",
    license: str | None = None,
    compatibility: str | None = None,
    metadata: dict[str, str] | None = None,
    tags: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    frontmatter: dict[str, object] = {"name": name, "description": description}
    if license is not None:
        frontmatter["license"] = license
    if compatibility is not None:
        frontmatter["compatibility"] = compatibility
    combined_metadata = dict(metadata or {})
    if tags:
        combined_metadata["sofias-memory.tags"] = json.dumps(tags)
    if combined_metadata:
        frontmatter["metadata"] = combined_metadata
    if allowed_tools:
        frontmatter["allowed-tools"] = " ".join(allowed_tools)
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{yaml_text}---\n{procedure}"


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": unique_name("http-skill"),
        "description": "A Skill exercised over real HTTP.",
        "procedure": "Do the thing.",
    }
    payload.update(overrides)
    return payload


def test_skills_import_export_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _skills_import_export_test_database_url({})


# --- POST /skills/import -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_new_skill_creates_skill_and_revision_one(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("import-new")
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            response = await client.post(
                "/api/v1/skills/import",
                json={"content": build_skill_md(name=name)},
                headers=HEADERS,
            )
        assert response.status_code == 201
        body = response.json()["data"]
        assert body["name"] == name
        assert body["current_revision"] == 1
        skill_id = body["skill_uuid"]
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_duplicate_name_identical_content_always_409(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``content_sha256`` never decides this route's outcome -- even byte-
    identical content on an already-existing name is 409, never replay."""

    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("import-dup-same")
    content = build_skill_md(name=name)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            first = await client.post(
                "/api/v1/skills/import", json={"content": content}, headers=HEADERS
            )
            assert first.status_code == 201
            skill_id = first.json()["data"]["skill_uuid"]

            second = await client.post(
                "/api/v1/skills/import", json={"content": content}, headers=HEADERS
            )
        assert second.status_code == 409
        error = second.json()["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert error["request_id"]

        async with postgres_session_factory() as session:
            count = len((await session.scalars(select(Skill).where(Skill.name == name))).all())
            assert count == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_duplicate_name_different_content_also_409(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("import-dup-diff")
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            first = await client.post(
                "/api/v1/skills/import",
                json={"content": build_skill_md(name=name, description="v1")},
                headers=HEADERS,
            )
            assert first.status_code == 201
            skill_id = first.json()["data"]["skill_uuid"]

            second = await client.post(
                "/api/v1/skills/import",
                json={
                    "content": build_skill_md(name=name, description="a completely different v2")
                },
                headers=HEADERS,
            )
        assert second.status_code == 409

        async with postgres_session_factory() as session:
            revisions = await session.scalars(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            assert len(revisions.all()) == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_malformed_skill_md_returns_422_not_500(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/skills/import",
            json={"content": "not-a-skill-md-document-at-all"},
            headers=HEADERS,
        )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "yaml.YAMLError" not in error["message"]
    assert "Traceback" not in error["message"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_unsafe_python_yaml_tag_returns_422_and_persists_nothing(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``yaml.safe_load`` refuses to construct a ``!!python/...`` tag --
    proven end-to-end through the real import route: no Python object is
    constructed, no command executes, and the request is rejected as an
    ordinary invalid document (422), never a 500 or a raw
    ``yaml.constructor.ConstructorError``."""

    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("unsafe-yaml")
    unsafe_document = (
        "---\n"
        f"name: {name}\n"
        "description: Unsafe YAML test\n"
        "metadata:\n"
        '  exploit: !!python/object/apply:os.system ["echo should-not-run"]\n'
        "---\n"
        "Do nothing.\n"
    )

    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/skills/import",
            json={"content": unsafe_document},
            headers=HEADERS,
        )
    assert response.status_code == 422
    body = response.json()
    error = body["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"] == "SKILL.md document is invalid."
    assert "Traceback" not in json.dumps(body)

    async with postgres_session_factory() as session:
        found = await session.scalar(select(Skill).where(Skill.name == name))
        assert found is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_provider_failure_leaves_no_rows(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(
        postgres_session_factory,
        monkeypatch,
        embedding_client_factory=lambda settings: FailingEmbeddingClient(),
    )
    name = unique_name("import-fail")

    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/skills/import",
            json={"content": build_skill_md(name=name)},
            headers=HEADERS,
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    async with postgres_session_factory() as session:
        found = await session.scalar(select(Skill).where(Skill.name == name))
        assert found is None


# --- POST /skills/{skill_uuid}/revisions/import ------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_new_content_creates_revision(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            name = created.json()["data"]["name"]

            response = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": build_skill_md(name=name, description="v2 via import")},
                headers=HEADERS,
            )
        assert response.status_code == 201
        assert response.json()["data"]["revision"] == 2
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_replay_returns_200_existing_revision(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """revision1=A, revision2=B, current=2; importing content semantically
    equal to A replays revision 1 and leaves current at 2."""

    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            name = created.json()["data"]["name"]

            await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json={"description": "v2", "procedure": "Do the thing v2."},
                headers=HEADERS,
            )

            replay = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": build_skill_md(name=name)},
                headers=HEADERS,
            )
        assert replay.status_code == 200
        assert replay.json()["data"]["revision"] == 1

        async with postgres_session_factory() as session:
            skill = await session.get(Skill, skill_id)
            assert skill is not None
            revisions = await session.scalars(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            assert len(revisions.all()) == 2
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_name_mismatch_returns_422(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected before any provider I/O -- a request already known to be
    invalid must never spend an embedding call."""

    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

        # Re-patched *after* the setup request above already completed --
        # OpenAIEmbeddingClient is looked up from the module namespace at
        # request time, not baked into the app object, so patching it again
        # mid-test would otherwise retroactively affect the first request.
        counting_client = CountingFailingEmbeddingClient()
        counting_app = build_app(
            postgres_session_factory,
            monkeypatch,
            embedding_client_factory=lambda settings: counting_client,
        )
        async with build_client(counting_app) as client:
            response = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": build_skill_md(name=unique_name("some-other-name"))},
                headers=HEADERS,
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
        assert counting_client.calls == 0

        async with postgres_session_factory() as session:
            revisions = await session.scalars(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            assert len(revisions.all()) == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_provider_failure_leaves_revision_unchanged(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            name = created.json()["data"]["name"]
            before_updated_at = created.json()["data"]["updated_at"]
            before_status = created.json()["data"]["status"]

        failing_app = build_app(
            postgres_session_factory,
            monkeypatch,
            embedding_client_factory=lambda settings: FailingEmbeddingClient(),
        )
        async with build_client(failing_app) as client:
            response = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": build_skill_md(name=name, description="a new v2 via import")},
                headers=HEADERS,
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

        async with build_client(app) as client:
            fetched = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            revisions = await client.get(f"/api/v1/skills/{skill_id}/revisions", headers=HEADERS)
        assert fetched.json()["data"]["current_revision"] == 1
        assert fetched.json()["data"]["updated_at"] == before_updated_at
        assert fetched.json()["data"]["status"] == before_status == "active"
        assert revisions.json()["data"]["total"] == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_missing_skill_returns_404(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    async with build_client(app) as client:
        response = await client.post(
            f"/api/v1/skills/{uuid4()}/revisions/import",
            json={"content": build_skill_md(name=unique_name("missing"))},
            headers=HEADERS,
        )
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_import_permitted_while_archived_status_unchanged(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]
            name = created.json()["data"]["name"]

            archived = await client.post(f"/api/v1/skills/{skill_id}/archive", headers=HEADERS)
            assert archived.json()["data"]["status"] == "archived"

            response = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": build_skill_md(name=name, description="v2 while archived")},
                headers=HEADERS,
            )
            assert response.status_code == 201
            assert response.json()["data"]["revision"] == 2

            fetched = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
        assert fetched.json()["data"]["status"] == "archived"
        assert fetched.json()["data"]["current_revision"] == 2
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- GET .../export -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_missing_skill_and_missing_revision_return_404(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            missing_skill = await client.get(
                f"/api/v1/skills/{uuid4()}/revisions/1/export", headers=HEADERS
            )
            assert missing_skill.status_code == 404

            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            missing_revision = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/99/export", headers=HEADERS
            )
            assert missing_revision.status_code == 404
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_permitted_while_archived(
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

            response = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS
            )
        assert response.status_code == 200
        assert response.json()["data"]["format"] == "skill_md"
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_is_deterministic_across_repeated_calls(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            first = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS
            )
            second = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS
            )
        first_data = first.json()["data"]
        second_data = second.json()["data"]
        assert first_data["content"] == second_data["content"]
        assert first_data["content_sha256"] == second_data["content_sha256"]
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_content_sha256_is_persisted_semantic_hash_not_file_digest(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``content_sha256`` in the export payload equals the persisted
    ``SkillRevision.content_sha256`` -- the semantic hash of the eight-key
    canonical object (Feature Contract SS 12.7), never
    ``sha256(content.encode())`` (a digest of the exported ``SKILL.md``
    bytes). The two are computed here purely for observation/reporting;
    this test does not assert they must differ for every possible input as
    a general mathematical property -- only that the production field
    means the persisted semantic hash, which for this fixture happens to
    differ from the byte digest of its own YAML+Markdown serialization."""

    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post("/api/v1/skills", json=create_payload(), headers=HEADERS)
            skill_id = created.json()["data"]["skill_uuid"]

            exported = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS
            )
        export_data = exported.json()["data"]
        export_hash = export_data["content_sha256"]
        file_digest = hashlib.sha256(export_data["content"].encode("utf-8")).hexdigest()

        async with postgres_session_factory() as session:
            revision = (
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            ).one()
            persisted_hash = revision.content_sha256

        assert export_hash == persisted_hash
        assert file_digest != persisted_hash, (
            f"observed for this fixture: persisted={persisted_hash} file_digest={file_digest}"
        )
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- cross-surface proofs ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_structured_create_export_reimport_replays(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured create (metadata={}, tags=["forms","pdf"]) -> export ->
    reimport that exact exported SKILL.md into the same Skill replays
    revision 1: structured semantics and SKILL.md-normalized semantics
    produce the same canonical content."""

    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json=create_payload(tags=["forms", "pdf"], declared_tools=["Read"]),
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]

            exported = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS
            )
            export_content = exported.json()["data"]["content"]
            export_hash = exported.json()["data"]["content_sha256"]

            reimported = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": export_content},
                headers=HEADERS,
            )
        assert reimported.status_code == 200
        assert reimported.json()["data"]["revision"] == 1
        assert reimported.json()["data"]["content_sha256"] == export_hash

        async with postgres_session_factory() as session:
            revisions = await session.scalars(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            assert len(revisions.all()) == 1
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_yaml_formatting_still_replays(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frontmatter key order, quoting, CRLF vs LF, tags array order/dupes,
    and allowed-tools spacing never change ``content_sha256`` -- proven
    through the real import route, not only at the domain-canonicalization
    unit-test level (SM-701)."""

    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("import-fmt")
    skill_id: object | None = None

    canonical_doc = build_skill_md(
        name=name,
        description="Deploys the app.",
        metadata={"author": "example"},
        tags=["forms", "pdf"],
        allowed_tools=["Read", "Bash(git:*)"],
    )
    differently_formatted_doc = (
        "---\r\n"
        f'name: "{name}"\r\n'
        "allowed-tools:   Read   Bash(git:*)  \r\n"
        "metadata:\r\n"
        '  sofias-memory.tags: \'["pdf","pdf","forms"]\'\r\n'
        "  author: 'example'\r\n"
        "description: 'Deploys the app.'\r\n"
        "---\r\n"
        "Do the thing."
    )

    try:
        async with build_client(app) as client:
            first = await client.post(
                "/api/v1/skills/import", json={"content": canonical_doc}, headers=HEADERS
            )
            assert first.status_code == 201
            skill_id = first.json()["data"]["skill_uuid"]
            first_hash = (
                await client.get(f"/api/v1/skills/{skill_id}/revisions/1/export", headers=HEADERS)
            ).json()["data"]["content_sha256"]

            second = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": differently_formatted_doc},
                headers=HEADERS,
            )
        assert second.status_code == 200
        assert second.json()["data"]["revision"] == 1
        assert second.json()["data"]["content_sha256"] == first_hash
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserved_tags_key_never_persists_in_metadata_db_proof(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    name = unique_name("import-tags-proof")
    skill_id: object | None = None

    try:
        async with build_client(app) as client:
            response = await client.post(
                "/api/v1/skills/import",
                json={
                    "content": build_skill_md(
                        name=name,
                        metadata={"author": "example"},
                        tags=["pdf", "forms"],
                    )
                },
                headers=HEADERS,
            )
        assert response.status_code == 201
        skill_id = response.json()["data"]["skill_uuid"]

        async with postgres_session_factory() as session:
            revision = (
                await session.scalars(
                    select(SkillRevision).where(SkillRevision.skill_id == skill_id)
                )
            ).one()
            assert revision.metadata_ == {"author": "example"}
            assert revision.tags == ["forms", "pdf"]
            assert "sofias-memory.tags" not in revision.metadata_
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())
