"""Real-PostgreSQL SM-705 cross-feature hardening for Skills.

Proves structural and behavioral isolation between Skills and the rest of
Sofias Memory: Session/SessionEntry carry no Skill column/FK and neither
feature's lifecycle affects the other; no PostgreSQL/Neo4j-ownership column
or FK exists on ``skills``/``skill_revisions`` for any other aggregate; a
comprehensive matrix of Skill write/read operations never writes to
``graph_outbox``; the archive/restore contract (discovery filter, not an
admission barrier; the current-revision pointer at restore time; full
idempotency) holds under the real public HTTP API. Requires migrations
already applied through 0014.
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
from sofias_memory.domain import SkillStatus
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    GraphOutbox,
    Skill,
    SkillRevision,
)
from sofias_memory.infrastructure.postgres.models import (
    Session as SessionModel,
)
from sofias_memory.infrastructure.postgres.models import (
    SessionEntry as SessionEntryModel,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.interoperability.skill_md import parse_skill_markdown, serialize_skill_markdown
from sofias_memory.schemas.session_entries import SessionEntryCreateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillResolveRequest,
    SkillRevisionCreateRequest,
    SkillUpdateRequest,
)
from sofias_memory.services.session_entries import SessionEntryService
from sofias_memory.services.sessions import SessionService
from sofias_memory.services.skills import SkillService
from tests.unit._app_factory import create_app

POSTGRES_SKILLS_CROSS_FEATURE_ENV = "SOFIAS_MEMORY_RUN_SKILLS_CROSS_FEATURE_POSTGRES_TESTS"

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
        "agent_id",
        "owner_id",
        "tenant_id",
        "user_id",
    }
)
OTHER_AGGREGATE_TABLES = ("datasets", "sessions", "session_entries", "sources", "documents")


class FakeEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _cross_feature_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_SKILLS_CROSS_FEATURE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SKILLS_CROSS_FEATURE_ENV}=1 to run this suite")
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
        await session.execute(delete(SessionModel).where(SessionModel.id.in_(session_ids)))
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


def build_app(
    session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
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


# --- 19: cross-feature database constraint audit -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_tables_have_no_skill_columns_or_fks(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        columns = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('sessions', 'session_entries') "
                "AND (column_name ILIKE '%skill%')"
            )
        )
        assert columns.all() == []

        fks = await session.execute(
            text(
                "SELECT tc.table_name, ccu.table_name AS foreign_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND tc.table_name IN ('sessions', 'session_entries') "
                "AND ccu.table_name IN ('skills', 'skill_revisions')"
            )
        )
        assert fks.all() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_tables_have_no_forbidden_ownership_columns_or_fks(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async with postgres_session_factory() as session:
        columns = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('skills', 'skill_revisions')"
            )
        )
        found_columns = {row.column_name for row in columns.all()}
        forbidden_present = found_columns & FORBIDDEN_OWNERSHIP_COLUMNS
        assert forbidden_present == set(), forbidden_present

        fks_from_skills = await session.execute(
            text(
                "SELECT tc.table_name, ccu.table_name AS foreign_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND tc.table_name IN ('skills', 'skill_revisions') "
                f"AND ccu.table_name IN {OTHER_AGGREGATE_TABLES!r}"
            )
        )
        assert fks_from_skills.all() == []

        fks_to_skills = await session.execute(
            text(
                "SELECT tc.table_name, ccu.table_name AS foreign_table "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                f"AND tc.table_name IN {OTHER_AGGREGATE_TABLES!r} "
                "AND ccu.table_name IN ('skills', 'skill_revisions')"
            )
        )
        assert fks_to_skills.all() == []


# --- 9/10: Session/SessionEntry structural + behavioral isolation ------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_and_skill_lifecycles_do_not_affect_each_other(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session_ids: set[Any] = set()
    skill_ids: set[Any] = set()

    try:
        session_result = await SessionService(
            session_factory=postgres_session_factory
        ).create_session(SessionCreateRequest())
        session_ids.add(session_result.session_uuid)
        entry_result = await SessionEntryService(
            session_factory=postgres_session_factory
        ).append_entry(
            session_result.session_uuid,
            SessionEntryCreateRequest(role="user", content="hello"),
        )

        resolver = skill_service(postgres_session_factory)
        created = await resolver.create_skill(
            SkillCreateRequest(name=unique_name("cross-feature"), description="d", procedure="p")  # type: ignore[call-arg]
        )
        skill_ids.add(created.skill_uuid)

        async def session_snapshot() -> tuple[object, object]:
            async with postgres_session_factory() as session:
                sess = await session.get(SessionModel, session_result.session_uuid)
                entry = await session.get(SessionEntryModel, entry_result.entry_id)
                return (
                    (sess.status, sess.updated_at, sess.archived_at) if sess else None,
                    (entry.role, entry.content) if entry else None,
                )

        before_session = await session_snapshot()

        # Full Skill lifecycle: revision, rollback, archive, restore, resolve, export.
        await resolver.create_revision(
            created.skill_uuid,
            SkillRevisionCreateRequest(description="d2", procedure="p2"),  # type: ignore[call-arg]
        )
        await resolver.update_skill(created.skill_uuid, SkillUpdateRequest(current_revision=1))
        await resolver.archive_skill(created.skill_uuid)
        await resolver.restore_skill(created.skill_uuid)
        await resolver.resolve(SkillResolveRequest(query="anything", top_k=5))
        await resolver.export_revision(created.skill_uuid, 1)

        after_session = await session_snapshot()
        assert before_session == after_session

        async def skill_snapshot() -> tuple[object, ...]:
            async with postgres_session_factory() as session:
                skill = await session.get(Skill, created.skill_uuid)
                assert skill is not None
                return (skill.status, skill.current_revision_id, skill.updated_at)

        before_skill = await skill_snapshot()

        session_service = SessionService(session_factory=postgres_session_factory)
        entry_service = SessionEntryService(session_factory=postgres_session_factory)
        await entry_service.append_entry(
            session_result.session_uuid,
            SessionEntryCreateRequest(role="assistant", content="world"),
        )
        await session_service.archive_session(session_result.session_uuid)
        await session_service.restore_session(session_result.session_uuid)

        after_skill = await skill_snapshot()
        assert before_skill == after_skill
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)
        await cleanup_sessions(postgres_session_factory, session_ids)


# --- 7: comprehensive Skill-operations graph_outbox matrix -------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_outbox_delta_zero_across_full_skill_operations_matrix(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    resolver = skill_service(postgres_session_factory)
    skill_ids: set[Any] = set()

    async def outbox_count() -> int:
        async with postgres_session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(GraphOutbox)) or 0)

    try:
        before = await outbox_count()

        created = await resolver.create_skill(
            SkillCreateRequest(name=unique_name("outbox"), description="d", procedure="p")  # type: ignore[call-arg]
        )
        skill_ids.add(created.skill_uuid)
        skill_id = created.skill_uuid

        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]
        # safe replay: identical content to the just-created revision.
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]
        await resolver.update_skill(skill_id, SkillUpdateRequest(current_revision=1))
        await resolver.archive_skill(skill_id)
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d3", procedure="p3")
        )  # type: ignore[call-arg]
        await resolver.restore_skill(skill_id)

        exported = await resolver.export_revision(skill_id, 1)
        parsed = parse_skill_markdown(exported.content)
        import_name = unique_name("outbox-import")
        new_document = serialize_skill_markdown(name=import_name, content=parsed.content)
        imported = await resolver.import_skill(new_document)
        skill_ids.add(imported.skill_uuid)

        content = await resolver.export_revision(imported.skill_uuid, 1)
        await resolver.import_revision(imported.skill_uuid, content.content)  # replay

        await resolver.list_skills(limit=10, offset=0, status=None)
        await resolver.get_skill(skill_id)
        await resolver.list_revisions(skill_id, limit=10, offset=0)
        await resolver.get_revision(skill_id, 1)
        await resolver.export_revision(skill_id, 1)
        await resolver.resolve(SkillResolveRequest(query="anything", top_k=5))

        after = await outbox_count()
        assert after - before == 0
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)


# --- 11: complete archived-management matrix via real HTTP -------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_management_matrix_via_real_http(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None
    name = unique_name("archive-matrix")

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json={"name": name, "description": "d", "procedure": "p"},
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]
            await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json={"description": "d2", "procedure": "p2"},
                headers=HEADERS,
            )
            # current = revision 2 at this point.
            archived = await client.post(f"/api/v1/skills/{skill_id}/archive", headers=HEADERS)
            assert archived.json()["data"]["status"] == "archived"

            get_skill = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            assert get_skill.status_code == 200
            get_revisions = await client.get(
                f"/api/v1/skills/{skill_id}/revisions", headers=HEADERS
            )
            assert get_revisions.status_code == 200
            get_revision = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/1", headers=HEADERS
            )
            assert get_revision.status_code == 200
            export = await client.get(
                f"/api/v1/skills/{skill_id}/revisions/2/export", headers=HEADERS
            )
            assert export.status_code == 200
            patch = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"current_revision": 1},
                headers=HEADERS,
            )
            assert patch.status_code == 200
            assert patch.json()["data"]["status"] == "archived"

            structured_revision = await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json={"description": "d3", "procedure": "p3"},
                headers=HEADERS,
            )
            assert structured_revision.status_code == 201

            export_content = (
                await client.get(f"/api/v1/skills/{skill_id}/revisions/3/export", headers=HEADERS)
            ).json()["data"]["content"]
            import_replay = await client.post(
                f"/api/v1/skills/{skill_id}/revisions/import",
                json={"content": export_content},
                headers=HEADERS,
            )
            assert import_replay.status_code == 200

            resolved = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 20},
                headers=HEADERS,
            )
            matches = resolved.json()["data"]["matches"]
            assert skill_id not in [m["skill_uuid"] for m in matches]

            still_archived = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            assert still_archived.json()["data"]["status"] == "archived"
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- 12: pointer-at-restore-time full scenario --------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pointer_at_restore_time_full_scenario(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    skill_id: object | None = None
    name = unique_name("pointer-restore")

    try:
        async with build_client(app) as client:
            created = await client.post(
                "/api/v1/skills",
                json={"name": name, "description": "d1", "procedure": "p1"},
                headers=HEADERS,
            )
            skill_id = created.json()["data"]["skill_uuid"]
            await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json={"description": "d2", "procedure": "p2"},
                headers=HEADERS,
            )
            # current = revision 2.

            archived = await client.post(f"/api/v1/skills/{skill_id}/archive", headers=HEADERS)
            assert archived.json()["data"]["current_revision"] == 2
            assert archived.json()["data"]["status"] == "archived"

            rev3 = await client.post(
                f"/api/v1/skills/{skill_id}/revisions",
                json={"description": "d3", "procedure": "p3"},
                headers=HEADERS,
            )
            assert rev3.status_code == 201
            assert rev3.json()["data"]["revision"] == 3

            still_archived = await client.get(f"/api/v1/skills/{skill_id}", headers=HEADERS)
            assert still_archived.json()["data"]["status"] == "archived"
            assert still_archived.json()["data"]["current_revision"] == 3

            resolved_while_archived = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 20},
                headers=HEADERS,
            )
            assert skill_id not in [
                m["skill_uuid"] for m in resolved_while_archived.json()["data"]["matches"]
            ]

            restored = await client.post(f"/api/v1/skills/{skill_id}/restore", headers=HEADERS)
            assert restored.json()["data"]["status"] == "active"
            assert restored.json()["data"]["current_revision"] == 3

            resolved_after_restore = await client.post(
                "/api/v1/skills/resolve",
                json={"query": "anything", "top_k": 20},
                headers=HEADERS,
            )
            match = next(
                m
                for m in resolved_after_restore.json()["data"]["matches"]
                if m["skill_uuid"] == skill_id
            )
            assert match["current_revision"] == 3
            assert match["description"] == "d3"
    finally:
        await cleanup_skills(postgres_session_factory, {skill_id} if skill_id else set())


# --- 13: archive/restore idempotency ------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_restore_idempotency_preserves_state(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    resolver = skill_service(postgres_session_factory)
    skill_ids: set[Any] = set()

    try:
        created = await resolver.create_skill(
            SkillCreateRequest(name=unique_name("idempotent"), description="d", procedure="p")  # type: ignore[call-arg]
        )
        skill_id = created.skill_uuid
        skill_ids.add(skill_id)
        await resolver.create_revision(
            skill_id, SkillRevisionCreateRequest(description="d2", procedure="p2")
        )  # type: ignore[call-arg]

        first_archive = await resolver.archive_skill(skill_id)
        assert first_archive.status == SkillStatus.ARCHIVED
        archived_at = first_archive.archived_at
        current_revision = first_archive.current_revision
        updated_at = first_archive.updated_at

        second_archive = await resolver.archive_skill(skill_id)
        assert second_archive.status == SkillStatus.ARCHIVED
        assert second_archive.archived_at == archived_at
        assert second_archive.current_revision == current_revision
        assert second_archive.updated_at == updated_at

        async with postgres_session_factory() as session:
            revisions_before = await session.execute(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            revision_count_before = len(revisions_before.all())

        first_restore = await resolver.restore_skill(skill_id)
        assert first_restore.status == SkillStatus.ACTIVE
        assert first_restore.current_revision == current_revision
        restored_updated_at = first_restore.updated_at

        second_restore = await resolver.restore_skill(skill_id)
        assert second_restore.status == SkillStatus.ACTIVE
        assert second_restore.archived_at is None
        assert second_restore.current_revision == current_revision
        assert second_restore.updated_at == restored_updated_at

        async with postgres_session_factory() as session:
            revisions_after = await session.execute(
                select(SkillRevision).where(SkillRevision.skill_id == skill_id)
            )
            revision_count_after = len(revisions_after.all())
        assert revision_count_before == revision_count_after
    finally:
        await cleanup_skills(postgres_session_factory, skill_ids)
