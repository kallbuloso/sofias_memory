"""Route-level proof for the SM-703 SKILL.md import/export routes: static
segments (``import``/``export``) are never captured by a dynamic
``{skill_uuid}``/``{revision}`` path parameter, and the envelope/status-code
shape matches the frozen contract. ``SkillService`` is faked -- no real
PostgreSQL or embedding provider is exercised here (that is the job of the
real-PostgreSQL/HTTP integration suite)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import SkillStatus
from sofias_memory.schemas.skills import SkillExportResult, SkillResult, SkillRevisionResult
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def fake_skill_result(skill_uuid: UUID) -> SkillResult:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SkillResult(
        skill_uuid=skill_uuid,
        name="deploy-app",
        description="d",
        status=SkillStatus.ACTIVE,
        current_revision=1,
        compatibility=None,
        tags=[],
        declared_tools=[],
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


def fake_revision_result(skill_uuid: UUID, revision: int) -> SkillRevisionResult:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SkillRevisionResult(
        skill_uuid=skill_uuid,
        revision=revision,
        description="d",
        procedure="p",
        license=None,
        compatibility=None,
        metadata={},
        tags=[],
        declared_tools=[],
        content_sha256="0" * 64,
        created_at=now,
    )


def build_app(monkeypatch: pytest.MonkeyPatch) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class FakeSkillService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def import_skill(self, raw_content: str) -> SkillResult:
            calls["import_skill"] = raw_content
            return fake_skill_result(uuid4())

        async def import_revision(
            self, skill_uuid: UUID, raw_content: str
        ) -> tuple[SkillRevisionResult, bool]:
            calls["import_revision"] = (skill_uuid, raw_content)
            return fake_revision_result(skill_uuid, 2), True

        async def export_revision(self, skill_uuid: UUID, revision: int) -> SkillExportResult:
            calls["export_revision"] = (skill_uuid, revision)
            return SkillExportResult(
                format="skill_md", content="---\n---\np", content_sha256="0" * 64
            )

    monkeypatch.setattr("sofias_memory.api.routes.skills.SkillService", FakeSkillService)
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


@pytest.mark.asyncio
async def test_skills_import_route_is_not_shadowed_by_dynamic_skill_uuid_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills/import", headers=HEADERS, json={"content": "---\nname: x\n---\np"}
        )

    assert response.status_code == 201
    assert "import_skill" in calls
    body = response.json()
    assert "detail" not in body
    assert body["data"]["name"] == "deploy-app"


@pytest.mark.asyncio
async def test_skills_revisions_import_route_is_not_shadowed_by_dynamic_revision_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    skill_uuid = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/skills/{skill_uuid}/revisions/import",
            headers=HEADERS,
            json={"content": "---\nname: x\n---\np"},
        )

    assert response.status_code == 201
    assert "import_revision" in calls
    assert calls["import_revision"][0] == skill_uuid
    body = response.json()
    assert body["data"]["revision"] == 2


@pytest.mark.asyncio
async def test_skills_revisions_import_replay_returns_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)

    class ReplayFakeSkillService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def import_revision(
            self, skill_uuid: UUID, raw_content: str
        ) -> tuple[SkillRevisionResult, bool]:
            return fake_revision_result(skill_uuid, 1), False

    monkeypatch.setattr("sofias_memory.api.routes.skills.SkillService", ReplayFakeSkillService)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/skills/{uuid4()}/revisions/import",
            headers=HEADERS,
            json={"content": "---\nname: x\n---\np"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_skills_export_route_reaches_export_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    skill_uuid = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/skills/{skill_uuid}/revisions/3/export", headers=HEADERS
        )

    assert response.status_code == 200
    assert calls["export_revision"] == (skill_uuid, 3)
    body = response.json()
    assert body["data"]["format"] == "skill_md"
    assert body["data"]["content_sha256"] == "0" * 64
