"""Route-level proof for the SM-704 ``POST /skills/resolve`` route: the
static ``resolve`` segment is never captured by the dynamic
``{skill_uuid}`` path parameter, and the envelope shape matches the frozen
contract. ``SkillService`` is faked -- no real PostgreSQL or embedding
provider is exercised here (that is the job of the real-PostgreSQL/HTTP
integration suite)."""

from __future__ import annotations

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.schemas.skills import SkillResolveRequest, SkillResolveResult
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


def build_app(monkeypatch: pytest.MonkeyPatch) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class FakeSkillService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def resolve(self, request: SkillResolveRequest) -> SkillResolveResult:
            calls["resolve"] = request
            return SkillResolveResult(matches=[])

    monkeypatch.setattr("sofias_memory.api.routes.skills.SkillService", FakeSkillService)
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


@pytest.mark.asyncio
async def test_resolve_route_is_not_shadowed_by_dynamic_skill_uuid_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills/resolve",
            headers=HEADERS,
            json={"query": "how do I deploy?"},
        )

    assert response.status_code == 200
    assert "resolve" in calls
    request = calls["resolve"]
    assert isinstance(request, SkillResolveRequest)
    assert request.query == "how do I deploy?"
    body = response.json()
    assert body["data"]["matches"] == []


@pytest.mark.asyncio
async def test_resolve_route_returns_envelope_with_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills/resolve",
            headers=HEADERS,
            json={"query": "deploy", "top_k": 3},
        )

    body = response.json()
    assert body["meta"]["request_id"]


@pytest.mark.asyncio
async def test_resolve_route_rejects_invalid_body_with_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills/resolve",
            headers=HEADERS,
            json={"query": "   ", "top_k": 21},
        )

    assert response.status_code == 422
    assert "resolve" not in calls
