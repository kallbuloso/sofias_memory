from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import cast
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sofias_memory.observability.logging import get_log_context, get_logger, redact_sensitive_data
from sofias_memory.schemas.common import ErrorBody, ErrorCode, ErrorEnvelope, JSONDetails, JSONValue

REQUEST_VALIDATION_STATUS_CODE = HTTPStatus.UNPROCESSABLE_ENTITY
INTERNAL_ERROR_MESSAGE = "Internal server error."


class SofiasMemoryError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        status_code: int,
        message: str,
        details: Mapping[str, JSONValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details: JSONDetails = dict(details or {})
        self.cause = cause


class MissingApiKeyError(SofiasMemoryError):
    def __init__(self, cause: BaseException | None = None) -> None:
        super().__init__(
            code=ErrorCode.MISSING_API_KEY,
            status_code=HTTPStatus.UNAUTHORIZED,
            message="API key is required.",
            cause=cause,
        )


class InvalidApiKeyError(SofiasMemoryError):
    def __init__(self, cause: BaseException | None = None) -> None:
        super().__init__(
            code=ErrorCode.INVALID_API_KEY,
            status_code=HTTPStatus.FORBIDDEN,
            message="API key is invalid.",
            cause=cause,
        )


class ConfigurationError(SofiasMemoryError):
    def __init__(
        self,
        message: str = "Application configuration is invalid.",
        details: Mapping[str, JSONValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            code=ErrorCode.CONFIGURATION_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message=message,
            details=details,
            cause=cause,
        )


class DependencyUnavailableError(SofiasMemoryError):
    def __init__(
        self,
        message: str = "Required dependency is unavailable.",
        details: Mapping[str, JSONValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            message=message,
            details=details,
            cause=cause,
        )


async def sofias_memory_error_handler(request: Request, exc: SofiasMemoryError) -> JSONResponse:
    del request
    request_id = current_request_id()
    get_logger(__name__).warning(
        "application_error",
        error_code=exc.code.value,
        status_code=exc.status_code,
    )
    return error_json_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id=request_id,
    )


async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request
    request_id = current_request_id()
    details: JSONDetails = {"errors": sanitize_validation_errors(_validation_error_mappings(exc))}
    get_logger(__name__).warning(
        "request_validation_error",
        error_code=ErrorCode.INVALID_REQUEST.value,
        status_code=REQUEST_VALIDATION_STATUS_CODE,
    )
    return error_json_response(
        status_code=REQUEST_VALIDATION_STATUS_CODE,
        code=ErrorCode.INVALID_REQUEST,
        message="Invalid request.",
        details=details,
        request_id=request_id,
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    request_id = current_request_id()
    get_logger(__name__).error(
        "unexpected_error",
        error_code=ErrorCode.INTERNAL_ERROR.value,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        exception_type=type(exc).__name__,
    )
    return error_json_response(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR,
        message=INTERNAL_ERROR_MESSAGE,
        request_id=request_id,
    )


def error_json_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    request_id: UUID,
    details: Mapping[str, JSONValue] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=dict(details or {}),
            request_id=request_id,
        )
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


def current_request_id() -> UUID:
    context_request_id = get_log_context().get("request_id")
    if isinstance(context_request_id, UUID):
        return context_request_id
    if isinstance(context_request_id, str):
        try:
            return UUID(context_request_id)
        except ValueError:
            return uuid4()
    return uuid4()


def sanitize_validation_errors(errors: list[Mapping[str, object]]) -> list[JSONValue]:
    return [_sanitize_validation_error(error) for error in errors]


def _validation_error_mappings(exc: RequestValidationError) -> list[Mapping[str, object]]:
    errors: list[Mapping[str, object]] = []
    for error in exc.errors():
        if isinstance(error, Mapping):
            errors.append(cast(Mapping[str, object], error))
    return errors


def _sanitize_validation_error(error: Mapping[str, object]) -> JSONDetails:
    sanitized: JSONDetails = {}

    loc = error.get("loc")
    if isinstance(loc, list | tuple):
        sanitized["loc"] = [_safe_location_part(part) for part in loc]
    elif loc is not None:
        sanitized["loc"] = [_safe_location_part(loc)]

    message = error.get("msg")
    if isinstance(message, str):
        sanitized["msg"] = message

    error_type = error.get("type")
    if isinstance(error_type, str):
        sanitized["type"] = error_type

    ctx = error.get("ctx")
    if isinstance(ctx, Mapping):
        safe_ctx = _json_safe_mapping(ctx)
        if safe_ctx:
            sanitized["ctx"] = cast(JSONValue, redact_sensitive_data(safe_ctx))

    return sanitized


def _safe_location_part(value: object) -> str | int:
    if isinstance(value, str | int):
        return value
    return str(value)


def _json_safe_mapping(mapping: Mapping[object, object]) -> JSONDetails:
    sanitized: JSONDetails = {}
    for key, value in mapping.items():
        json_value = _json_safe_value(value)
        if json_value is not None:
            sanitized[str(key)] = json_value
    return sanitized


def _json_safe_value(value: object) -> JSONValue | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, list | tuple):
        return [item for item in (_json_safe_value(child) for child in value) if item is not None]
    return None
