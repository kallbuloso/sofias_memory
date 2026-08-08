from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from sofias_memory.api.middleware import API_KEY_HEADER, REQUEST_ID_HEADER
from sofias_memory.api.routes.health import ReadinessCheckResult
from sofias_memory.app import create_app
from sofias_memory.config import Settings

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
INVALID_API_KEY = "sf-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
DATABASE_PASSWORD = "DATABASE_SECRET_DO_NOT_LEAK_123"
NEO4J_PASSWORD = "NEO4J_SECRET_DO_NOT_LEAK_123"
LLM_API_KEY = "LLM_SECRET_DO_NOT_LEAK_123"
EMBEDDING_API_KEY = "EMBEDDING_SECRET_DO_NOT_LEAK_123"
DATABASE_URL = f"postgresql+asyncpg://sofias_memory:{DATABASE_PASSWORD}@postgres:5432/sofias_memory"
INFO_DATA_FIELDS = {
    "name",
    "version",
    "environment",
    "config_fingerprint",
    "llm_model",
    "embedding_model",
    "embedding_dimensions",
}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "embedding_api_key": EMBEDDING_API_KEY,
        "app_name": "Sofias Memory Test",
        "app_version": "9.8.7",
        "app_env": "test",
        "llm_model": "gpt-test-model",
        "embedding_model": "embedding-test-model",
        "embedding_dimensions": 3072,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def json_object(response: httpx.Response) -> dict[str, object]:
    return cast(dict[str, object], response.json())


def data_object(response: httpx.Response) -> dict[str, object]:
    body = json_object(response)
    return cast(dict[str, object], body["data"])


def meta_object(response: httpx.Response) -> dict[str, object]:
    body = json_object(response)
    return cast(dict[str, object], body["meta"])


async def ready_check() -> ReadinessCheckResult:
    return ReadinessCheckResult(ready=True)


@pytest.mark.asyncio
async def test_info_route_exists_and_requires_api_key() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info")

    assert response.status_code == 401
    assert json_object(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_info_with_invalid_key_returns_403() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: INVALID_API_KEY})

    assert response.status_code == 403
    assert json_object(response)["error"]["code"] == "INVALID_API_KEY"


@pytest.mark.asyncio
async def test_info_with_valid_key_returns_success_envelope() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200
    assert set(json_object(response)) == {"data", "meta"}
    assert set(data_object(response)) == INFO_DATA_FIELDS
    assert set(meta_object(response)) == {"request_id", "timestamp"}


@pytest.mark.asyncio
async def test_info_returns_expected_application_metadata() -> None:
    settings = make_settings(
        app_name="Sofias Memory Custom",
        app_version="1.2.3",
        app_env="staging",
        llm_model="gpt-5-mini-test",
        embedding_model="text-embedding-test",
        embedding_dimensions=3072,
    )

    async with make_client(create_app(settings)) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    data = data_object(response)
    assert data == {
        "name": "Sofias Memory Custom",
        "version": "1.2.3",
        "environment": "staging",
        "config_fingerprint": settings.config_fingerprint(),
        "llm_model": "gpt-5-mini-test",
        "embedding_model": "text-embedding-test",
        "embedding_dimensions": 3072,
    }


@pytest.mark.asyncio
async def test_info_request_id_matches_response_header() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert REQUEST_ID_HEADER in response.headers
    assert meta_object(response)["request_id"] == response.headers[REQUEST_ID_HEADER]


@pytest.mark.asyncio
async def test_info_timestamp_is_utc_iso8601() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    timestamp = cast(str, meta_object(response)["timestamp"])
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert timestamp.endswith("Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)


@pytest.mark.asyncio
async def test_two_apps_return_their_own_settings() -> None:
    first_settings = make_settings(
        api_key=EXPECTED_API_KEY,
        app_name="First Sofia",
        app_version="1.0.0",
        app_env="first",
    )
    second_settings = make_settings(
        api_key=INVALID_API_KEY,
        app_name="Second Sofia",
        app_version="2.0.0",
        app_env="second",
    )
    first_app = create_app(first_settings)
    second_app = create_app(second_settings)

    async with make_client(first_app) as first_client:
        first_response = await first_client.get(
            "/api/v1/info",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
    async with make_client(second_app) as second_client:
        second_response = await second_client.get(
            "/api/v1/info",
            headers={API_KEY_HEADER: INVALID_API_KEY},
        )

    assert first_app.state.settings is first_settings
    assert second_app.state.settings is second_settings
    assert data_object(first_response)["name"] == "First Sofia"
    assert data_object(second_response)["name"] == "Second Sofia"


@pytest.mark.asyncio
async def test_changing_only_app_version_does_not_change_fingerprint() -> None:
    first_settings = make_settings(app_version="0.1.0")
    second_settings = make_settings(app_version="0.1.1")

    async with make_client(create_app(first_settings)) as client:
        first_response = await client.get(
            "/api/v1/info",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
    async with make_client(create_app(second_settings)) as client:
        second_response = await client.get(
            "/api/v1/info",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    first_data = data_object(first_response)
    second_data = data_object(second_response)
    assert first_data["version"] == "0.1.0"
    assert second_data["version"] == "0.1.1"
    assert first_data["config_fingerprint"] == second_data["config_fingerprint"]


@pytest.mark.asyncio
async def test_info_response_does_not_expose_secrets_or_sensitive_configuration() -> None:
    settings = make_settings()
    known_secrets = {
        EXPECTED_API_KEY,
        DATABASE_PASSWORD,
        NEO4J_PASSWORD,
        LLM_API_KEY,
        EMBEDDING_API_KEY,
        DATABASE_URL,
    }

    async with make_client(create_app(settings)) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200
    for secret in known_secrets:
        assert secret not in response.text
    forbidden_fields = {
        "api_key",
        "llm_api_key",
        "embedding_api_key",
        "database_url",
        "neo4j_password",
        "llm_base_url",
        "embedding_base_url",
    }
    assert forbidden_fields.isdisjoint(data_object(response))


@pytest.mark.asyncio
async def test_info_does_not_reload_settings_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called() -> Settings:
        raise AssertionError("load_settings should not be called by /api/v1/info")

    import sofias_memory.app as app_module

    monkeypatch.setattr(app_module, "load_settings", fail_if_called)

    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_info_does_not_execute_readiness_checks() -> None:
    calls = 0

    async def check() -> ReadinessCheckResult:
        nonlocal calls
        calls += 1
        return ReadinessCheckResult(ready=True)

    checks: tuple[tuple[str, Callable[[], Awaitable[ReadinessCheckResult]]], ...] = (
        ("postgresql", check),
    )

    async with make_client(create_app(make_settings(), readiness_checks=checks)) as client:
        response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200
    assert calls == 0


@pytest.mark.asyncio
async def test_health_endpoints_continue_working_independently() -> None:
    async with make_client(create_app(make_settings())) as client:
        live_response = await client.get("/health/live")
        ready_response = await client.get("/health/ready")

    assert live_response.status_code == 200
    assert ready_response.status_code == 200


@pytest.mark.asyncio
async def test_info_is_not_in_public_allowlist() -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/api/v1/info")

    assert response.status_code == 401
    assert json_object(response)["error"]["code"] == "MISSING_API_KEY"
