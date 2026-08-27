"""Unit tests for HTTP request completion metrics (SM-516 SS 17, SS 61)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from io import StringIO
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from sofias_memory.api.middleware.api_key import ApiKeyMiddleware
from sofias_memory.api.middleware.request_id import RequestIdMiddleware
from sofias_memory.api.middleware.request_metrics import RequestMetricsMiddleware
from sofias_memory.observability.logging import clear_log_context, configure_logging

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

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


class _FakeRoute:
    def __init__(self, path: str) -> None:
        self.path = path


def make_downstream(*, route_template: str | None = None, status: int = 200) -> ASGIApp:
    async def app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del receive
        if route_template is not None:
            scope["route"] = _FakeRoute(route_template)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def make_client(app: ASGIApp, *, with_api_key: bool = False) -> httpx.AsyncClient:
    wrapped: ASGIApp = app
    if with_api_key:
        wrapped = ApiKeyMiddleware(wrapped, api_key=SecretStr(EXPECTED_API_KEY))
    protected = RequestMetricsMiddleware(RequestIdMiddleware(wrapped))
    transport = httpx.ASGITransport(app=protected)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_request_metrics_uses_resolved_route_template(log_stream: StringIO) -> None:
    app = make_downstream(route_template="/api/v1/runs/{run_id}")
    async with make_client(app) as client:
        await client.get(f"/api/v1/runs/{uuid4()}")

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "http_request_completed"]
    assert record["route"] == "/api/v1/runs/{run_id}"
    assert record["method"] == "GET"
    assert record["status_code"] == 200
    assert isinstance(record["duration_ms"], (int, float))
    assert record["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_request_metrics_falls_back_to_raw_path_without_route(
    log_stream: StringIO,
) -> None:
    app = make_downstream(route_template=None)
    async with make_client(app) as client:
        await client.get("/health/live")

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "http_request_completed"]
    assert record["route"] == "/health/live"


@pytest.mark.asyncio
async def test_request_metrics_captures_request_id_from_response_header(
    log_stream: StringIO,
) -> None:
    app = make_downstream(route_template="/api/v1/runs")
    async with make_client(app) as client:
        response = await client.get("/api/v1/runs")

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "http_request_completed"]
    assert record["request_id"] == response.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_request_metrics_observes_auth_rejected_requests(log_stream: StringIO) -> None:
    """SS 17: request metrics wrap the API key middleware too -- an
    unauthenticated 401 is still a request completion worth counting, even
    though routing never ran and no route template exists yet."""

    app = make_downstream(route_template="/api/v1/runs")
    async with make_client(app, with_api_key=True) as client:
        response = await client.get("/api/v1/runs")

    assert response.status_code == 401
    [record] = [r for r in read_log_records(log_stream) if r["event"] == "http_request_completed"]
    assert record["status_code"] == 401
    assert record["route"] == "/api/v1/runs"


@pytest.mark.asyncio
async def test_request_metrics_never_logs_headers_query_or_body(log_stream: StringIO) -> None:
    app = make_downstream(route_template="/api/v1/remember/text")
    async with make_client(app) as client:
        await client.post(
            "/api/v1/remember/text?secret_param=leak-me",
            headers={"X-Custom-Sensitive": "leak-me-too"},
            json={"content": "leak-me-three"},
        )

    [record] = [r for r in read_log_records(log_stream) if r["event"] == "http_request_completed"]
    serialized = json.dumps(record)
    assert "leak-me" not in serialized
    assert "secret_param" not in serialized
    assert "headers" not in record
    assert "query" not in record
    assert "body" not in record
