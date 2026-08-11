from __future__ import annotations

import asyncio
import json
from io import StringIO
from uuid import uuid4

import httpx
import pytest
from fastapi import Body, FastAPI

from sofias_memory.api.errors import ConfigurationError
from sofias_memory.api.middleware import API_KEY_HEADER, REQUEST_ID_HEADER
from sofias_memory.api.routes.health import (
    READINESS_CHECK_TIMEOUT_SECONDS,
    ReadinessCheckRegistry,
    ReadinessCheckResult,
)
from sofias_memory.app import create_app as create_production_app
from sofias_memory.config import Settings
from sofias_memory.observability.logging import clear_log_context, configure_logging

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


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


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def create_app(
    settings: Settings,
    readiness_checks: ReadinessCheckRegistry = (),
) -> FastAPI:
    return create_production_app(
        settings,
        readiness_checks=readiness_checks,
        enable_postgres_readiness=False,
        enable_neo4j=False,
    )


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


async def ready_check() -> ReadinessCheckResult:
    return ReadinessCheckResult(ready=True)


async def not_ready_check(detail: str = "dependency unavailable") -> ReadinessCheckResult:
    return ReadinessCheckResult(ready=False, detail=detail)


@pytest.mark.asyncio
async def test_liveness_route_exists_and_returns_200(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_liveness_does_not_require_api_key(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_liveness_has_status_ok(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert response_json(response) == {"status": "ok"}


@pytest.mark.asyncio
async def test_liveness_response_has_request_id(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_liveness_does_not_execute_readiness_checks(log_stream: StringIO) -> None:
    calls = 0

    async def check() -> ReadinessCheckResult:
        nonlocal calls
        calls += 1
        return ReadinessCheckResult(ready=True)

    async with make_client(
        create_app(make_settings(), readiness_checks=(("check", check),))
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert calls == 0


@pytest.mark.asyncio
async def test_liveness_does_not_access_external_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    log_stream: StringIO,
) -> None:
    def fail_if_called() -> Settings:
        raise AssertionError("no settings reload during liveness")

    import sofias_memory.app as app_module

    monkeypatch.setattr(app_module, "load_settings", fail_if_called)

    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_readiness_route_exists_without_checks(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_readiness_without_checks_does_not_require_api_key(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_readiness_without_checks_returns_ready(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/ready")

    assert response_json(response) == {"status": "ready", "checks": {}}


@pytest.mark.asyncio
async def test_readiness_without_checks_response_has_request_id(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/health/ready")

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_one_ready_check_returns_200(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(), readiness_checks=(("configuration", ready_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response_json(response)["status"] == "ready"


@pytest.mark.asyncio
async def test_check_name_comes_from_registry_not_result(log_stream: StringIO) -> None:
    async def unnamed_check() -> ReadinessCheckResult:
        return ReadinessCheckResult(ready=True)

    async with make_client(
        create_app(make_settings(), readiness_checks=(("postgresql", unnamed_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert response_json(response)["checks"] == {"postgresql": {"ready": True}}


def test_readiness_check_result_does_not_know_its_own_name() -> None:
    result = ReadinessCheckResult(ready=True)

    assert not hasattr(result, "name")


@pytest.mark.asyncio
async def test_multiple_ready_checks_return_200(log_stream: StringIO) -> None:
    async with make_client(
        create_app(
            make_settings(),
            readiness_checks=(
                ("postgresql", ready_check),
                ("neo4j", ready_check),
            ),
        )
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response_json(response)["status"] == "ready"


@pytest.mark.asyncio
async def test_two_distinct_registered_names_appear_separately(
    log_stream: StringIO,
) -> None:
    async with make_client(
        create_app(
            make_settings(),
            readiness_checks={
                "postgresql": ready_check,
                "neo4j": ready_check,
            },
        )
    ) as client:
        response = await client.get("/health/ready")

    assert set(response_json(response)["checks"]) == {"postgresql", "neo4j"}


def test_empty_readiness_check_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="readiness check name"):
        create_app(make_settings(), readiness_checks=(("", ready_check),))


def test_whitespace_readiness_check_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="readiness check name"):
        create_app(make_settings(), readiness_checks=(("   ", ready_check),))


def test_duplicate_readiness_check_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate readiness check name"):
        create_app(
            make_settings(),
            readiness_checks=(
                ("postgresql", ready_check),
                ("postgresql", ready_check),
            ),
        )


def test_mapping_readiness_check_registry_has_unique_keys_structurally() -> None:
    app = create_app(
        make_settings(),
        readiness_checks={
            "postgresql": ready_check,
            "neo4j": ready_check,
        },
    )

    assert len(app.state.readiness_checks) == 2


@pytest.mark.asyncio
async def test_one_not_ready_check_returns_503(log_stream: StringIO) -> None:
    async with make_client(
        create_app(make_settings(), readiness_checks=(("postgresql", not_ready_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response)["status"] == "not_ready"


@pytest.mark.asyncio
async def test_mixed_checks_return_503(log_stream: StringIO) -> None:
    async with make_client(
        create_app(
            make_settings(),
            readiness_checks=(
                ("configuration", ready_check),
                ("neo4j", not_ready_check),
            ),
        )
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response)["status"] == "not_ready"


@pytest.mark.asyncio
async def test_check_names_and_safe_details_appear(log_stream: StringIO) -> None:
    async with make_client(
        create_app(
            make_settings(),
            readiness_checks=(("postgresql", lambda: not_ready_check("dependency unavailable")),),
        )
    ) as client:
        response = await client.get("/health/ready")

    body = response_json(response)
    assert body["checks"] == {"postgresql": {"ready": False, "detail": "dependency unavailable"}}


@pytest.mark.asyncio
async def test_readiness_result_order_is_deterministic(log_stream: StringIO) -> None:
    async with make_client(
        create_app(
            make_settings(),
            readiness_checks=(
                ("zeta", ready_check),
                ("alpha", ready_check),
            ),
        )
    ) as client:
        response = await client.get("/health/ready")

    assert list(response_json(response)["checks"]) == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_check_exception_returns_503_not_500(log_stream: StringIO) -> None:
    async def failing_check() -> ReadinessCheckResult:
        raise RuntimeError("dependency exploded")

    async with make_client(
        create_app(make_settings(), readiness_checks=(("failing_check", failing_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response)["status"] == "not_ready"


@pytest.mark.asyncio
async def test_check_exception_message_and_secret_do_not_appear_in_response(
    log_stream: StringIO,
) -> None:
    async def failing_check() -> ReadinessCheckResult:
        raise RuntimeError(f"postgres password {KNOWN_SECRET}")

    async with make_client(
        create_app(make_settings(), readiness_checks=(("failing_check", failing_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert "postgres password" not in response.text
    assert KNOWN_SECRET not in response.text
    assert response_json(response)["checks"]["failing_check"]["detail"] == "check failed"


@pytest.mark.asyncio
async def test_check_failure_is_logged_safely(log_stream: StringIO) -> None:
    async def failing_check() -> ReadinessCheckResult:
        raise RuntimeError(f"boom {KNOWN_SECRET}")

    async with make_client(
        create_app(make_settings(), readiness_checks=(("failing_check", failing_check),))
    ) as client:
        await client.get("/health/ready")

    output = log_stream.getvalue()
    records = read_log_records(log_stream)
    assert any(record["event"] == "readiness_check_failed" for record in records)
    assert KNOWN_SECRET not in output


@pytest.mark.asyncio
async def test_multiple_async_checks_are_executed_correctly(log_stream: StringIO) -> None:
    started = 0
    started_lock = asyncio.Lock()
    both_started = asyncio.Event()

    async def first_check() -> ReadinessCheckResult:
        nonlocal started
        async with started_lock:
            started += 1
            if started == 2:
                both_started.set()
        await both_started.wait()
        return ReadinessCheckResult(ready=True)

    async def second_check() -> ReadinessCheckResult:
        nonlocal started
        async with started_lock:
            started += 1
            if started == 2:
                both_started.set()
        await both_started.wait()
        return ReadinessCheckResult(ready=True)

    async with make_client(
        create_app(
            make_settings(),
            readiness_checks=(("first", first_check), ("second", second_check)),
        )
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert set(response_json(response)["checks"]) == {"first", "second"}


@pytest.mark.asyncio
async def test_request_id_does_not_leak_between_concurrent_health_requests(
    log_stream: StringIO,
) -> None:
    first_request_id = str(uuid4())
    second_request_id = str(uuid4())

    async with make_client(create_app(make_settings())) as client:
        first, second = await asyncio.gather(
            client.get("/health/ready", headers={REQUEST_ID_HEADER: first_request_id}),
            client.get("/health/ready", headers={REQUEST_ID_HEADER: second_request_id}),
        )

    assert first.headers[REQUEST_ID_HEADER] == first_request_id
    assert second.headers[REQUEST_ID_HEADER] == second_request_id


@pytest.mark.asyncio
async def test_two_apps_have_independent_readiness_checks(log_stream: StringIO) -> None:
    first = create_app(make_settings(), readiness_checks=(("first", ready_check),))
    second = create_app(make_settings(), readiness_checks=(("second", ready_check),))

    async with make_client(first) as first_client:
        first_response = await first_client.get("/health/ready")
    async with make_client(second) as second_client:
        second_response = await second_client.get("/health/ready")

    assert list(response_json(first_response)["checks"]) == ["first"]
    assert list(response_json(second_response)["checks"]) == ["second"]


@pytest.mark.asyncio
async def test_slow_check_times_out(log_stream: StringIO) -> None:
    async def slow_check() -> ReadinessCheckResult:
        await asyncio.sleep(READINESS_CHECK_TIMEOUT_SECONDS + 1)
        return ReadinessCheckResult(ready=True)

    async with make_client(
        create_app(make_settings(), readiness_checks=(("slow_check", slow_check),))
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response_json(response)["checks"]["slow_check"]["detail"] == "check timed out"


@pytest.mark.asyncio
async def test_docs_continue_private(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        response = await client.get("/docs")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_info_route_still_private(log_stream: StringIO) -> None:
    async with make_client(create_app(make_settings())) as client:
        missing = await client.get("/api/v1/info")
        authenticated = await client.get("/api/v1/info", headers={API_KEY_HEADER: EXPECTED_API_KEY})

    assert missing.status_code == 401
    assert authenticated.status_code == 200


def test_health_routes_are_not_under_api_v1() -> None:
    paths = set(create_app(make_settings()).openapi()["paths"])

    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/api/v1/health/live" not in paths
    assert "/api/v1/health/ready" not in paths


@pytest.mark.asyncio
async def test_existing_exception_handlers_still_work(log_stream: StringIO) -> None:
    app = create_app(make_settings())

    @app.get("/raise-application-error")
    async def raise_application_error() -> None:
        raise ConfigurationError()

    async with make_client(app) as client:
        response = await client.get(
            "/raise-application-error",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 500
    assert response_json(response)["error"]["code"] == "CONFIGURATION_ERROR"


@pytest.mark.asyncio
async def test_validation_exception_handler_still_works(log_stream: StringIO) -> None:
    app = create_app(make_settings())

    @app.post("/validate")
    async def validate_payload(count: int = Body()) -> dict[str, int]:
        return {"count": count}

    async with make_client(app) as client:
        response = await client.post(
            "/validate",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json=KNOWN_SECRET,
        )

    assert response.status_code == 422
    assert response_json(response)["error"]["code"] == "INVALID_REQUEST"
    assert KNOWN_SECRET not in response.text
