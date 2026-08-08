from __future__ import annotations

from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from sofias_memory.api.routes.health import ReadinessCheckResult
from sofias_memory.app import create_app
from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.infrastructure.postgres.readiness import (
    POSTGRES_NOT_READY_DETAIL,
    PostgresReadinessChecker,
    PostgresReadinessResult,
)

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"
VALID_NEO4J_PASSWORD = "fake-neo4j-password"
VALID_LLM_API_KEY = "sk-fake-test-key"
KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


class FakePostgresReadinessChecker:
    def __init__(self, result: PostgresReadinessResult | None = None) -> None:
        self.result = result or PostgresReadinessResult(ready=True)
        self.check_calls = 0
        self.dispose_calls = 0

    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        return self.result

    async def dispose(self) -> None:
        self.dispose_calls += 1


class ExplodingPostgresReadinessChecker(FakePostgresReadinessChecker):
    async def check(self) -> PostgresReadinessResult:
        self.check_calls += 1
        raise RuntimeError(f"postgres exploded {KNOWN_SECRET}")


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "llm_api_key": VALID_LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def as_checker(fake_checker: FakePostgresReadinessChecker) -> PostgresReadinessChecker:
    return cast(PostgresReadinessChecker, fake_checker)


async def not_initialized_check() -> ReadinessCheckResult:
    return ReadinessCheckResult(ready=False, detail="not initialized")


@pytest.mark.asyncio
async def test_ready_route_reports_postgres_ready_when_checker_is_healthy() -> None:
    fake_checker = FakePostgresReadinessChecker(PostgresReadinessResult(ready=True))
    app = create_app(make_settings(), postgres_readiness_checker=as_checker(fake_checker))

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response_json(response) == {
        "status": "ready",
        "checks": {"postgres": {"ready": True}},
    }
    assert fake_checker.check_calls == 1


@pytest.mark.asyncio
async def test_ready_route_reports_postgres_not_ready_when_checker_fails() -> None:
    fake_checker = FakePostgresReadinessChecker(
        PostgresReadinessResult(ready=False, failures=("connection",))
    )
    app = create_app(make_settings(), postgres_readiness_checker=as_checker(fake_checker))

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response) == {
        "status": "not_ready",
        "checks": {"postgres": {"ready": False, "detail": POSTGRES_NOT_READY_DETAIL}},
    }
    assert fake_checker.check_calls == 1


@pytest.mark.asyncio
async def test_ready_route_keeps_overall_not_ready_when_other_components_are_not_ready() -> None:
    fake_checker = FakePostgresReadinessChecker(PostgresReadinessResult(ready=True))
    app = create_app(
        make_settings(),
        readiness_checks=(
            ("neo4j", not_initialized_check),
            ("worker", not_initialized_check),
        ),
        postgres_readiness_checker=as_checker(fake_checker),
    )

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    body = response_json(response)
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"] == {"ready": True}
    assert body["checks"]["neo4j"] == {"ready": False, "detail": "not initialized"}
    assert body["checks"]["worker"] == {"ready": False, "detail": "not initialized"}


@pytest.mark.asyncio
async def test_ready_route_does_not_leak_postgres_exception_details() -> None:
    fake_checker = ExplodingPostgresReadinessChecker()
    app = create_app(make_settings(), postgres_readiness_checker=as_checker(fake_checker))

    async with make_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert KNOWN_SECRET not in response.text
    assert "postgres exploded" not in response.text
    assert response_json(response)["checks"]["postgres"]["detail"] == "check failed"


@pytest.mark.asyncio
async def test_live_route_does_not_call_postgres_readiness_checker() -> None:
    fake_checker = ExplodingPostgresReadinessChecker()
    app = create_app(make_settings(), postgres_readiness_checker=as_checker(fake_checker))

    async with make_client(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response_json(response) == {"status": "ok"}
    assert fake_checker.check_calls == 0
