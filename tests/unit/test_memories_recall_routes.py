"""Route-level proof for SM-1003's ``POST /api/v1/memories/recall``: auth,
envelope shape, status codes. ``CognitiveMemoryRecallService`` is faked
here -- no real PostgreSQL or embedding provider is exercised (that is the
job of the real-PostgreSQL/HTTP integration suite)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import httpx
import pytest

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.schemas.memories import (
    MemoryItemResult,
    MemoryProvenanceResult,
    MemoryRecallItem,
    MemoryRecallRequest,
    MemoryRecallResult,
)
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}

VALID_RECALL_BODY = {
    "query": "What interface color does the user prefer?",
    "scopes": ["global"],
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


def fake_memory_item_result() -> MemoryItemResult:
    now = datetime.now(UTC)
    return MemoryItemResult(
        memory_id=uuid4(),
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
    recall_side_effect: Exception | None = None,
    recall_return: MemoryRecallResult | None = None,
) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class FakeCognitiveMemoryRecallService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def recall(self, request: MemoryRecallRequest) -> MemoryRecallResult:
            calls["recall_request"] = request
            if recall_side_effect is not None:
                raise recall_side_effect
            if recall_return is not None:
                return recall_return
            return MemoryRecallResult(
                items=[
                    MemoryRecallItem(
                        memory=fake_memory_item_result(), relevance=0.87, is_current_truth=True
                    )
                ]
            )

    monkeypatch.setattr(
        "sofias_memory.api.routes.memories.CognitiveMemoryRecallService",
        FakeCognitiveMemoryRecallService,
    )
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


def json_object(response: httpx.Response) -> dict[str, object]:
    return cast(dict[str, object], response.json())


@pytest.mark.asyncio
async def test_recall_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/memories/recall", json=VALID_RECALL_BODY)

    assert response.status_code == 401
    assert json_object(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_recall_returns_200_with_success_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories/recall", headers=HEADERS, json=VALID_RECALL_BODY
        )

    assert response.status_code == 200
    body = json_object(response)
    assert set(body) == {"data", "meta"}
    data = cast(dict[str, object], body["data"])
    items = cast(list[dict[str, object]], data["items"])
    assert len(items) == 1
    assert items[0]["relevance"] == 0.87
    assert items[0]["is_current_truth"] is True
    assert "embedding" not in cast(dict[str, object], items[0]["memory"])
    assert "recall_request" in calls


@pytest.mark.asyncio
async def test_recall_rejects_missing_scopes_with_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories/recall",
            headers=HEADERS,
            json={"query": "x", "scopes": []},
        )

    assert response.status_code == 422
    assert json_object(response)["error"]["code"] == "INVALID_REQUEST"
    assert "recall_request" not in calls


@pytest.mark.asyncio
async def test_recall_rejects_future_as_of_with_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories/recall",
            headers=HEADERS,
            json={**VALID_RECALL_BODY, "as_of": "2999-01-01T00:00:00Z"},
        )

    assert response.status_code == 422
    assert "recall_request" not in calls


@pytest.mark.asyncio
async def test_recall_dependency_unavailable_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(
        monkeypatch, recall_side_effect=DependencyUnavailableError("Embedding provider down.")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories/recall", headers=HEADERS, json=VALID_RECALL_BODY
        )

    assert response.status_code == 503
    assert json_object(response)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_recall_empty_items_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch, recall_return=MemoryRecallResult(items=[]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/memories/recall", headers=HEADERS, json=VALID_RECALL_BODY
        )

    assert response.status_code == 200
    assert json_object(response)["data"]["items"] == []
