from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from uuid import UUID, uuid4

from sofias_memory.observability.logging import bound_log_context

MAX_REQUEST_ID_LENGTH = 64
REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("ascii")

ASGIMessage = MutableMapping[str, object]
ASGIScope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(_request_id_header_value(scope))

        async def send_with_request_id(message: ASGIMessage) -> None:
            if message.get("type") == "http.response.start":
                message["headers"] = _response_headers_with_request_id(message, request_id)
            await send(message)

        with bound_log_context(request_id=request_id):
            await self.app(scope, receive, send_with_request_id)


def resolve_request_id(header_value: str | None) -> str:
    if header_value is None or len(header_value) > MAX_REQUEST_ID_LENGTH:
        return str(uuid4())

    candidate = header_value.strip()
    if not candidate:
        return str(uuid4())

    try:
        return str(UUID(candidate))
    except ValueError:
        return str(uuid4())


def _request_id_header_value(scope: ASGIScope) -> str | None:
    for name, value in _scope_headers(scope):
        if name.lower() == _REQUEST_ID_HEADER_BYTES:
            return value.decode("latin-1", errors="replace")
    return None


def _response_headers_with_request_id(
    message: ASGIMessage,
    request_id: str,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (name, value)
        for name, value in _message_headers(message)
        if name.lower() != _REQUEST_ID_HEADER_BYTES
    ]
    headers.append((_REQUEST_ID_HEADER_BYTES, request_id.encode("ascii")))
    return headers


def _scope_headers(scope: ASGIScope) -> Iterable[tuple[bytes, bytes]]:
    headers = scope.get("headers", [])
    if not isinstance(headers, list):
        return []
    return headers


def _message_headers(message: ASGIMessage) -> Iterable[tuple[bytes, bytes]]:
    headers = message.get("headers", [])
    if not isinstance(headers, list):
        return []
    return headers
