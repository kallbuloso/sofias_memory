from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any, cast

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
from sofias_memory.api.routes.runs import router as runs_router
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.embeddings import OpenAIEmbeddingClient
from sofias_memory.infrastructure.llm import (
    OpenAIDocumentSummaryClient,
    OpenAIKnowledgeExtractionClient,
)
from sofias_memory.infrastructure.neo4j import (
    NEO4J_NOT_READY_DETAIL,
    Neo4jProjection,
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
from sofias_memory.pipelines.steps.cognify import COGNIFY_SERVICE_RESOURCE
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService
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
    # Resolved unconditionally: the Cognify route submits against the same
    # closed registry the worker executes with (ADR-0009 SS O), so it must be
    # reachable even when a test injects its own coordinator.
    resolved_registry = (
        pipeline_registry if pipeline_registry is not None else build_default_pipeline_registry()
    )
    application.state.pipeline_registry = resolved_registry

    active_neo4j_resource = cast(
        Neo4jResource | None, getattr(application.state, "neo4j_resource", None)
    )
    graph_outbox_processor = (
        GraphOutboxProcessor(
            session_factory=application.state.postgres_session_factory,
            projection=Neo4jProjection(active_neo4j_resource),
        )
        if active_neo4j_resource is not None
        else None
    )
    # ADR-0009 SS O / PipelineContext.resources: LLM, embedding and summary
    # dependencies belong to the worker process, built once here, never per
    # HTTP request inside a route.
    pipeline_resources = build_pipeline_resources(
        resolved_settings,
        session_factory=application.state.postgres_session_factory,
    )
    application.state.pipeline_resources = pipeline_resources

    if pipeline_worker_coordinator is not None:
        application.state.pipeline_worker = pipeline_worker_coordinator
        resolved_readiness_checks = (
            ("worker", _worker_readiness_check(pipeline_worker_coordinator)),
            *resolved_readiness_checks,
        )
    elif enable_worker:
        worker_coordinator = PipelineWorkerCoordinator(
            application.state.postgres_session_factory,
            resolved_registry,
            enabled=resolved_settings.worker_enabled,
            poll_interval_ms=resolved_settings.worker_poll_interval_ms,
            stale_after_seconds=resolved_settings.worker_stale_after_seconds,
            max_concurrent_datasets=resolved_settings.worker_max_concurrent_datasets,
            graph_outbox_processor=graph_outbox_processor,
            resources=pipeline_resources,
        )
        application.state.pipeline_worker = worker_coordinator
        application.state.pipeline_recovery = PipelineRecoveryService(
            application.state.postgres_session_factory,
            resolved_registry,
            stale_after_seconds=resolved_settings.worker_stale_after_seconds,
            config_fingerprint=resolved_settings.config_fingerprint(),
        )
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
    application.include_router(runs_router, prefix="/api/v1")

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


def build_pipeline_resources(
    settings: Settings,
    *,
    session_factory: AsyncSessionFactory,
) -> Mapping[str, Any]:
    """The engine's explicitly-populated ``PipelineContext.resources`` map.

    Not a service locator: the key set is fixed and known here, nothing is
    ever resolved from request data, and a step that finds its resource
    absent fails with a typed, permanent error rather than constructing a
    provider client of its own.

    Each value is materialized on first access rather than eagerly, because
    an OpenAI-compatible client builds a full TLS/HTTP transport in its
    constructor (~0.3s each) -- a cost every ``create_app`` would otherwise
    pay even in a process that never claims a run.
    """

    def build_cognify_service() -> CognifyService:
        return CognifyService(
            settings,
            session_factory=session_factory,
            embedding_client=OpenAIEmbeddingClient(settings),
            knowledge_extraction_client=OpenAIKnowledgeExtractionClient(settings),
            document_summary_client=OpenAIDocumentSummaryClient(settings),
        )

    return _PipelineResources({COGNIFY_SERVICE_RESOURCE: build_cognify_service})


class _PipelineResources(Mapping[str, Any]):
    """Fixed-key resource mapping whose values are built on first access."""

    def __init__(self, factories: dict[str, Callable[[], Any]]) -> None:
        self._factories = factories
        self._values: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key not in self._values:
            self._values[key] = self._factories[key]()
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._factories)

    def __len__(self) -> int:
        return len(self._factories)


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
