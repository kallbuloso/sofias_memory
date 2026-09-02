"""Business-route lifecycle gate (ADR-0011 D31/D43, STORAGE-007).

Blocks every non-health request from executing its use case while the
process is not yet ``OPERATIONAL`` -- the request-side half of D43's
convergence/destructive-lifecycle exclusion (worker claim gating is the
other half, in ``pipeline_queue_claimer.py``). A single centralized
mechanism, mirroring ``ApiKeyMiddleware`` exactly, rather than scattering a
process-state check through every route.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from http import HTTPStatus

from sofias_memory.api.errors import current_request_id, error_json_response
from sofias_memory.api.middleware.api_key import PUBLIC_PATHS
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.process_state import ProcessStateHolder

NOT_OPERATIONAL_MESSAGE = "The service is starting up and not yet accepting requests."

ASGIMessage = MutableMapping[str, object]
ASGIScope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


class OperationalGateMiddleware:
    """Rejects any request outside :data:`PUBLIC_PATHS`/docs paths while the
    process has not yet reached ``OPERATIONAL`` (D31/D43). Health endpoints
    (``/health/live``, ``/health/ready``) are never gated -- they are how an
    operator/orchestrator observes this exact state (D33)."""

    def __init__(
        self,
        app: ASGIApp,
        state_holder: ProcessStateHolder,
        exempt_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self._state_holder = state_holder
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if _scope_path(scope) in self.exempt_paths or self._state_holder.is_operational:
            await self.app(scope, receive, send)
            return

        get_logger(__name__).info(
            "business_request_blocked_not_operational",
            path=_scope_path(scope),
            process_state=self._state_holder.state.value,
        )
        response = error_json_response(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message=NOT_OPERATIONAL_MESSAGE,
            request_id=current_request_id(),
        )
        await response(scope, receive, send)


def _scope_path(scope: ASGIScope) -> str:
    path = scope.get("path", "")
    if isinstance(path, str):
        return path
    return ""
