from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from http import HTTPStatus

from sofias_memory.api.errors import SofiasMemoryError, current_request_id, error_json_response
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import ErrorCode

BYTES_PER_MIB = 1024 * 1024
REQUEST_TOO_LARGE_MESSAGE = "Request body is too large."

ASGIMessage = MutableMapping[str, object]
ASGIScope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


class RequestTooLargeError(SofiasMemoryError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.REQUEST_TOO_LARGE,
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            message=REQUEST_TOO_LARGE_MESSAGE,
        )


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await _send_request_too_large(scope, receive, send)
            return

        limited_receive = _LimitedReceive(receive, max_body_bytes=self.max_body_bytes)
        await self.app(scope, limited_receive, send)


class _LimitedReceive:
    def __init__(self, receive: Receive, max_body_bytes: int) -> None:
        self.receive = receive
        self.max_body_bytes = max_body_bytes
        self.received_body_bytes = 0

    async def __call__(self) -> ASGIMessage:
        message = await self.receive()
        if message.get("type") != "http.request":
            return message

        body = message.get("body", b"")
        body_size = len(body) if isinstance(body, bytes) else 0
        self.received_body_bytes += body_size
        if self.received_body_bytes > self.max_body_bytes:
            raise RequestTooLargeError()

        return message


def max_body_bytes_from_mebibytes(max_request_body_mb: int) -> int:
    if max_request_body_mb <= 0:
        raise ValueError("max_request_body_mb must be positive")
    return max_request_body_mb * BYTES_PER_MIB


def _content_length(scope: ASGIScope) -> int | None:
    for name, value in _scope_headers(scope):
        if name.lower() == b"content-length":
            try:
                parsed = int(value.decode("ascii"))
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
    return None


async def _send_request_too_large(
    scope: ASGIScope,
    receive: Receive,
    send: Send,
) -> None:
    get_logger(__name__).warning(
        "request_body_too_large",
        error_code=ErrorCode.REQUEST_TOO_LARGE.value,
        method=_scope_method(scope),
        path=_scope_path(scope),
        status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    )
    response = error_json_response(
        status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        code=ErrorCode.REQUEST_TOO_LARGE,
        message=REQUEST_TOO_LARGE_MESSAGE,
        request_id=current_request_id(),
    )
    await response(scope, receive, send)


def _scope_headers(scope: ASGIScope) -> Iterable[tuple[bytes, bytes]]:
    headers = scope.get("headers", [])
    if not isinstance(headers, list):
        return []
    return headers


def _scope_method(scope: ASGIScope) -> str:
    method = scope.get("method", "")
    if isinstance(method, str):
        return method
    return ""


def _scope_path(scope: ASGIScope) -> str:
    path = scope.get("path", "")
    if isinstance(path, str):
        return path
    return ""
