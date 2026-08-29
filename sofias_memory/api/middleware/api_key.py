from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Iterable, MutableMapping

from pydantic import SecretStr

from sofias_memory.api.errors import (
    InvalidApiKeyError,
    MissingApiKeyError,
    current_request_id,
    error_json_response,
)
from sofias_memory.observability.logging import get_logger

API_KEY_HEADER = "X-API-Key"
PUBLIC_PATHS = frozenset(
    {
        "/health/live",
        "/health/live/",
        "/health/ready",
        "/health/ready/",
    }
)

# Always exempt from X-API-Key, in every environment -- not conditional on
# whether docs are actually registered (see create_app()). Whether a request
# here resolves to 200 or 404 is decided entirely by FastAPI's router
# (docs_url/openapi_url are None outside dev/development, redoc_url is
# always None): this set only ensures that decision is reachable at all. If
# these paths were exempted only when enabled, an unauthenticated request in
# a non-dev environment would be intercepted by the auth check first and see
# 401 instead of the required "route doesn't exist" 404.
DOCS_PUBLIC_PATHS = frozenset(
    {
        "/docs",
        "/docs/",
        "/openapi.json",
        "/redoc",
        "/redoc/",
    }
)
_API_KEY_HEADER_BYTES = API_KEY_HEADER.lower().encode("ascii")

ASGIMessage = MutableMapping[str, object]
ASGIScope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


class ApiKeyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        api_key: SecretStr,
        public_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if _scope_path(scope) in self.public_paths:
            await self.app(scope, receive, send)
            return

        received_api_key = _api_key_header_value(scope)
        if received_api_key is None:
            await _send_auth_error(scope, receive, send, MissingApiKeyError())
            return

        if not is_valid_api_key(received_api_key, self.api_key):
            await _send_auth_error(scope, receive, send, InvalidApiKeyError())
            return

        await self.app(scope, receive, send)


def is_valid_api_key(received: str, expected: SecretStr) -> bool:
    return hmac.compare_digest(received, expected.get_secret_value())


def _api_key_header_value(scope: ASGIScope) -> str | None:
    for name, value in _scope_headers(scope):
        if name.lower() == _API_KEY_HEADER_BYTES:
            return value.decode("latin-1", errors="replace")
    return None


async def _send_auth_error(
    scope: ASGIScope,
    receive: Receive,
    send: Send,
    error: MissingApiKeyError | InvalidApiKeyError,
) -> None:
    get_logger(__name__).warning(
        "api_key_auth_failed",
        error_code=error.code.value,
        method=_scope_method(scope),
        path=_scope_path(scope),
        status_code=error.status_code,
    )
    response = error_json_response(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        request_id=current_request_id(),
        details=error.details,
    )
    await response(scope, receive, send)


def _scope_headers(scope: ASGIScope) -> Iterable[tuple[bytes, bytes]]:
    headers = scope.get("headers", [])
    if not isinstance(headers, list):
        return []
    return headers


def _scope_path(scope: ASGIScope) -> str:
    path = scope.get("path", "")
    if isinstance(path, str):
        return path
    return ""


def _scope_method(scope: ASGIScope) -> str:
    method = scope.get("method", "")
    if isinstance(method, str):
        return method
    return ""
