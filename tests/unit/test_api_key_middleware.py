from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from io import StringIO
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from sofias_memory.api.middleware.api_key import (
    API_KEY_HEADER,
    ApiKeyMiddleware,
    is_valid_api_key,
)
from sofias_memory.api.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from sofias_memory.config import Settings
from sofias_memory.observability.logging import clear_log_context, configure_logging

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WRONG_API_KEY = "sf-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

ASGIMessage = dict[str, object]
ASGIScope = dict[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


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


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def make_downstream(executions: list[str] | None = None) -> ASGIApp:
    async def app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del receive
        if executions is not None:
            executions.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def make_client(app: ASGIApp) -> httpx.AsyncClient:
    protected = RequestIdMiddleware(
        ApiKeyMiddleware(app, api_key=SecretStr(EXPECTED_API_KEY)),
    )
    transport = httpx.ASGITransport(app=protected)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


@pytest.mark.asyncio
async def test_private_route_without_header_returns_401(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_header_uses_stable_error_code(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private")

    assert response_json(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_private_route_with_wrong_key_returns_403(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_wrong_key_uses_stable_error_code(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})

    assert response_json(response)["error"]["code"] == "INVALID_API_KEY"


@pytest.mark.asyncio
async def test_correct_key_allows_downstream(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_downstream_runs_only_with_valid_key(log_stream: StringIO) -> None:
    executions: list[str] = []

    async with make_client(make_downstream(executions)) as client:
        await client.get("/api/v1/private")
        await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})
        await client.get("/api/v1/private", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert executions == ["/api/v1/private"]


def test_valid_api_key_helper_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def spy_compare_digest(left: str, right: str) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(
        "sofias_memory.api.middleware.api_key.hmac.compare_digest", spy_compare_digest
    )

    assert is_valid_api_key(EXPECTED_API_KEY, SecretStr(EXPECTED_API_KEY)) is True
    assert calls == [(EXPECTED_API_KEY, EXPECTED_API_KEY)]


def test_different_character_fails() -> None:
    changed = f"{EXPECTED_API_KEY[:-1]}B"

    assert is_valid_api_key(changed, SecretStr(EXPECTED_API_KEY)) is False


def test_correct_key_with_extra_whitespace_fails() -> None:
    assert is_valid_api_key(f"{EXPECTED_API_KEY} ", SecretStr(EXPECTED_API_KEY)) is False


@pytest.mark.asyncio
async def test_empty_header_is_invalid_not_missing(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={API_KEY_HEADER: ""})

    assert response.status_code == 403
    assert response_json(response)["error"]["code"] == "INVALID_API_KEY"


@pytest.mark.asyncio
async def test_header_name_casing_is_accepted(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={"x-api-key": EXPECTED_API_KEY})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_query_string_key_does_not_authenticate(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get(f"/api/v1/private?api_key={EXPECTED_API_KEY}")

    assert response.status_code == 401
    assert response_json(response)["error"]["code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_authorization_header_does_not_authenticate(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get(
            "/api/v1/private",
            headers={"Authorization": f"Bearer {EXPECTED_API_KEY}"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/live", "/health/live/"])
async def test_health_live_is_public(path: str, log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get(path)

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/ready", "/health/ready/"])
async def test_health_ready_is_public(path: str, log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get(path)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_similar_health_path_is_not_public(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/health/live/private")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_info_route_is_not_public(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/info")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_http_scope_is_delegated() -> None:
    observed_scope_type = None

    async def app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        nonlocal observed_scope_type
        del receive, send
        observed_scope_type = scope["type"]

    middleware = ApiKeyMiddleware(app, api_key=SecretStr(EXPECTED_API_KEY))

    async def receive() -> ASGIMessage:
        return {"type": "lifespan.startup"}

    sent_messages: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent_messages.append(message)

    await middleware({"type": "lifespan"}, receive, send)

    assert observed_scope_type == "lifespan"
    assert sent_messages == []


@pytest.mark.asyncio
async def test_expected_key_never_appears_in_response(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private")

    assert EXPECTED_API_KEY not in response.text


@pytest.mark.asyncio
async def test_received_key_never_appears_in_response(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})

    assert WRONG_API_KEY not in response.text


@pytest.mark.asyncio
async def test_expected_key_never_appears_in_log(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        await client.get("/api/v1/private")

    assert EXPECTED_API_KEY not in log_stream.getvalue()


@pytest.mark.asyncio
async def test_received_key_never_appears_in_log(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})

    assert WRONG_API_KEY not in log_stream.getvalue()


@pytest.mark.asyncio
async def test_request_id_header_is_returned_for_missing_key(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private")

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_response_request_id_matches_error_envelope(log_stream: StringIO) -> None:
    request_id = str(uuid4())
    async with make_client(make_downstream()) as client:
        response = await client.get("/api/v1/private", headers={REQUEST_ID_HEADER: request_id})

    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert response_json(response)["error"]["request_id"] == request_id


@pytest.mark.asyncio
async def test_concurrent_requests_with_different_keys_are_isolated(
    log_stream: StringIO,
) -> None:
    valid_request_id = str(uuid4())
    invalid_request_id = str(uuid4())
    executions: list[str] = []

    async with make_client(make_downstream(executions)) as client:
        valid_response, invalid_response = await asyncio.gather(
            client.get(
                "/api/v1/private",
                headers={
                    REQUEST_ID_HEADER: valid_request_id,
                    API_KEY_HEADER: EXPECTED_API_KEY,
                },
            ),
            client.get(
                "/api/v1/private",
                headers={
                    REQUEST_ID_HEADER: invalid_request_id,
                    API_KEY_HEADER: WRONG_API_KEY,
                },
            ),
        )

    assert valid_response.status_code == 200
    assert invalid_response.status_code == 403
    assert valid_response.headers[REQUEST_ID_HEADER] == valid_request_id
    assert invalid_response.headers[REQUEST_ID_HEADER] == invalid_request_id
    assert response_json(invalid_response)["error"]["request_id"] == invalid_request_id
    assert executions == ["/api/v1/private"]


@pytest.mark.asyncio
async def test_middleware_configuration_does_not_modify_settings(log_stream: StringIO) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
    )
    before = repr(settings)

    async with make_client(make_downstream()) as client:
        await client.get("/api/v1/private", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert repr(settings) == before


@pytest.mark.asyncio
async def test_secret_str_is_not_rendered_in_logs(log_stream: StringIO) -> None:
    async with make_client(make_downstream()) as client:
        await client.get("/api/v1/private", headers={API_KEY_HEADER: WRONG_API_KEY})

    output = log_stream.getvalue()
    assert EXPECTED_API_KEY not in output
    assert "SecretStr" not in output
