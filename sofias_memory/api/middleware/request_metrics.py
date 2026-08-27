"""HTTP request completion metrics (SM-516 SS 17).

One structured event per HTTP request -- method, resolved route template,
status code, and duration -- never headers, query string, body, or the API
key. Deliberately outermost in the middleware stack (registered so it wraps
:class:`~sofias_memory.api.middleware.request_id.RequestIdMiddleware`) so it
also observes requests rejected before routing ever runs (CORS, API key,
request-body-limit) -- not just requests that reach a route handler.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping

from sofias_memory.observability.logging import get_logger

logger = get_logger(__name__)

ASGIMessage = MutableMapping[str, object]
ASGIScope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]

_REQUEST_ID_HEADER_BYTES = b"x-request-id"


class RequestMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.monotonic()
        status_code = 0
        request_id: str | None = None

        async def send_and_capture(message: ASGIMessage) -> None:
            nonlocal status_code, request_id
            if message.get("type") == "http.response.start":
                status_value = message.get("status")
                if isinstance(status_value, int):
                    status_code = status_value
                request_id = _extract_request_id(message)
            await send(message)

        await self.app(scope, receive, send_and_capture)

        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        fields: dict[str, object] = {
            "method": scope.get("method"),
            "route": _resolve_route_template(scope),
            "status_code": status_code,
            "duration_ms": duration_ms,
        }
        if request_id is not None:
            fields["request_id"] = request_id
        logger.info("http_request_completed", **fields)


def _extract_request_id(message: ASGIMessage) -> str | None:
    for name, value in _message_headers(message):
        if name.lower() == _REQUEST_ID_HEADER_BYTES:
            return value.decode("latin-1", errors="replace")
    return None


def _message_headers(message: ASGIMessage) -> Iterable[tuple[bytes, bytes]]:
    headers = message.get("headers", [])
    if not isinstance(headers, list):
        return []
    return headers


def _resolve_route_template(scope: ASGIScope) -> str:
    """Uses the route Starlette's router already matched, when routing ran
    at all (SS 17: a stable template, e.g. ``/api/v1/runs/{run_id}``, never
    the raw path for a matched route). Falls back to the raw path (still
    never the query string) only for a request rejected before routing --
    e.g. a missing/invalid ``X-API-Key`` -- where no template exists yet."""

    route = scope.get("route")
    path_format = getattr(route, "path", None)
    if isinstance(path_format, str):
        return path_format
    path = scope.get("path")
    return path if isinstance(path, str) else "unknown"


__all__ = ["RequestMetricsMiddleware"]
