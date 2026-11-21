"""Route-level proof for the SM-CTX-002 checkpoint HTTP surface: auth,
envelope, status codes, required Idempotency-Key, default context_key and
stable Contract validation/error codes. ``ContextCheckpointService`` is faked --
no PostgreSQL here (see the real-PostgreSQL integration suite)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointListResult,
    ContextCheckpointProvenanceResult,
    ContextCheckpointResult,
    ContextStatementResult,
)
from sofias_memory.services.context_checkpoints import (
    checkpoint_not_found_error,
    checkpoint_state_conflict_error,
    context_state_conflict_error,
    current_checkpoint_not_found_error,
)
from sofias_memory.services.pipeline_submission import idempotency_conflict_error
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}
KEYED = {**HEADERS, "Idempotency-Key": "key-1"}
BASE = "/api/v1/context/checkpoints"

CREATE_BODY: dict[str, Any] = {
    "scope": "project:contextual-memory-validation",
    "current_state": {"text": "PRD e Spec estão aprovados."},
    "provenance": {"origin_kind": "assistant_generated", "source_system": "assistant"},
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


def fake_result(
    checkpoint_id: UUID | None = None, context_key: str = "main"
) -> ContextCheckpointResult:
    return ContextCheckpointResult(
        checkpoint_id=checkpoint_id or uuid4(),
        scope="project:contextual-memory-validation",
        context_key=context_key,
        revision=1,
        lifecycle="active",  # type: ignore[arg-type]
        objective=None,
        current_state=ContextStatementResult(
            statement_id=uuid4(), text="PRD e Spec estão aprovados."
        ),
        approved_decisions=[],
        pending_work=[],
        open_items=[],
        blockers=[],
        next_action=None,
        provenance=ContextCheckpointProvenanceResult(
            origin_kind="assistant_generated",  # type: ignore[arg-type]
            source_system="assistant",
            conversation_ref=None,
            turn_ref=None,
        ),
        created_at=datetime.now(UTC),
        superseded_at=None,
        superseded_by=None,
        invalidated_at=None,
        forgotten_at=None,
    )


def build_app(
    monkeypatch: pytest.MonkeyPatch, *, side_effect: Exception | None = None
) -> tuple[Any, dict[str, Any]]:
    calls: dict[str, Any] = {}

    class FakeService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def _maybe_raise(self) -> None:
            if side_effect is not None:
                raise side_effect

        async def create_current(self, request: Any, *, idempotency_key: str) -> Any:
            calls["create"] = (request, idempotency_key)
            self._maybe_raise()
            return fake_result(context_key=request.context_key)

        async def get(self, checkpoint_id: UUID) -> Any:
            calls["get"] = checkpoint_id
            self._maybe_raise()
            return fake_result(checkpoint_id)

        async def get_current(self, *, scope: str, context_key: str) -> Any:
            calls["current"] = (scope, context_key)
            self._maybe_raise()
            return fake_result(context_key=context_key)

        async def list_checkpoints(self, **kwargs: Any) -> Any:
            calls["list"] = kwargs
            self._maybe_raise()
            return ContextCheckpointListResult(
                items=[fake_result()], limit=kwargs["limit"], next_cursor=None
            )

        async def invalidate(
            self, checkpoint_id: UUID, request: Any, *, idempotency_key: str
        ) -> Any:
            calls["invalidate"] = (checkpoint_id, request, idempotency_key)
            self._maybe_raise()
            return fake_result(checkpoint_id)

        async def forget(self, checkpoint_id: UUID, *, idempotency_key: str) -> Any:
            calls["forget"] = (checkpoint_id, idempotency_key)
            self._maybe_raise()
            return fake_result(checkpoint_id)

    monkeypatch.setattr(
        "sofias_memory.api.routes.context_checkpoints.ContextCheckpointService", FakeService
    )
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, calls


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_create_returns_201_envelope_with_resolved_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        response = await http.post(BASE, json=CREATE_BODY, headers=KEYED)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["data"]["context_key"] == "main"
    assert "request_id" in body["meta"]
    request, key = calls["create"]
    assert request.context_key == "main" and key == "key-1"


@pytest.mark.asyncio
async def test_mutations_require_api_key_and_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    checkpoint_id = uuid4()
    async with client(app) as http:
        assert (await http.post(BASE, json=CREATE_BODY)).status_code == 401
        for path, body in (
            (BASE, CREATE_BODY),
            (f"{BASE}/{checkpoint_id}/invalidate", {"reason": "r"}),
            (f"{BASE}/{checkpoint_id}/forget", None),
        ):
            response = await http.post(path, json=body, headers=HEADERS)
            assert response.status_code == 422, path
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert calls == {}


@pytest.mark.asyncio
async def test_validation_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        scope = await http.post(BASE, json={**CREATE_BODY, "scope": "global"}, headers=KEYED)
        key = await http.post(BASE, json={**CREATE_BODY, "context_key": "Main"}, headers=KEYED)
        missing = await http.post(
            BASE, json={k: v for k, v in CREATE_BODY.items() if k != "current_state"}, headers=KEYED
        )
        null_state = await http.post(
            BASE, json={**CREATE_BODY, "current_state": None}, headers=KEYED
        )
        current_scope = await http.get(
            f"{BASE}/current", params={"scope": "global"}, headers=HEADERS
        )
        list_key = await http.get(
            BASE, params={"scope": "project:p", "context_key": "a b"}, headers=HEADERS
        )
    assert scope.json()["error"]["code"] == "INVALID_SCOPE"
    assert key.json()["error"]["code"] == "INVALID_CONTEXT_KEY"
    assert missing.json()["error"]["code"] == "INVALID_REQUEST"
    assert null_state.json()["error"]["code"] == "INVALID_REQUEST"
    assert current_scope.json()["error"]["code"] == "INVALID_SCOPE"
    assert list_key.json()["error"]["code"] == "INVALID_CONTEXT_KEY"
    for response in (scope, key, missing, null_state, current_scope, list_key):
        assert response.status_code == 422
    assert calls == {}


@pytest.mark.asyncio
async def test_current_defaults_to_main_and_list_passes_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    async with client(app) as http:
        current = await http.get(f"{BASE}/current", params={"scope": "project:p"}, headers=HEADERS)
        listed = await http.get(
            BASE,
            params={"scope": "project:p", "lifecycle": "invalidated", "limit": 5},
            headers=HEADERS,
        )
        too_big = await http.get(BASE, params={"scope": "project:p", "limit": 101}, headers=HEADERS)
        no_scope = await http.get(BASE, headers=HEADERS)
    assert current.status_code == 200
    assert calls["current"] == ("project:p", "main")
    assert listed.status_code == 200
    assert calls["list"]["lifecycle"].value == "invalidated"
    assert calls["list"]["context_key"] is None and calls["list"]["limit"] == 5
    assert too_big.status_code == 422 and no_scope.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (checkpoint_not_found_error(uuid4()), 404, "CHECKPOINT_NOT_FOUND"),
        (
            current_checkpoint_not_found_error("project:p", "main"),
            404,
            "CURRENT_CHECKPOINT_NOT_FOUND",
        ),
        (context_state_conflict_error(uuid4()), 409, "CONTEXT_STATE_CONFLICT"),
        (checkpoint_state_conflict_error(uuid4()), 409, "CHECKPOINT_STATE_CONFLICT"),
        (idempotency_conflict_error(), 409, "IDEMPOTENCY_CONFLICT"),
    ],
)
async def test_service_errors_map_to_stable_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: str
) -> None:
    app, _ = build_app(monkeypatch, side_effect=error)
    async with client(app) as http:
        response = await http.post(BASE, json=CREATE_BODY, headers=KEYED)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_get_invalidate_forget_and_no_patch_put_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = build_app(monkeypatch)
    checkpoint_id = uuid4()
    async with client(app) as http:
        got = await http.get(f"{BASE}/{checkpoint_id}", headers=HEADERS)
        invalidated = await http.post(
            f"{BASE}/{checkpoint_id}/invalidate", json={"reason": " r "}, headers=KEYED
        )
        forgotten = await http.post(f"{BASE}/{checkpoint_id}/forget", headers=KEYED)
        bad_id = await http.get(f"{BASE}/not-a-uuid", headers=HEADERS)
        methods = [
            await http.request(method, f"{BASE}/{checkpoint_id}", headers=KEYED)
            for method in ("PATCH", "PUT", "DELETE")
        ]
        supersede = await http.post(f"{BASE}/{checkpoint_id}/supersede", json={}, headers=KEYED)
    assert got.status_code == 200 and calls["get"] == checkpoint_id
    assert invalidated.status_code == 200 and calls["invalidate"][1].reason == "r"
    assert forgotten.status_code == 200 and calls["forget"] == (checkpoint_id, "key-1")
    assert bad_id.status_code == 422
    assert all(response.status_code == 405 for response in methods)
    assert supersede.status_code in {404, 405}
