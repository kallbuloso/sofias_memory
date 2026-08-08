from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ExceptionHandler

from sofias_memory.api.errors import (
    SofiasMemoryError,
    request_validation_error_handler,
    sofias_memory_error_handler,
    unexpected_error_handler,
)
from sofias_memory.api.middleware import (
    API_KEY_HEADER,
    REQUEST_ID_HEADER,
    ApiKeyMiddleware,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    max_body_bytes_from_mebibytes,
)
from sofias_memory.api.routes.health import ReadinessCheckRegistry, validate_readiness_checks
from sofias_memory.api.routes.health import router as health_router
from sofias_memory.api.routes.info import router as info_router
from sofias_memory.config import Settings, load_settings
from sofias_memory.lifespan import lifespan


def create_app(
    settings: Settings | None = None,
    readiness_checks: ReadinessCheckRegistry = (),
) -> FastAPI:
    resolved_settings = settings if settings is not None else load_settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.readiness_checks = validate_readiness_checks(readiness_checks)

    application.add_exception_handler(
        SofiasMemoryError,
        cast(ExceptionHandler, sofias_memory_error_handler),
    )
    application.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, request_validation_error_handler),
    )
    application.add_exception_handler(Exception, unexpected_error_handler)

    application.include_router(health_router)
    application.include_router(info_router, prefix="/api/v1")

    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=max_body_bytes_from_mebibytes(resolved_settings.max_request_body_mb),
    )
    application.add_middleware(ApiKeyMiddleware, api_key=resolved_settings.api_key)
    if resolved_settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_allowed_origins),
            allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"],
            allow_headers=["Content-Type", API_KEY_HEADER, REQUEST_ID_HEADER],
            allow_credentials=False,
        )
    application.add_middleware(RequestIdMiddleware)

    return application
