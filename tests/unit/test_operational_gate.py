"""Unit tests for ``OperationalGateMiddleware`` (ADR-0011 D31/D43,
STORAGE-007) -- the request-side half of the business-route lifecycle gate.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from sofias_memory.api.middleware import ApiKeyMiddleware, OperationalGateMiddleware
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def make_gated_app(holder: ProcessStateHolder) -> FastAPI:
    app = FastAPI()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/remember/text")
    async def remember() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(ApiKeyMiddleware, api_key=SecretStr(EXPECTED_API_KEY))
    app.add_middleware(OperationalGateMiddleware, state_holder=holder)
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": EXPECTED_API_KEY},
    )


@pytest.mark.asyncio
async def test_health_live_never_gated_regardless_of_state() -> None:
    holder = ProcessStateHolder(state=ProcessState.BOOTSTRAP_MAINTENANCE)
    async with make_client(make_gated_app(holder)) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_business_route_blocked_during_bootstrap_maintenance() -> None:
    holder = ProcessStateHolder(state=ProcessState.BOOTSTRAP_MAINTENANCE)
    async with make_client(make_gated_app(holder)) as client:
        response = await client.get("/api/v1/remember/text")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_business_route_blocked_during_storage_converging() -> None:
    holder = ProcessStateHolder(state=ProcessState.STORAGE_CONVERGING)
    async with make_client(make_gated_app(holder)) as client:
        response = await client.get("/api/v1/remember/text")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_business_route_succeeds_once_operational() -> None:
    holder = ProcessStateHolder(state=ProcessState.OPERATIONAL)
    async with make_client(make_gated_app(holder)) as client:
        response = await client.get("/api/v1/remember/text")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_gate_never_leaks_internal_detail() -> None:
    holder = ProcessStateHolder(state=ProcessState.BOOTSTRAP_MAINTENANCE)
    async with make_client(make_gated_app(holder)) as client:
        response = await client.get("/api/v1/remember/text")
    body = response.json()
    assert "traceback" not in str(body).lower()
    assert "credential" not in str(body).lower()


@pytest.mark.asyncio
async def test_invalid_api_key_rejected_regardless_of_process_state() -> None:
    """Existing auth contract preserved unchanged -- API-key failure is
    always 401/403, never masked by/confused with the operational gate."""

    holder = ProcessStateHolder(state=ProcessState.OPERATIONAL)
    app = make_gated_app(holder)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers={"X-API-Key": "wrong"}
    ) as client:
        response = await client.get("/api/v1/remember/text")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_gate_transition_is_observed_live_not_snapshotted() -> None:
    """The middleware reads ``state_holder.state`` fresh per request -- a
    transition to OPERATIONAL mid-process is observed immediately, without
    reconstructing the app."""

    holder = ProcessStateHolder(state=ProcessState.BOOTSTRAP_MAINTENANCE)
    app = make_gated_app(holder)
    async with make_client(app) as client:
        blocked = await client.get("/api/v1/remember/text")
        holder.transition(ProcessState.OPERATIONAL)
        allowed = await client.get("/api/v1/remember/text")

    assert blocked.status_code == 503
    assert allowed.status_code == 200
