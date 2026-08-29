from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any, cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
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
    DOCS_PUBLIC_PATHS,
    PUBLIC_PATHS,
    REQUEST_ID_HEADER,
    ApiKeyMiddleware,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    RequestMetricsMiddleware,
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
    OpenAIDatasetSummaryClient,
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
from sofias_memory.pipelines.steps.dataset_delete import (
    DATASET_DELETE_RESOURCES_RESOURCE,
    DatasetDeletePipelineResources,
)
from sofias_memory.pipelines.steps.forget import FORGET_RESOURCES_RESOURCE, ForgetPipelineResources
from sofias_memory.pipelines.steps.improve import (
    IMPROVE_RESOURCES_RESOURCE,
    ImprovePipelineResources,
)
from sofias_memory.pipelines.steps.remember import (
    REMEMBER_RESOURCES_RESOURCE,
    RememberPipelineResources,
)
from sofias_memory.services.cognify import CognifyService
from sofias_memory.services.graph_maintenance_service import GraphMaintenanceService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from sofias_memory.services.operational_metrics import (
    OperationalMetricsReporter,
    OperationalMetricsService,
)
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.summary_rebuild_service import SummaryRebuildService

WORKER_NOT_READY_DETAIL = "worker not ready"

# Allowlist, not a denylist: Swagger/OpenAPI exist only for these APP_ENV
# values. Every other value -- including typos, "staging"/"qa", and the
# "production" default -- is fail-closed to no docs surface at all.
DOCS_ENABLED_APP_ENVS = frozenset({"dev", "development"})


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
    docs_enabled = resolved_settings.app_env.strip().lower() in DOCS_ENABLED_APP_ENVS
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        redoc_url=None,
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
        neo4j_resource=active_neo4j_resource,
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

    # SM-516 SS 19-20: local operational visibility, deliberately independent
    # of worker enablement -- built unconditionally so it stays available
    # even in a disabled/read-only degraded deployment (never authoritative,
    # never touches readiness).
    application.state.operational_metrics_reporter = OperationalMetricsReporter(
        OperationalMetricsService(
            application.state.postgres_session_factory,
            stale_after_seconds=float(resolved_settings.worker_stale_after_seconds),
        )
    )

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
    # Exempted unconditionally, not gated on docs_enabled -- whether a
    # request here actually resolves to the Swagger UI/schema or a plain 404
    # is decided entirely by whether FastAPI registered the route
    # (docs_url/openapi_url/redoc_url above), not by this middleware.
    application.add_middleware(
        ApiKeyMiddleware,
        api_key=resolved_settings.api_key,
        public_paths=PUBLIC_PATHS | DOCS_PUBLIC_PATHS,
    )
    if resolved_settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_allowed_origins),
            allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"],
            allow_headers=["Content-Type", API_KEY_HEADER, REQUEST_ID_HEADER],
            allow_credentials=False,
        )
    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(RequestMetricsMiddleware)

    application.openapi = _custom_openapi(application)  # type: ignore[method-assign]

    return application


def build_pipeline_resources(
    settings: Settings,
    *,
    session_factory: AsyncSessionFactory,
    neo4j_resource: Neo4jResource | None = None,
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

    _cognify_service: CognifyService | None = None

    def build_cognify_service() -> CognifyService:
        # A single shared instance, built at most once regardless of which
        # resource key triggers it first (SM-513 SS 59): Remember's `cognify`
        # step reuses this exact object rather than constructing its own
        # LLM/embedding/summary clients.
        nonlocal _cognify_service
        if _cognify_service is None:
            _cognify_service = CognifyService(
                settings,
                session_factory=session_factory,
                embedding_client=OpenAIEmbeddingClient(settings),
                knowledge_extraction_client=OpenAIKnowledgeExtractionClient(settings),
                document_summary_client=OpenAIDocumentSummaryClient(settings),
            )
        return _cognify_service

    def build_improve_resources() -> ImprovePipelineResources:
        # One shared embedding client, reused for entity/relation embedding
        # candidates and by summary_rebuild, mirroring B4's own wiring
        # (SM-511 SS 12: avoid duplicating OpenAI-compatible clients without
        # a concrete need).
        embedding_client = OpenAIEmbeddingClient(settings)
        graph_reconciliation: GraphReconciliationService | None = None
        graph_outbox_drain: GraphOutboxBatchProcessor | None = None
        if neo4j_resource is not None:
            projection = Neo4jProjection(neo4j_resource)
            rebuild_service = GraphRebuildService(
                session_factory=session_factory,
                neo4j_resource=neo4j_resource,
                projection=projection,
            )
            graph_reconciliation = GraphReconciliationService(
                session_factory=session_factory,
                neo4j_resource=neo4j_resource,
                rebuild_service=rebuild_service,
            )
            graph_outbox_drain = GraphOutboxBatchProcessor(
                session_factory=session_factory,
                processor=GraphOutboxProcessor(
                    session_factory=session_factory, projection=projection
                ),
            )
        return ImprovePipelineResources(
            settings=settings,
            embedding_client=embedding_client,
            graph_maintenance=GraphMaintenanceService(session_factory=session_factory),
            summary_rebuild=SummaryRebuildService(
                settings,
                session_factory=session_factory,
                embedding_client=embedding_client,
                document_summary_client=OpenAIDocumentSummaryClient(settings),
                dataset_summary_client=OpenAIDatasetSummaryClient(settings),
            ),
            graph_reconciliation=graph_reconciliation,
            graph_outbox_drain=graph_outbox_drain,
        )

    def build_forget_resources() -> ForgetPipelineResources:
        graph_outbox_drain: GraphOutboxBatchProcessor | None = None
        if neo4j_resource is not None:
            projection = Neo4jProjection(neo4j_resource)
            graph_outbox_drain = GraphOutboxBatchProcessor(
                session_factory=session_factory,
                processor=GraphOutboxProcessor(
                    session_factory=session_factory, projection=projection
                ),
            )
        return ForgetPipelineResources(settings=settings, graph_outbox_drain=graph_outbox_drain)

    def build_remember_resources() -> RememberPipelineResources:
        return RememberPipelineResources(settings=settings, cognify_service=build_cognify_service())

    def build_dataset_delete_resources() -> DatasetDeletePipelineResources:
        # Same shape as Forget's own resources (SM-515, ADR-0010 D9) --
        # distinct resource key so each pipeline's wiring stays independently
        # traceable, but built independently rather than sharing Forget's
        # instance since neither declares any cross-pipeline reuse contract.
        graph_outbox_drain: GraphOutboxBatchProcessor | None = None
        if neo4j_resource is not None:
            projection = Neo4jProjection(neo4j_resource)
            graph_outbox_drain = GraphOutboxBatchProcessor(
                session_factory=session_factory,
                processor=GraphOutboxProcessor(
                    session_factory=session_factory, projection=projection
                ),
            )
        return DatasetDeletePipelineResources(
            settings=settings, graph_outbox_drain=graph_outbox_drain
        )

    return _PipelineResources(
        {
            COGNIFY_SERVICE_RESOURCE: build_cognify_service,
            IMPROVE_RESOURCES_RESOURCE: build_improve_resources,
            FORGET_RESOURCES_RESOURCE: build_forget_resources,
            REMEMBER_RESOURCES_RESOURCE: build_remember_resources,
            DATASET_DELETE_RESOURCES_RESOURCE: build_dataset_delete_resources,
        }
    )


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
    """ADR-0009 SS U, SM-516 SS 6-7: ``WORKER_ENABLED=false`` is always
    ``not ready`` (no synchronous fallback exists); ``WORKER_ENABLED=true``
    is ready only once the coordinator has started, has not (yet) stopped,
    and its background tasks (poll loop, and the graph outbox loop when one
    is expected) are still alive. A single ``PipelineRun``'s own failure
    never flips this -- only the coordinator's own background tasks dying
    unexpectedly does. Reads :attr:`PipelineWorkerCoordinator.is_operational`
    -- the same single signal new-work gating (``pipeline_submission``,
    ``dataset_delete``) reads, so readiness and write-acceptance can never
    disagree (SM-516 staging fix)."""

    async def check() -> ReadinessCheckResult:
        if not coordinator.enabled:
            return ReadinessCheckResult(ready=False, detail=WORKER_NOT_READY_DETAIL)
        ready = coordinator.is_operational
        return ReadinessCheckResult(
            ready=ready,
            detail=None if ready else WORKER_NOT_READY_DETAIL,
        )

    return check


API_KEY_SECURITY_SCHEME_NAME = "ApiKeyAuth"
"""SM-516 SS 46: ``X-API-Key`` is enforced by :class:`ApiKeyMiddleware`, an
ASGI middleware outside FastAPI's per-route dependency system -- so nothing
about it appears in the generated OpenAPI document unless added explicitly.
This documents the real runtime requirement (every private route needs the
header); it changes no runtime behavior, since the middleware already
enforces it regardless of what the schema says."""


def _custom_openapi(application: FastAPI) -> Callable[[], dict[str, Any]]:
    def openapi() -> dict[str, Any]:
        if application.openapi_schema:
            return application.openapi_schema

        schema = get_openapi(
            title=application.title,
            version=application.version,
            routes=application.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            API_KEY_SECURITY_SCHEME_NAME: {
                "type": "apiKey",
                "in": "header",
                "name": API_KEY_HEADER,
            }
        }
        exempt_paths = {path.rstrip("/") for path in PUBLIC_PATHS}
        for path, operations in schema.get("paths", {}).items():
            if path.rstrip("/") in exempt_paths:
                continue
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{API_KEY_SECURITY_SCHEME_NAME: []}]

        application.openapi_schema = schema
        return application.openapi_schema

    return openapi
