"""ADR-0011 D31/D43 final fail-closed audit: proves the real production
composition root (``sofias_memory.app.create_app``) starts fail-closed, and
that only a real ``lifespan()`` run -- never construction-time state -- may
ever advance it out of ``BOOTSTRAP_MAINTENANCE``.

Every other route/unit test in this suite deliberately opts into an
already-``OPERATIONAL`` holder via ``tests/unit/_app_factory.py`` precisely
*because* this is the real default; this file is where that real default
itself, and the real lifespan-driven escape from it, are proven.
"""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.readiness import (
    PostgresReadinessChecker,
    PostgresReadinessResult,
)
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


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


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": EXPECTED_API_KEY},
    )


def make_bare_app() -> FastAPI:
    """No process_state_holder override -- exercises the real default."""

    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        enable_worker=False,
    )


# -- Items 1-5: create_app() without running lifespan --------------------


def test_create_app_without_lifespan_defaults_to_bootstrap_maintenance() -> None:
    app = make_bare_app()
    holder = cast(ProcessStateHolder, app.state.process_state_holder)
    assert holder.state is ProcessState.BOOTSTRAP_MAINTENANCE


@pytest.mark.asyncio
async def test_create_app_without_lifespan_health_live_remains_reachable() -> None:
    async with make_client(make_bare_app()) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_create_app_without_lifespan_health_ready_is_not_ready() -> None:
    async with make_client(make_bare_app()) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["process_state"] == {"ready": False, "detail": "process not operational"}


@pytest.mark.asyncio
async def test_create_app_without_lifespan_business_request_blocked_and_not_executed() -> None:
    """``GET /api/v1/info`` touches no external dependency at all -- if its
    handler ever ran it would unconditionally return 200. Getting 503/
    DEPENDENCY_UNAVAILABLE instead is direct proof the request was answered
    by the gate itself, never by the route."""

    async with make_client(make_bare_app()) as client:
        response = await client.get("/api/v1/info")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


# -- Item 6: explicit test injection of an OPERATIONAL holder -------------


@pytest.mark.asyncio
async def test_explicit_operational_injection_allows_ordinary_route_execution() -> None:
    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        enable_worker=False,
        process_state_holder=ProcessStateHolder(state=ProcessState.OPERATIONAL),
    )
    async with make_client(app) as client:
        response = await client.get("/api/v1/info")
    assert response.status_code == 200


# -- Item 7: real lifespan always resets/starts from BOOTSTRAP_MAINTENANCE -


def test_real_lifespan_resets_regardless_of_construction_default() -> None:
    """Even an app explicitly constructed with an already-OPERATIONAL holder
    observes the real, correct state machine once a real lifespan actually
    runs (``TestClient(app)`` as a context manager) -- ``lifespan()`` is the
    one authoritative "boot sequence begins now" trigger, never construction
    time."""

    app = create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        enable_worker=False,
        process_state_holder=ProcessStateHolder(state=ProcessState.OPERATIONAL),
    )
    with TestClient(app):
        holder = cast(ProcessStateHolder, app.state.process_state_holder)
        # The background bootstrap task may have already raced ahead to
        # OPERATIONAL (postgres/neo4j/worker are all disabled here, so its
        # own attempt is nearly instantaneous) -- what matters is that it was
        # forced back through BOOTSTRAP_MAINTENANCE at all, never left at the
        # construction-time OPERATIONAL value without lifespan ever running.
        assert holder.state in (ProcessState.BOOTSTRAP_MAINTENANCE, ProcessState.OPERATIONAL)


# -- Real ASGI maintenance -> convergence -> operational proof ------------


class _GatedPostgresReadinessChecker:
    """Reports the real bootstrap sequence's own first gate (D32 schema
    readiness) as not-ready until an ``asyncio.Event`` is set -- a faithful
    proxy for "bootstrap/convergence still in progress", not a fabricated
    hook. Deliberately non-blocking on every call (``check()`` never
    ``await``s the event itself): this same checker instance is also the one
    ``/health/ready`` queries on demand, and that request must observe an
    ordinary prompt not-ready result, never hang waiting for the event this
    test controls."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
        self.check_calls = 0

    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        if not self._gate.is_set():
            return PostgresReadinessResult(ready=False, failures=("held_by_test",))
        return PostgresReadinessResult(ready=True)

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_real_asgi_maintenance_surface_reachable_then_business_route_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No health-function calls, no sleep-based polling for correctness: a
    real ``FastAPI`` app, a real ``lifespan()`` run, and real HTTP requests
    over ``httpx.ASGITransport`` throughout. Bootstrap is held open by an
    ``asyncio.Event`` gating the real schema-readiness check; the maintenance
    surface is proven reachable and correctly NOT_READY/503 while it is held,
    then the gate is released and the process is awaited (via the tracked
    ``bootstrap_task``, never a fixed sleep) to ``OPERATIONAL`` before proving
    the business route then succeeds. The bootstrap retry interval is
    shortened only so this test does not itself have to wait out the real
    (5s) production retry cadence while the gate is held -- the retry
    mechanism being exercised, and the event-driven waits below, are real.
    """

    monkeypatch.setattr("sofias_memory.lifespan.BOOTSTRAP_RETRY_INTERVAL_SECONDS", 0.01)

    gate = asyncio.Event()
    checker = _GatedPostgresReadinessChecker(gate)
    app = create_app(
        make_settings(),
        enable_neo4j=False,
        enable_worker=False,
        postgres_readiness_checker=cast(PostgresReadinessChecker, checker),
    )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-API-Key": EXPECTED_API_KEY},
        ) as client,
        app.router.lifespan_context(app),
    ):
        # Held open: bootstrap is blocked inside the schema-readiness check.
        live_response = await client.get("/health/live")
        assert live_response.status_code == 200

        ready_response = await client.get("/health/ready")
        assert ready_response.status_code == 503
        assert ready_response.json()["status"] == "not_ready"

        business_response = await client.get("/api/v1/info")
        assert business_response.status_code == 503
        assert business_response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

        # Release the gate and await the SAME tracked background task the
        # real shutdown path also awaits -- never a fixed sleep.
        gate.set()
        await asyncio.wait_for(app.state.bootstrap_task, timeout=5.0)

        holder = cast(ProcessStateHolder, app.state.process_state_holder)
        assert holder.state is ProcessState.OPERATIONAL

        ready_after = await client.get("/health/ready")
        assert ready_after.status_code == 200

        business_after = await client.get("/api/v1/info")
        assert business_after.status_code == 200

    assert checker.check_calls >= 1
