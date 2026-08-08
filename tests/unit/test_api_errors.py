from __future__ import annotations

import json
import logging
from io import StringIO
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from sofias_memory.api.errors import (
    ConfigurationError,
    DependencyUnavailableError,
    InvalidApiKeyError,
    MissingApiKeyError,
    SofiasMemoryError,
    current_request_id,
    request_validation_error_handler,
    sanitize_validation_errors,
    sofias_memory_error_handler,
    unexpected_error_handler,
)
from sofias_memory.api.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from sofias_memory.observability.logging import (
    bind_log_context,
    clear_log_context,
    configure_logging,
)
from sofias_memory.schemas.common import ErrorCode

KNOWN_SECRET = "SUPER_SECRET_DO_NOT_LEAK_123"


class ValidationPayload(BaseModel):
    count: int


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


def make_test_app(exception: Exception | None = None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(SofiasMemoryError, sofias_memory_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.get("/raise")
    async def raise_error() -> dict[str, str]:
        if exception is not None:
            raise exception
        return {"status": "ok"}

    @app.post("/validate")
    async def validate_payload(payload: ValidationPayload) -> dict[str, int]:
        return {"count": payload.count}

    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=RequestIdMiddleware(app),
        raise_app_exceptions=False,
    )
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def response_json(response: httpx.Response) -> dict[str, object]:
    return response.json()


def test_missing_api_key_error_contract() -> None:
    error = MissingApiKeyError()

    assert error.code == ErrorCode.MISSING_API_KEY
    assert error.status_code == 401
    assert error.message == "API key is required."


def test_invalid_api_key_error_contract() -> None:
    error = InvalidApiKeyError()

    assert error.code == ErrorCode.INVALID_API_KEY
    assert error.status_code == 403
    assert error.message == "API key is invalid."


def test_configuration_error_contract() -> None:
    error = ConfigurationError()

    assert error.code == ErrorCode.CONFIGURATION_ERROR
    assert error.status_code == 500
    assert error.message == "Application configuration is invalid."


def test_dependency_unavailable_error_contract() -> None:
    error = DependencyUnavailableError(details={"component": "postgres"})

    assert error.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert error.status_code == 503
    assert error.message == "Required dependency is unavailable."
    assert error.details == {"component": "postgres"}


def test_internal_cause_does_not_appear_in_public_message() -> None:
    cause = RuntimeError(f"postgresql://user:{KNOWN_SECRET}@host/db")
    error = ConfigurationError(cause=cause)

    assert KNOWN_SECRET not in error.message
    assert str(cause) != error.message


@pytest.mark.asyncio
async def test_application_error_handler_returns_status_and_envelope(
    log_stream: StringIO,
) -> None:
    request_id = str(uuid4())
    async with make_client(make_test_app(MissingApiKeyError())) as client:
        response = await client.get("/raise", headers={REQUEST_ID_HEADER: request_id})

    body = response_json(response)
    assert response.status_code == 401
    assert body == {
        "error": {
            "code": "MISSING_API_KEY",
            "message": "API key is required.",
            "details": {},
            "request_id": request_id,
        }
    }
    assert read_log_records(log_stream)[0]["error_code"] == "MISSING_API_KEY"


@pytest.mark.asyncio
async def test_application_error_handler_uses_request_id_from_context() -> None:
    request_id = str(uuid4())
    async with make_client(make_test_app(InvalidApiKeyError())) as client:
        response = await client.get("/raise", headers={REQUEST_ID_HEADER: request_id})

    assert response_json(response)["error"]["request_id"] == request_id


@pytest.mark.asyncio
async def test_request_validation_error_returns_422_invalid_request() -> None:
    request_id = str(uuid4())
    async with make_client(make_test_app()) as client:
        response = await client.post(
            "/validate",
            json={"count": "not-an-int"},
            headers={REQUEST_ID_HEADER: request_id},
        )

    body = response_json(response)
    assert response.status_code == 422
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["request_id"] == request_id


@pytest.mark.asyncio
async def test_request_validation_error_removes_raw_input_and_sensitive_values() -> None:
    request_id = str(uuid4())
    async with make_client(make_test_app()) as client:
        response = await client.post(
            "/validate",
            json={"count": KNOWN_SECRET},
            headers={REQUEST_ID_HEADER: request_id},
        )

    rendered = response.text
    error = response_json(response)["error"]
    details = error["details"]
    first_error = details["errors"][0]
    assert "input" not in first_error
    assert KNOWN_SECRET not in rendered


@pytest.mark.asyncio
async def test_request_validation_error_preserves_safe_error_fields() -> None:
    async with make_client(make_test_app()) as client:
        response = await client.post("/validate", json={"count": "not-an-int"})

    first_error = response_json(response)["error"]["details"]["errors"][0]
    assert first_error["loc"] == ["body", "count"]
    assert isinstance(first_error["msg"], str)
    assert isinstance(first_error["type"], str)


def test_sanitize_validation_errors_redacts_sensitive_ctx() -> None:
    sanitized = sanitize_validation_errors(
        [
            {
                "loc": ("body", "token"),
                "msg": "Invalid request.",
                "type": "value_error",
                "ctx": {"api_key": KNOWN_SECRET, "safe": "value", "unsafe_object": object()},
                "input": KNOWN_SECRET,
            }
        ]
    )

    assert sanitized == [
        {
            "loc": ["body", "token"],
            "msg": "Invalid request.",
            "type": "value_error",
            "ctx": {"api_key": "[REDACTED]", "safe": "value"},
        }
    ]
    assert KNOWN_SECRET not in str(sanitized)


@pytest.mark.asyncio
async def test_internal_error_response_is_generic_and_uses_request_id(
    log_stream: StringIO,
) -> None:
    request_id = str(uuid4())
    exception = RuntimeError(f"connection failed with {KNOWN_SECRET}")
    async with make_client(make_test_app(exception)) as client:
        response = await client.get("/raise", headers={REQUEST_ID_HEADER: request_id})

    body = response_json(response)
    assert response.status_code == 500
    assert body == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error.",
            "details": {},
            "request_id": request_id,
        }
    }
    assert KNOWN_SECRET not in response.text
    assert "connection failed" not in response.text
    assert read_log_records(log_stream)[0]["error_code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_internal_error_is_logged(log_stream: StringIO) -> None:
    async with make_client(make_test_app(RuntimeError("boom"))) as client:
        await client.get("/raise")

    [record] = read_log_records(log_stream)
    assert record["event"] == "unexpected_error"
    assert record["exception_type"] == "RuntimeError"


def test_fallback_request_id_works_without_middleware() -> None:
    clear_log_context()

    assert isinstance(current_request_id(), UUID)


def test_existing_request_id_is_preserved() -> None:
    request_id = str(uuid4())
    bind_log_context(request_id=request_id)

    assert current_request_id() == UUID(request_id)
