"""Route-level proof for the SM-CTX-001 Episode HTTP surface: auth, envelope,
status codes, required Idempotency-Key, stable Contract v1 validation codes.
``EpisodeService`` is faked -- no PostgreSQL or embedding provider here (see
the real-PostgreSQL integration suite)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.schemas.episodes import (
    EpisodeListResult,
    EpisodeProvenanceResult,
    EpisodeResult,
)
from sofias_memory.services.episodes import episode_not_found_error, episode_state_conflict_error
from sofias_memory.services.pipeline_submission import idempotency_conflict_error
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}
KEYED = {**HEADERS, "Idempotency-Key": "key-1"}

CREATE_BODY: dict[str, Any] = {
    "scope": "project:contextual-memory-validation",
    "context_key": "feature:v1",
    "kind": "decision",
    "summary": "Episode foi escolhido como agregado first-class.",
    "occurred_at": "2026-11-18T20:00:00Z",
    "provenance": {"origin_kind": "user_asserted", "source_system": "manual-validation"},
}


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory",
        neo4j_password="fake-neo4j-password",
        llm_api_key="sk-fake-test-key",
        cognitive_idempotency_hmac_key="test-cognitive-idempotency-hmac-key-0123456789abcdef",
        app_env="test",
    )


def fake_result(episode_id: UUID | None = None) -> EpisodeResult:
    return EpisodeResult(
        episode_id=episode_id or uuid4(),
        scope="project:contextual-memory-validation",
        context_key="feature:v1",
        kind="decision",  # type: ignore[arg-type]
        summary="S",
        situation=None,
        reason=None,
        outcome=None,
        occurred_at=datetime(2026, 11, 18, 20, 0, tzinfo=UTC),
        lifecycle="active",  # type: ignore[arg-type]
        confidence=None,
        provenance=EpisodeProvenanceResult(
            origin_kind="user_asserted",  # type: ignore[arg-type]
            source_system="manual-validation",
            conversation_ref=None,
            turn_ref=None,
        ),
        created_at=datetime.now(UTC),
        invalidated_at=None,
        forgotten_at=None,
    )


def build_app(
    monkeypatch: pytest.MonkeyPatch, *, side_effect: Exception | None = None
) -> tuple[Any, dict[str, Any]]:
    calls: dict[str, Any] = {}

    class FakeEpisodeService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def create(self, request: Any, *, idempotency_key: str) -> EpisodeResult:
            calls["create"] = (request, idempotency_key)
            if side_effect is not None:
                raise side_effect
            return fake_result()

        async def get(self, episode_id: UUID) -> EpisodeResult:
            calls["get"] = episode_id
            if side_effect is not None:
                raise side_effect
            return fake_result(episode_id)

        async def list_episodes(self, **kwargs: Any) -> EpisodeListResult:
            calls["list"] = kwargs
            return EpisodeListResult(items=[fake_result()], limit=kwargs["limit"], next_cursor=None)

        async def invalidate(
            self, episode_id: UUID, request: Any, *, idempotency_key: str
        ) -> EpisodeResult:
            calls["invalidate"] = (episode_id, request, idempotency_key)
            if side_effect is not None:
                raise side_effect
            return fake_result(episode_id)

        async def forget(self, episode_id: UUID, *, idempotency_key: str) -> EpisodeResult:
            calls["forget"] = (episode_id, idempotency_key)
            if side_effect is not None:
                raise side_effect
            return fake_result(episode_id)

    monkeypatch.setattr("sofias_memory.api.routes.episodes.EpisodeService", FakeEpisodeService)
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def error_code(response: httpx.Response) -> str:
    return cast(str, response.json()["error"]["code"])


@pytest.mark.asyncio
async def test_episode_routes_require_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.post("/api/v1/episodes", json=CREATE_BODY)
    assert response.status_code == 401
    assert error_code(response) == "MISSING_API_KEY"
    assert calls == {}


@pytest.mark.asyncio
async def test_create_returns_201_envelope_and_passes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.post("/api/v1/episodes", json=CREATE_BODY, headers=KEYED)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert "embedding" not in body["data"]
    assert body["data"]["lifecycle"] == "active"
    assert calls["create"][1] == "key-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("post", "/api/v1/episodes", CREATE_BODY),
        ("post", f"/api/v1/episodes/{uuid4()}/invalidate", {"reason": "wrong"}),
        ("post", f"/api/v1/episodes/{uuid4()}/forget", None),
    ],
)
async def test_mutations_require_idempotency_key(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, json_body: Any
) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        missing = await http.request(method, path, json=json_body, headers=HEADERS)
        empty = await http.request(
            method, path, json=json_body, headers={**HEADERS, "Idempotency-Key": ""}
        )
    for response in (missing, empty):
        assert response.status_code == 422
        assert error_code(response) == "INVALID_REQUEST"
    assert calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"scope": "global"}, "INVALID_SCOPE"),
        ({"scope": "project:Bad Key"}, "INVALID_SCOPE"),
        ({"context_key": "Feature"}, "INVALID_CONTEXT_KEY"),
        ({"kind": "note"}, "INVALID_EPISODE_KIND"),
        ({"summary": " "}, "INVALID_REQUEST"),
        ({"scope": "global", "kind": "note"}, "INVALID_REQUEST"),
        # SM-CTX-004: support is accepted; an empty Evidence set is an invalid policy.
        ({"support": {"mode": "all_required", "refs": []}}, "INVALID_SUPPORT_POLICY"),
    ],
)
async def test_create_validation_codes(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], expected: str
) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.post(
            "/api/v1/episodes", json={**CREATE_BODY, **overrides}, headers=KEYED
        )
    assert response.status_code == 422
    assert error_code(response) == expected
    assert "errors" in response.json()["error"]["details"]
    assert calls == {}


@pytest.mark.asyncio
async def test_list_defaults_and_passes_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.get(
            "/api/v1/episodes",
            params={"scope": "project:x", "context_key": "main", "kind": "incident"},
            headers=HEADERS,
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"items", "limit", "next_cursor"}
    kwargs = calls["list"]
    assert kwargs["scope"] == "project:x"
    assert kwargs["context_key"] == "main"
    assert kwargs["kind"].value == "incident"
    assert kwargs["lifecycle"] is None
    assert kwargs["limit"] == 50
    assert kwargs["cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, "INVALID_REQUEST"),
        ({"scope": "global"}, "INVALID_SCOPE"),
        ({"scope": "project:x", "context_key": "BAD"}, "INVALID_CONTEXT_KEY"),
        ({"scope": "project:x", "kind": "note"}, "INVALID_EPISODE_KIND"),
        ({"scope": "project:x", "lifecycle": "superseded"}, "INVALID_REQUEST"),
        ({"scope": "project:x", "limit": "0"}, "INVALID_REQUEST"),
        ({"scope": "project:x", "limit": "101"}, "INVALID_REQUEST"),
    ],
)
async def test_list_validation_codes(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, str], expected: str
) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.get("/api/v1/episodes", params=params, headers=HEADERS)
    assert response.status_code == 422
    assert error_code(response) == expected
    assert calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (episode_not_found_error(uuid4()), 404, "EPISODE_NOT_FOUND"),
        (episode_state_conflict_error(uuid4()), 409, "EPISODE_STATE_CONFLICT"),
        (idempotency_conflict_error(), 409, "IDEMPOTENCY_CONFLICT"),
        (DependencyUnavailableError("x"), 503, "DEPENDENCY_UNAVAILABLE"),
    ],
)
async def test_service_errors_map_to_stable_envelope(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: str
) -> None:
    app, _ = build_app(monkeypatch, side_effect=error)
    async with client(app) as http:
        response = await http.post(
            f"/api/v1/episodes/{uuid4()}/invalidate", json={"reason": "r"}, headers=KEYED
        )
    assert response.status_code == status
    assert error_code(response) == code


@pytest.mark.asyncio
async def test_get_invalidate_forget_success(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    episode_id = uuid4()
    replacement = uuid4()
    async with client(app) as http:
        got = await http.get(f"/api/v1/episodes/{episode_id}", headers=HEADERS)
        invalidated = await http.post(
            f"/api/v1/episodes/{episode_id}/invalidate",
            json={"reason": "inaccurate", "replacement_episode_id": str(replacement)},
            headers=KEYED,
        )
        forgotten = await http.post(f"/api/v1/episodes/{episode_id}/forget", headers=KEYED)
        bad_id = await http.get("/api/v1/episodes/not-a-uuid", headers=HEADERS)
    assert got.status_code == invalidated.status_code == forgotten.status_code == 200
    assert calls["get"] == episode_id
    assert calls["invalidate"][1].replacement_episode_id == replacement
    assert calls["forget"] == (episode_id, "key-1")
    assert bad_id.status_code == 422


@pytest.mark.asyncio
async def test_no_patch_put_delete_on_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    episode_id = uuid4()
    async with client(app) as http:
        for method in ("PATCH", "PUT", "DELETE"):
            response = await http.request(
                method, f"/api/v1/episodes/{episode_id}", json={}, headers=KEYED
            )
            assert response.status_code == 405


@pytest.mark.asyncio
async def test_info_does_not_advertise_contextual_contract_in_sm_ctx_001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.get("/api/v1/info", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()["data"]
    assert "contextual_project_memory" not in data.get("contracts", {})
    assert not [
        c for c in data.get("capabilities", []) if c.startswith("contextual_project_memory")
    ]
