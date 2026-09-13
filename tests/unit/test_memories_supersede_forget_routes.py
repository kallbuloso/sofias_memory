"""Route-level proof for SM-1004's ``POST /api/v1/memories/{memory_id}/supersede``
and ``POST /api/v1/memories/{memory_id}/forget``: auth, envelope shape,
status codes, and Idempotency-Key passthrough. ``CognitiveMemoryService``
is faked here -- no real PostgreSQL or embedding provider is exercised
(that is the job of the real-PostgreSQL/HTTP integration suite)."""

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
    MemoryItemResult,
    MemoryProvenanceResult,
    MemorySupersedeRequest,
    MemorySupersedeResult,
)
from sofias_memory.services.cognitive_memory import memory_state_conflict_error
from sofias_memory.services.pipeline_submission import idempotency_conflict_error
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}

VALID_SUPERSEDE_BODY = {
    "content": "The user now prefers dark mode.",
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


def fake_result(
    memory_id: UUID | None = None,
    *,
    lifecycle: str = "active",
    content: str | None = "Prefers teal interfaces.",
) -> MemoryItemResult:
    now = datetime.now(UTC)
    return MemoryItemResult(
        memory_id=memory_id or uuid4(),
        memory_type="profile",  # type: ignore[arg-type]
        scope="global" if lifecycle != "forgotten" else None,
        content=content if lifecycle != "forgotten" else None,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        confidence=None,
        valid_from=None,
        valid_until=None,
        created_at=now,
        superseded_at=None,
        superseded_by=None,
        forgotten_at=now if lifecycle == "forgotten" else None,
        provenance=MemoryProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="sofias-assistant",
            conversation_uuid=None,
            turn_uuid=uuid4() if lifecycle != "forgotten" else None,
            task_uuid=None,
            confirmation_ref=None,
            source_ref=None,
            observed_at=None,
        ),
    )


def build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supersede_side_effect: Exception | None = None,
    supersede_return: MemorySupersedeResult | None = None,
    forget_side_effect: Exception | None = None,
    forget_return: MemoryItemResult | None = None,
) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class FakeCognitiveMemoryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def supersede(
            self,
            memory_id: UUID,
            request: MemorySupersedeRequest,
            *,
            idempotency_key: str | None,
        ) -> MemorySupersedeResult:
            calls["supersede_memory_id"] = memory_id
            calls["supersede_request"] = request
            calls["supersede_idempotency_key"] = idempotency_key
            if supersede_side_effect is not None:
                raise supersede_side_effect
            if supersede_return is not None:
                return supersede_return
            return MemorySupersedeResult(
                old=fake_result(memory_id, lifecycle="superseded"),
                replacement=fake_result(),
            )

        async def forget(self, memory_id: UUID, *, idempotency_key: str | None) -> MemoryItemResult:
            calls["forget_memory_id"] = memory_id
            calls["forget_idempotency_key"] = idempotency_key
            if forget_side_effect is not None:
                raise forget_side_effect
            if forget_return is not None:
                return forget_return
            return fake_result(memory_id, lifecycle="forgotten")

    monkeypatch.setattr(
        "sofias_memory.api.routes.memories.CognitiveMemoryService", FakeCognitiveMemoryService
    )
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


def json_object(response: httpx.Response) -> dict[str, object]:
    return cast(dict[str, object], response.json())


# --- Supersede ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede", json=VALID_SUPERSEDE_BODY
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_supersede_returns_200_with_old_and_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede", headers=HEADERS, json=VALID_SUPERSEDE_BODY
        )

    assert response.status_code == 200
    body = json_object(response)
    data = cast(dict[str, object], body["data"])
    assert set(data) == {"old", "replacement"}
    old = cast(dict[str, object], data["old"])
    replacement = cast(dict[str, object], data["replacement"])
    assert old["lifecycle"] == "superseded"
    assert replacement["lifecycle"] == "active"
    assert "embedding" not in old
    assert "embedding" not in replacement
    assert calls["supersede_memory_id"] == memory_id
    assert calls["supersede_idempotency_key"] is None


@pytest.mark.asyncio
async def test_supersede_passes_idempotency_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede",
            headers={**HEADERS, "Idempotency-Key": "sup-key-1"},
            json=VALID_SUPERSEDE_BODY,
        )

    assert response.status_code == 200
    assert calls["supersede_idempotency_key"] == "sup-key-1"


@pytest.mark.asyncio
async def test_supersede_rejects_memory_type_field_with_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede",
            headers=HEADERS,
            json={**VALID_SUPERSEDE_BODY, "memory_type": "profile"},
        )

    assert response.status_code == 422
    assert "supersede_request" not in calls


@pytest.mark.asyncio
async def test_supersede_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    not_found = SofiasMemoryError(
        code=ErrorCode.MEMORY_NOT_FOUND, status_code=404, message="MemoryItem does not exist."
    )
    app, _ = build_app(monkeypatch, supersede_side_effect=not_found)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede", headers=HEADERS, json=VALID_SUPERSEDE_BODY
        )

    assert response.status_code == 404
    assert json_object(response)["error"]["code"] == "MEMORY_NOT_FOUND"


@pytest.mark.asyncio
async def test_supersede_state_conflict_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    memory_id = uuid4()
    app, _ = build_app(monkeypatch, supersede_side_effect=memory_state_conflict_error(memory_id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede", headers=HEADERS, json=VALID_SUPERSEDE_BODY
        )

    assert response.status_code == 409
    assert json_object(response)["error"]["code"] == "MEMORY_STATE_CONFLICT"


@pytest.mark.asyncio
async def test_supersede_idempotency_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch, supersede_side_effect=idempotency_conflict_error())
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede",
            headers={**HEADERS, "Idempotency-Key": "dup-key"},
            json=VALID_SUPERSEDE_BODY,
        )

    assert response.status_code == 409
    assert json_object(response)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_supersede_dependency_unavailable_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(
        monkeypatch, supersede_side_effect=DependencyUnavailableError("Embedding provider down.")
    )
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/supersede", headers=HEADERS, json=VALID_SUPERSEDE_BODY
        )

    assert response.status_code == 503
    assert json_object(response)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


# --- Forget ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(f"/api/v1/memories/{memory_id}/forget")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_forget_returns_200_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(f"/api/v1/memories/{memory_id}/forget", headers=HEADERS)

    assert response.status_code == 200
    data = cast(dict[str, object], json_object(response)["data"])
    assert data["memory_id"] == str(memory_id)
    assert data["lifecycle"] == "forgotten"
    assert data["content"] is None
    assert data["scope"] is None
    assert "embedding" not in data
    assert calls["forget_memory_id"] == memory_id
    assert calls["forget_idempotency_key"] is None


@pytest.mark.asyncio
async def test_forget_accepts_no_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/forget", headers=HEADERS, content=b""
        )

    assert response.status_code == 200
    assert "forget_memory_id" in calls


@pytest.mark.asyncio
async def test_forget_passes_idempotency_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/forget",
            headers={**HEADERS, "Idempotency-Key": "forget-key-1"},
        )

    assert response.status_code == 200
    assert calls["forget_idempotency_key"] == "forget-key-1"


@pytest.mark.asyncio
async def test_forget_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    not_found = SofiasMemoryError(
        code=ErrorCode.MEMORY_NOT_FOUND, status_code=404, message="MemoryItem does not exist."
    )
    app, _ = build_app(monkeypatch, forget_side_effect=not_found)
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(f"/api/v1/memories/{memory_id}/forget", headers=HEADERS)

    assert response.status_code == 404
    assert json_object(response)["error"]["code"] == "MEMORY_NOT_FOUND"


@pytest.mark.asyncio
async def test_forget_idempotency_conflict_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch, forget_side_effect=idempotency_conflict_error())
    memory_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/memories/{memory_id}/forget",
            headers={**HEADERS, "Idempotency-Key": "dup-key"},
        )

    assert response.status_code == 409
    assert json_object(response)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
