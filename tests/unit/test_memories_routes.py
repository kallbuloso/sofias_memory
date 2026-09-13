"""Route-level proof for SM-1002's ``POST /api/v1/memories`` and
``GET /api/v1/memories/{memory_id}``: auth, envelope shape, status codes,
and Idempotency-Key passthrough. ``CognitiveMemoryService`` is faked here --
no real PostgreSQL or embedding provider is exercised (that is the job of
the real-PostgreSQL/HTTP integration suite)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.memories import (
    MemoryCreateRequest,
    MemoryItemResult,
    MemoryProvenanceResult,
)
from sofias_memory.services.pipeline_submission import idempotency_conflict_error
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}

VALID_CREATE_BODY = {
    "memory_type": "profile",
    "scope": "global",
    "content": "Prefers teal interfaces.",
    "provenance": {
        "origin_kind": "user_asserted",
        "source_system": "sofias-assistant",
        "turn_uuid": str(uuid4()),
    },
}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "cognitive_idempotency_hmac_key": COGNITIVE_IDEMPOTENCY_HMAC_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def fake_result(memory_id: UUID | None = None) -> MemoryItemResult:
    now = datetime.now(UTC)
    return MemoryItemResult(
        memory_id=memory_id or uuid4(),
        memory_type="profile",  # type: ignore[arg-type]
        scope="global",
        content="Prefers teal interfaces.",
        lifecycle="active",  # type: ignore[arg-type]
        confidence=None,
        valid_from=None,
        valid_until=None,
        created_at=now,
        superseded_at=None,
        superseded_by=None,
        forgotten_at=None,
        provenance=MemoryProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="sofias-assistant",
            conversation_uuid=None,
            turn_uuid=uuid4(),
            task_uuid=None,
            confirmation_ref=None,
            source_ref=None,
            observed_at=None,
        ),
    )


def build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_side_effect: Exception | None = None,
    get_side_effect: Exception | None = None,
) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class FakeCognitiveMemoryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def create(
            self, request: MemoryCreateRequest, *, idempotency_key: str | None
        ) -> MemoryItemResult:
            calls["create_request"] = request
            calls["idempotency_key"] = idempotency_key
            if create_side_effect is not None:
                raise create_side_effect
            return fake_result()

        async def get(self, memory_id: UUID) -> MemoryItemResult:
            calls["get_memory_id"] = memory_id
            if get_side_effect is not None:
                raise get_side_effect
            return fake_result(memory_id)

    monkeypatch.setattr(
        "sofias_memory.api.routes.memories.CognitiveMemoryService", FakeCognitiveMemoryService
    )
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


def json_object(response: httpx.Response) -> dict[str, object]:
    return cast(dict[str, object], response.json())


@pytest.mark.asyncio
async def test_create_memory_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/memories", json=VALID_CREATE_BODY)

    assert response.status_code == 401
    assert json_object(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_create_memory_returns_201_with_success_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/memories", headers=HEADERS, json=VALID_CREATE_BODY)

    assert response.status_code == 201
    body = json_object(response)
    assert set(body) == {"data", "meta"}
    data = cast(dict[str, object], body["data"])
    assert "embedding" not in data
    assert data["lifecycle"] == "active"
    assert "create_request" in calls
    assert calls["idempotency_key"] is None


@pytest.mark.asyncio
async def test_create_memory_passes_idempotency_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories",
            headers={**HEADERS, "Idempotency-Key": "caller-key-1"},
            json=VALID_CREATE_BODY,
        )

    assert response.status_code == 201
    assert calls["idempotency_key"] == "caller-key-1"


@pytest.mark.asyncio
async def test_create_memory_rejects_invalid_body_with_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories",
            headers=HEADERS,
            json={**VALID_CREATE_BODY, "memory_type": "episodic"},
        )

    assert response.status_code == 422
    assert json_object(response)["error"]["code"] == "INVALID_REQUEST"
    assert "create_request" not in calls


@pytest.mark.asyncio
async def test_create_memory_idempotency_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch, create_side_effect=idempotency_conflict_error())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories",
            headers={**HEADERS, "Idempotency-Key": "dup-key"},
            json=VALID_CREATE_BODY,
        )

    assert response.status_code == 409
    assert json_object(response)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_create_memory_dependency_unavailable_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(
        monkeypatch, create_side_effect=DependencyUnavailableError("Embedding provider down.")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/memories", headers=HEADERS, json=VALID_CREATE_BODY)

    assert response.status_code == 503
    assert json_object(response)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_get_memory_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/memories/{uuid4()}")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_memory_returns_200_with_success_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)

    assert response.status_code == 200
    body = json_object(response)
    data = cast(dict[str, object], body["data"])
    assert data["memory_id"] == str(memory_id)
    assert calls["get_memory_id"] == memory_id


@pytest.mark.asyncio
async def test_get_memory_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    not_found = SofiasMemoryError(
        code=ErrorCode.MEMORY_NOT_FOUND,
        status_code=404,
        message="MemoryItem does not exist.",
    )
    app, _ = build_app(monkeypatch, get_side_effect=not_found)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/memories/{uuid4()}", headers=HEADERS)

    assert response.status_code == 404
    assert json_object(response)["error"]["code"] == "MEMORY_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_memory_malformed_uuid_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/memories/not-a-uuid", headers=HEADERS)

    assert response.status_code == 422
    assert "get_memory_id" not in calls
