from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from io import StringIO

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from sofias_memory.api.middleware import (
    API_KEY_HEADER,
    REQUEST_ID_HEADER,
    RequestBodyLimitMiddleware,
)
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.observability.logging import clear_log_context, configure_logging
from sofias_memory.schemas.common import ErrorCode

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"
ALLOWED_ORIGIN = "https://client.example"
DISALLOWED_ORIGIN = "https://not-allowed.example"


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class FailingReadStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("request body should not have been read")
        yield b""


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
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()
    httpx_logger.setLevel(previous_httpx_level)


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def add_echo_route(app: FastAPI, executions: list[int] | None = None) -> None:
    @app.post("/api/v1/echo-size")
    async def echo_size(request: Request) -> dict[str, int]:
        body = await request.body()
        if executions is not None:
            executions.append(len(body))
        return {"size": len(body)}


def one_mib(settings: Settings) -> int:
    return settings.max_request_body_mb * 1024 * 1024


def test_cors_empty_configuration_disables_cors_middleware() -> None:
    app = create_app(make_settings(cors_allowed_origins=()))

    assert not any(middleware.cls is CORSMiddleware for middleware in app.user_middleware)


@pytest.mark.asyncio
async def test_cors_empty_origin_does_not_receive_allow_origin(
    log_stream: StringIO,
) -> None:
    async with make_client(create_app(make_settings(cors_allowed_origins=()))) as client:
        response = await client.get("/health/live", headers={"Origin": ALLOWED_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_allowed_origin_receives_cors_headers(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.get("/health/live", headers={"Origin": ALLOWED_ORIGIN})

    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


@pytest.mark.asyncio
async def test_disallowed_origin_does_not_receive_allow_origin(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.get("/health/live", headers={"Origin": DISALLOWED_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_preflight_allows_api_key_request_id_and_content_type(
    log_stream: StringIO,
) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.options(
            "/api/v1/info",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key, X-Request-Id, Content-Type",
            },
        )

    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "x-api-key" in allowed_headers
    assert "x-request-id" in allowed_headers
    assert "content-type" in allowed_headers


@pytest.mark.asyncio
async def test_cors_does_not_make_private_route_public(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.get("/api/v1/info", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 401
    assert response_json(response)["error"]["code"] == ErrorCode.MISSING_API_KEY
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


@pytest.mark.asyncio
async def test_auth_is_still_required_for_real_cors_request(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.get(
            "/api/v1/info",
            headers={"Origin": ALLOWED_ORIGIN, API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_id_still_works_with_cors(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(cors_allowed_origins=(ALLOWED_ORIGIN,)))
    ) as client:
        response = await client.get("/health/live", headers={"Origin": ALLOWED_ORIGIN})

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_body_below_limit_reaches_downstream(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=b"x" * (one_mib(settings) - 1),
        )

    assert response.status_code == 200
    assert response_json(response) == {"size": one_mib(settings) - 1}


@pytest.mark.asyncio
async def test_body_exactly_at_limit_reaches_downstream(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=b"x" * one_mib(settings),
        )

    assert response.status_code == 200
    assert response_json(response) == {"size": one_mib(settings)}


@pytest.mark.asyncio
async def test_body_above_limit_returns_413(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=b"x" * (one_mib(settings) + 1),
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_413_uses_stable_error_code_and_matching_request_id(
    log_stream: StringIO,
) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=b"x" * (one_mib(settings) + 1),
        )

    error = response_json(response)["error"]
    assert error["code"] == ErrorCode.REQUEST_TOO_LARGE
    assert REQUEST_ID_HEADER in response.headers
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]


@pytest.mark.asyncio
async def test_content_length_above_limit_rejects_before_reading_body(
    log_stream: StringIO,
) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={
                API_KEY_HEADER: EXPECTED_API_KEY,
                "Content-Length": str(one_mib(settings) + 1),
            },
            content=FailingReadStream(),
        )

    assert response.status_code == 413
    assert response_json(response)["error"]["code"] == ErrorCode.REQUEST_TOO_LARGE


@pytest.mark.asyncio
async def test_missing_content_length_is_still_limited(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=ChunkStream((b"x" * (one_mib(settings) + 1),)),
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_multiple_chunks_are_counted_cumulatively(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)
    half_limit = one_mib(settings) // 2

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=ChunkStream((b"x" * half_limit, b"x" * (half_limit + 1))),
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_downstream_does_not_receive_complete_request_after_limit(
    log_stream: StringIO,
) -> None:
    settings = make_settings(max_request_body_mb=1)
    executions: list[int] = []
    app = create_app(settings)
    add_echo_route(app, executions=executions)

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=b"x" * (one_mib(settings) + 1),
        )

    assert response.status_code == 413
    assert executions == []


@pytest.mark.asyncio
async def test_request_body_is_not_logged_or_returned(log_stream: StringIO) -> None:
    settings = make_settings(max_request_body_mb=1)
    app = create_app(settings)
    add_echo_route(app)
    body = (KNOWN_SECRET.encode("utf-8") * ((one_mib(settings) // len(KNOWN_SECRET)) + 1))[
        : one_mib(settings) + 1
    ]

    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/echo-size",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            content=body,
        )

    assert response.status_code == 413
    assert KNOWN_SECRET not in response.text
    assert KNOWN_SECRET not in log_stream.getvalue()


@pytest.mark.asyncio
async def test_private_request_without_api_key_does_not_read_huge_body(
    log_stream: StringIO,
) -> None:
    async with make_client(create_app(make_settings(max_request_body_mb=1))) as client:
        response = await client.post("/api/v1/info", content=FailingReadStream())

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_without_body_and_health_continue_working(log_stream: StringIO) -> None:
    app = create_app(make_settings(max_request_body_mb=1))

    async with make_client(app) as client:
        info_response = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})
        live_response = await client.get("/health/live")
        ready_response = await client.get("/health/ready")

    assert info_response.status_code == 200
    assert live_response.status_code == 200
    assert ready_response.status_code == 200


def test_request_body_limit_middleware_is_registered() -> None:
    app = create_app(make_settings())

    assert any(middleware.cls is RequestBodyLimitMiddleware for middleware in app.user_middleware)
