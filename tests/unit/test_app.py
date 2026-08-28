from __future__ import annotations

import importlib
import json
import sys
from io import StringIO

import httpx
import pytest
from fastapi import Body, FastAPI
from fastapi.exceptions import RequestValidationError

from sofias_memory.api.errors import (
    ConfigurationError,
    SofiasMemoryError,
    request_validation_error_handler,
    sofias_memory_error_handler,
    unexpected_error_handler,
)
from sofias_memory.api.middleware import API_KEY_HEADER, REQUEST_ID_HEADER
from sofias_memory.api.middleware.api_key import ApiKeyMiddleware
from sofias_memory.api.middleware.request_id import RequestIdMiddleware
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.observability.logging import clear_log_context, configure_logging

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SECOND_API_KEY = "sf-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_name": "Sofias Memory Test",
        "app_version": "9.8.7",
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def test_create_app_returns_fastapi() -> None:
    assert isinstance(create_app(make_settings()), FastAPI)


def test_import_app_module_does_not_call_load_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called() -> Settings:
        raise AssertionError("load_settings should not be called during import")

    monkeypatch.setattr("sofias_memory.config.load_settings", fail_if_called)
    sys.modules.pop("sofias_memory.app", None)

    module = importlib.import_module("sofias_memory.app")

    assert module.create_app is not None


def test_import_app_module_does_not_create_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fastapi_spy(*args: object, **kwargs: object) -> FastAPI:
        nonlocal calls
        calls += 1
        return FastAPI(*args, **kwargs)

    monkeypatch.setattr("fastapi.FastAPI", fastapi_spy)
    sys.modules.pop("sofias_memory.app", None)

    importlib.import_module("sofias_memory.app")

    assert calls == 0


def test_import_app_module_does_not_open_external_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called() -> Settings:
        raise AssertionError("external configuration should not be loaded during import")

    monkeypatch.setattr("sofias_memory.config.load_settings", fail_if_called)
    sys.modules.pop("sofias_memory.app", None)

    importlib.import_module("sofias_memory.app")


def test_create_app_without_settings_uses_loader_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    calls = 0

    def load_settings_spy() -> Settings:
        nonlocal calls
        calls += 1
        return settings

    import sofias_memory.app as app_module

    monkeypatch.setattr(app_module, "load_settings", load_settings_spy)

    app = app_module.create_app()

    assert calls == 1
    assert app.state.settings is settings


def test_two_create_app_calls_produce_independent_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    import sofias_memory.app as app_module

    monkeypatch.setattr(app_module, "load_settings", lambda: settings)

    first = app_module.create_app()
    second = app_module.create_app()

    assert first is not second
    assert first.state is not second.state


def test_lazy_application_is_not_public() -> None:
    import sofias_memory.app as app_module

    assert not hasattr(app_module, "LazyApplication")
    assert not hasattr(app_module, "app")


def test_create_app_result_is_directly_introspectable_fastapi() -> None:
    app = create_app(make_settings())

    assert isinstance(app, FastAPI)
    assert hasattr(app, "user_middleware")
    assert hasattr(app, "routes")


def test_injected_settings_are_stored_in_app_state() -> None:
    settings = make_settings()
    app = create_app(settings)

    assert app.state.settings is settings


def test_same_settings_instance_is_preserved() -> None:
    settings = make_settings()
    app = create_app(settings)

    assert app.state.settings is settings


def test_title_uses_app_name() -> None:
    app = create_app(make_settings(app_name="Custom Sofia"))

    assert app.title == "Custom Sofia"


def test_version_uses_app_version() -> None:
    app = create_app(make_settings(app_version="1.2.3"))

    assert app.version == "1.2.3"


def test_openapi_schema_reports_app_version() -> None:
    app = create_app(make_settings(app_version="1.2.3"))

    assert app.openapi()["info"]["version"] == "1.2.3"


def test_request_id_middleware_is_registered() -> None:
    app = create_app(make_settings())

    assert any(middleware.cls is RequestIdMiddleware for middleware in app.user_middleware)


def test_api_key_middleware_is_registered() -> None:
    app = create_app(make_settings())

    assert any(middleware.cls is ApiKeyMiddleware for middleware in app.user_middleware)


@pytest.mark.asyncio
async def test_private_route_without_api_key_returns_401(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/private")

    assert response.status_code == 401
    assert response_json(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_auth_failure_has_request_id_header(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/private")

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_auth_failure_request_id_matches_error_envelope(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/private")

    request_id = response.headers[REQUEST_ID_HEADER]
    assert response_json(response)["error"]["request_id"] == request_id


@pytest.mark.asyncio
async def test_correct_key_reaches_fastapi_downstream(log_stream: StringIO) -> None:
    app = create_app(make_settings())

    @app.get("/private")
    async def private_route() -> dict[str, str]:
        return {"status": "ok"}

    async with make_client(app) as client:
        response = await client.get("/private", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200
    assert response_json(response) == {"status": "ok"}


@pytest.mark.asyncio
async def test_docs_without_api_key_are_protected(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/docs")

    assert response.status_code == 401
    assert response_json(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_openapi_without_api_key_is_protected(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 401
    assert response_json(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_health_path_without_api_key_is_not_blocked_by_auth(
    log_stream: StringIO,
) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


def test_sofias_memory_error_handler_is_registered() -> None:
    app = create_app(make_settings())

    assert app.exception_handlers[SofiasMemoryError] is sofias_memory_error_handler


def test_request_validation_error_handler_is_registered() -> None:
    app = create_app(make_settings())

    assert app.exception_handlers[RequestValidationError] is request_validation_error_handler


def test_internal_exception_handler_is_registered() -> None:
    app = create_app(make_settings())

    assert app.exception_handlers[Exception] is unexpected_error_handler


@pytest.mark.asyncio
async def test_registered_sofias_memory_error_handler_works(log_stream: StringIO) -> None:
    app = create_app(make_settings())

    @app.get("/raise-application-error")
    async def raise_application_error() -> None:
        raise ConfigurationError()

    async with make_client(app) as client:
        response = await client.get(
            "/raise-application-error",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 500
    assert response_json(response)["error"]["code"] == "CONFIGURATION_ERROR"


@pytest.mark.asyncio
async def test_registered_request_validation_error_handler_works(
    log_stream: StringIO,
) -> None:
    app = create_app(make_settings())

    @app.post("/validate")
    async def validate_payload(count: int = Body()) -> dict[str, int]:
        return {"count": count}

    async with make_client(app) as client:
        response = await client.post(
            "/validate",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json=KNOWN_SECRET,
        )

    assert response.status_code == 422
    assert response_json(response)["error"]["code"] == "INVALID_REQUEST"
    assert KNOWN_SECRET not in response.text


@pytest.mark.asyncio
async def test_registered_internal_exception_handler_works(log_stream: StringIO) -> None:
    app = create_app(make_settings())

    @app.get("/raise-internal-error")
    async def raise_internal_error() -> None:
        raise RuntimeError(f"boom {KNOWN_SECRET}")

    async with make_client(app) as client:
        response = await client.get(
            "/raise-internal-error",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 500
    assert response_json(response)["error"]["code"] == "INTERNAL_ERROR"
    assert KNOWN_SECRET not in response.text


def test_two_apps_with_distinct_settings_are_independent() -> None:
    first_settings = make_settings(app_name="First", api_key=EXPECTED_API_KEY)
    second_settings = make_settings(app_name="Second", api_key=SECOND_API_KEY)
    first = create_app(first_settings)
    second = create_app(second_settings)

    assert first.title == "First"
    assert second.title == "Second"
    assert first.state.settings is first_settings
    assert second.state.settings is second_settings


def test_creating_two_apps_does_not_share_state() -> None:
    first = create_app(make_settings(app_name="First"))
    second = create_app(make_settings(app_name="Second"))

    first.state.marker = "first"

    assert not hasattr(second.state, "marker")


@pytest.mark.asyncio
async def test_secrets_do_not_appear_in_captured_logs(log_stream: StringIO) -> None:
    settings = make_settings(
        api_key=SECOND_API_KEY,
        database_url=(
            f"postgresql+asyncpg://sofias_memory:{KNOWN_SECRET}@postgres:5432/sofias_memory"
        ),
        neo4j_password=KNOWN_SECRET,
        llm_api_key=KNOWN_SECRET,
    )

    async with make_client(create_app(settings)) as client:
        await client.get("/private")

    output = log_stream.getvalue()
    assert KNOWN_SECRET not in output
    assert SECOND_API_KEY not in output
