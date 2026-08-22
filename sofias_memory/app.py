from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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
from sofias_memory.api.routes.cognify import router as cognify_router
from sofias_memory.api.routes.datasets import router as datasets_router
from sofias_memory.api.routes.feedback import router as feedback_router
from sofias_memory.api.routes.forget import router as forget_router
from sofias_memory.api.routes.graph import router as graph_router
from sofias_memory.api.routes.health import (
    ReadinessCheckRegistry,
    ReadinessCheckResult,
    validate_readiness_checks,
)
from sofias_memory.api.routes.health import router as health_router
from sofias_memory.api.routes.improve import router as improve_router
from sofias_memory.api.routes.info import router as info_router
from sofias_memory.api.routes.provenance import router as provenance_router
from sofias_memory.api.routes.recall import router as recall_router
from sofias_memory.api.routes.remember import router as remember_router
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.neo4j import (
    NEO4J_NOT_READY_DETAIL,
    Neo4jReadinessChecker,
    Neo4jResource,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.postgres import (
    AsyncSessionFactory,
    create_async_engine_from_settings,
    create_session_factory,
)
from sofias_memory.infrastructure.postgres.readiness import (
    POSTGRES_NOT_READY_DETAIL,
    PostgresReadinessChecker,
)
from sofias_memory.lifespan import lifespan
from sofias_memory.pipelines.registry import PipelineRegistry, build_default_pipeline_registry
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

WORKER_NOT_READY_DETAIL = "worker not ready"


def create_app(
    settings: Settings | None = None,
    readiness_checks: ReadinessCheckRegistry = (),
    *,
    enable_postgres_readiness: bool = True,
    postgres_readiness_checker: PostgresReadinessChecker | None = None,
    enable_neo4j: bool = True,
    neo4j_resource: Neo4jResource | None = None,
    neo4j_readiness_checker: Neo4jReadinessChecker | None = None,
    postgres_session_factory: AsyncSessionFactory | None = None,
    enable_worker: bool = True,
    pipeline_registry: PipelineRegistry | None = None,
    pipeline_worker_coordinator: PipelineWorkerCoordinator | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else load_settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    if postgres_session_factory is None:
        postgres_engine = create_async_engine_from_settings(resolved_settings)
        application.state.postgres_engine = postgres_engine
        application.state.postgres_session_factory = create_session_factory(postgres_engine)
    else:
        application.state.postgres_session_factory = postgres_session_factory
    resolved_readiness_checks = tuple(
        readiness_checks.items() if isinstance(readiness_checks, Mapping) else readiness_checks
    )
    if enable_postgres_readiness:
        postgres_checker = postgres_readiness_checker or PostgresReadinessChecker(resolved_settings)
        application.state.postgres_readiness_checker = postgres_checker
        resolved_readiness_checks = (
            ("postgres", _postgres_readiness_check(postgres_checker)),
            *resolved_readiness_checks,
        )
    if enable_neo4j:
        resource = neo4j_resource or create_neo4j_resource_from_settings(resolved_settings)
        neo4j_checker = neo4j_readiness_checker or Neo4jReadinessChecker(resource)
        application.state.neo4j_resource = resource
        application.state.neo4j_readiness_checker = neo4j_checker
        resolved_readiness_checks = (
            ("neo4j", _neo4j_readiness_check(neo4j_checker)),
            *resolved_readiness_checks,
        )
    if pipeline_worker_coordinator is not None:
        application.state.pipeline_worker = pipeline_worker_coordinator
        resolved_readiness_checks = (
            ("worker", _worker_readiness_check(pipeline_worker_coordinator)),
            *resolved_readiness_checks,
        )
    elif enable_worker:
        resolved_registry = (
            pipeline_registry
            if pipeline_registry is not None
            else build_default_pipeline_registry()
        )
        worker_coordinator = PipelineWorkerCoordinator(
            application.state.postgres_session_factory,
            resolved_registry,
            enabled=resolved_settings.worker_enabled,
            poll_interval_ms=resolved_settings.worker_poll_interval_ms,
            stale_after_seconds=resolved_settings.worker_stale_after_seconds,
            max_concurrent_datasets=resolved_settings.worker_max_concurrent_datasets,
        )
        application.state.pipeline_worker = worker_coordinator
        resolved_readiness_checks = (
            ("worker", _worker_readiness_check(worker_coordinator)),
            *resolved_readiness_checks,
        )
    application.state.readiness_checks = validate_readiness_checks(resolved_readiness_checks)

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
    application.include_router(datasets_router, prefix="/api/v1")
    application.include_router(remember_router, prefix="/api/v1")
    application.include_router(cognify_router, prefix="/api/v1")
    application.include_router(recall_router, prefix="/api/v1")
    application.include_router(feedback_router, prefix="/api/v1")
    application.include_router(improve_router, prefix="/api/v1")
    application.include_router(forget_router, prefix="/api/v1")
    application.include_router(graph_router, prefix="/api/v1")
    application.include_router(provenance_router, prefix="/api/v1")

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


def _postgres_readiness_check(
    checker: PostgresReadinessChecker,
) -> Callable[[], Awaitable[ReadinessCheckResult]]:
    async def check() -> ReadinessCheckResult:
        result = await checker.check()
        return ReadinessCheckResult(
            ready=result.ready,
            detail=None if result.ready else POSTGRES_NOT_READY_DETAIL,
        )

    return check


def _neo4j_readiness_check(
    checker: Neo4jReadinessChecker,
) -> Callable[[], Awaitable[ReadinessCheckResult]]:
    async def check() -> ReadinessCheckResult:
        result = await checker.check()
        return ReadinessCheckResult(
            ready=result.ready,
            detail=None if result.ready else NEO4J_NOT_READY_DETAIL,
        )

    return check


def _worker_readiness_check(
    coordinator: PipelineWorkerCoordinator,
) -> Callable[[], Awaitable[ReadinessCheckResult]]:
    """ADR-0009 SS U, SM-505 SS 37: ``WORKER_ENABLED=false`` is always
    ``not ready`` (no synchronous fallback exists); ``WORKER_ENABLED=true``
    is ready only once the coordinator has started and not (yet) stopped.
    Deliberately minimal -- no queue/heartbeat diagnostics here, that is
    SM-516."""

    async def check() -> ReadinessCheckResult:
        if not coordinator.enabled:
            return ReadinessCheckResult(ready=False, detail=WORKER_NOT_READY_DETAIL)
        ready = coordinator.is_running
        return ReadinessCheckResult(
            ready=ready,
            detail=None if ready else WORKER_NOT_READY_DETAIL,
        )

    return check
