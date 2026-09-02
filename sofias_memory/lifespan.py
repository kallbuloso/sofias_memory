from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.stdlib import BoundLogger

from sofias_memory.config import Settings
from sofias_memory.infrastructure.neo4j import Neo4jResource, ensure_neo4j_schema
from sofias_memory.infrastructure.postgres import AsyncSessionFactory, dispose_async_engine
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.infrastructure.storage import SourceStorageRouter
from sofias_memory.observability.logging import configure_logging, get_logger
from sofias_memory.pipelines.registry import PipelineRegistry
from sofias_memory.services.operational_metrics import OperationalMetricsReporter
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder
from sofias_memory.services.storage_convergence import StorageConvergenceService

NEO4J_STARTUP_PROBE_QUERY = "RETURN 1 AS ok"
POSTGRES_STARTUP_PROBE_QUERY = text("SELECT 1")

BOOTSTRAP_RETRY_INTERVAL_SECONDS = 5.0
"""ADR-0011 D33: a failed bootstrap attempt (schema not current, Neo4j/
Postgres unreachable, S3 probe failure, or a convergence integrity
condition) never crash-loops the process -- it logs, waits this long, and
retries the *entire* attempt from the top. Every phase in one attempt is
independently safe to re-run (schema check is read-only; Neo4j bootstrap/
Postgres probe/pipeline recovery/S3 probe are all already idempotent by
their own existing contracts; storage convergence is idempotent by
construction, ADR-0011 D10), so restarting from scratch is simplicity, not a
correctness compromise."""

CONVERGENCE_POLL_INTERVAL_SECONDS = 5.0
"""How often the STORAGE_CONVERGING fixed-point loop re-classifies durable
state while waiting for a recovery-owned lineage to progress through the
existing worker/engine, or for an integrity condition to be resolved by an
operator. Not a busy-wait -- this is the same "poll a bounded interval,
never sleep-as-correctness" discipline the worker's own poll loop uses."""


def app_settings(app: FastAPI) -> Settings:
    settings = getattr(app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("application settings are not configured")
    return settings


def app_postgres_session_factory(app: FastAPI) -> AsyncSessionFactory:
    session_factory = getattr(app.state, "postgres_session_factory", None)
    if session_factory is None:
        raise RuntimeError("PostgreSQL session factory is not configured")
    return cast(AsyncSessionFactory, session_factory)


def app_neo4j_resource(app: FastAPI) -> Neo4jResource:
    resource = getattr(app.state, "neo4j_resource", None)
    if not isinstance(resource, Neo4jResource):
        raise RuntimeError("Neo4j resource is not configured")
    return resource


def app_pipeline_registry(app: FastAPI) -> PipelineRegistry:
    """The one closed registry this process submits against and executes with.

    Resolved once in ``create_app`` and stored on ``app.state`` so a route can
    never build a second, divergent registry: "the steps a submission
    materializes" and "the steps the worker executes" must stay the same
    definition (ADR-0009 SS O, SM-509 Part J).
    """

    registry = getattr(app.state, "pipeline_registry", None)
    if not isinstance(registry, PipelineRegistry):
        raise RuntimeError("pipeline registry is not configured")
    return registry


def app_pipeline_worker(app: FastAPI) -> PipelineWorkerCoordinator:
    coordinator = getattr(app.state, "pipeline_worker", None)
    if not isinstance(coordinator, PipelineWorkerCoordinator):
        raise RuntimeError("pipeline worker coordinator is not configured")
    return coordinator


def app_process_state_holder(app: FastAPI) -> ProcessStateHolder:
    holder = getattr(app.state, "process_state_holder", None)
    if not isinstance(holder, ProcessStateHolder):
        raise RuntimeError("process state holder is not configured")
    return holder


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """ADR-0011 D31/D33 (STORAGE-007): reaches ``yield`` immediately after
    the cheap, synchronous setup below, so the ASGI server begins routing
    ``/health/live``/``/health/ready`` right away -- schema validation, Neo4j
    bootstrap, PostgreSQL probing, stale-run recovery, worker start, and (for
    ``STORAGE_BACKEND=s3``) storage convergence all run in one background
    "bootstrap" task instead of blocking this function's own body. This is
    the fix for the previously-blocking pre-``yield`` sequence: a long
    storage-convergence pass (D33: "may take minutes or hours") could
    otherwise prevent the maintenance HTTP surface from ever becoming
    reachable, which is exactly the failure mode D33 exists to forbid.
    """

    settings = app_settings(app)
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    safe_metadata = _safe_application_metadata(settings)

    logger.info("application_starting", **safe_metadata)

    holder = getattr(app.state, "process_state_holder", None)
    if not isinstance(holder, ProcessStateHolder):
        holder = ProcessStateHolder()
        app.state.process_state_holder = holder
    # A real process boot always starts fresh, regardless of whatever state
    # a caller may have pre-populated the holder with (e.g. `create_app`'s
    # own OPERATIONAL default for apps that never run this function at all,
    # see app.py) -- lifespan is the one authoritative "boot sequence begins
    # now" trigger.
    holder.transition(ProcessState.BOOTSTRAP_MAINTENANCE)
    logger.info("process_state_transition", state=ProcessState.BOOTSTRAP_MAINTENANCE.value)

    bootstrap_task = asyncio.create_task(
        _run_bootstrap(
            settings=settings,
            holder=holder,
            session_factory=app_postgres_session_factory(app),
            postgres_readiness_checker=cast(
                PostgresReadinessChecker | None,
                getattr(app.state, "postgres_readiness_checker", None),
            ),
            neo4j_resource=cast(Neo4jResource | None, getattr(app.state, "neo4j_resource", None)),
            recovery=cast(
                PipelineRecoveryService | None, getattr(app.state, "pipeline_recovery", None)
            ),
            worker=cast(
                PipelineWorkerCoordinator | None, getattr(app.state, "pipeline_worker", None)
            ),
            source_storage_router=cast(
                SourceStorageRouter | None, getattr(app.state, "source_storage_router", None)
            ),
            convergence_service=cast(
                StorageConvergenceService | None,
                getattr(app.state, "storage_convergence_service", None),
            ),
        ),
        name="sofias-memory-bootstrap",
    )
    app.state.bootstrap_task = bootstrap_task

    try:
        yield
    finally:
        # Cooperative cancel-and-await (never an abandoned/unobserved task):
        # a bootstrap attempt in flight is interrupted at its next `await`,
        # and no destructive cleanup is ever inferred merely from
        # cancellation -- restart always recovers through durable state.
        bootstrap_task.cancel()
        await asyncio.gather(bootstrap_task, return_exceptions=True)

        worker = cast(PipelineWorkerCoordinator | None, getattr(app.state, "pipeline_worker", None))
        if worker is not None:
            # ADR-0009 SS T: stop accepting new claims and let in-flight work
            # reach a safe checkpoint *before* Neo4j/PostgreSQL are closed --
            # closing those out from under a still-unwinding execution task
            # would turn a graceful shutdown into a broken-connection crash.
            # No-op internally if the worker was never started (disabled).
            await worker.stop()
        reporter = cast(
            OperationalMetricsReporter | None,
            getattr(app.state, "operational_metrics_reporter", None),
        )
        if reporter is not None:
            await reporter.stop()
        neo4j_resource = getattr(app.state, "neo4j_resource", None)
        if neo4j_resource is not None:
            await neo4j_resource.close()
        source_storage_router = cast(
            SourceStorageRouter | None, getattr(app.state, "source_storage_router", None)
        )
        if source_storage_router is not None:
            # STORAGE-007: the one application-owned S3 client/adapter,
            # closed here after the worker (which may still be reading/
            # writing/deleting Source storage through it) has fully stopped.
            await source_storage_router.aclose()
        postgres_readiness_checker = getattr(app.state, "postgres_readiness_checker", None)
        if postgres_readiness_checker is not None:
            await postgres_readiness_checker.dispose()
        postgres_engine = getattr(app.state, "postgres_engine", None)
        if postgres_engine is not None:
            await dispose_async_engine(cast(AsyncEngine, postgres_engine))
        logger.info("application_shutdown", **safe_metadata)


async def _run_bootstrap(
    *,
    settings: Settings,
    holder: ProcessStateHolder,
    session_factory: AsyncSessionFactory,
    postgres_readiness_checker: PostgresReadinessChecker | None,
    neo4j_resource: Neo4jResource | None,
    recovery: PipelineRecoveryService | None,
    worker: PipelineWorkerCoordinator | None,
    source_storage_router: SourceStorageRouter | None,
    convergence_service: StorageConvergenceService | None,
) -> None:
    """The one application lifecycle bootstrap task (ADR-0011 D7/D31/D32/D33,
    STORAGE-007) -- not a generic background-job system, this is a single,
    named coroutine driving one process's own boot sequence to
    ``OPERATIONAL``. Retries the whole attempt, with a fixed delay, on any
    failure (never crash-loops the process, D33) -- an unexpected software
    defect is logged and retried exactly like an ordinary transient
    dependency failure; it never silently reports ``OPERATIONAL``, since
    that line is only ever reached after every gate above it succeeded.
    """

    logger = get_logger(__name__)
    while True:
        try:
            await _attempt_bootstrap(
                settings=settings,
                holder=holder,
                session_factory=session_factory,
                postgres_readiness_checker=postgres_readiness_checker,
                neo4j_resource=neo4j_resource,
                recovery=recovery,
                worker=worker,
                source_storage_router=source_storage_router,
                convergence_service=convergence_service,
                logger=logger,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - D33: never crash-loop, always retry
            logger.error(
                "bootstrap_attempt_failed",
                process_state=holder.state.value,
                exception_type=type(exc).__name__,
            )
            await asyncio.sleep(BOOTSTRAP_RETRY_INTERVAL_SECONDS)


async def _attempt_bootstrap(
    *,
    settings: Settings,
    holder: ProcessStateHolder,
    session_factory: AsyncSessionFactory,
    postgres_readiness_checker: PostgresReadinessChecker | None,
    neo4j_resource: Neo4jResource | None,
    recovery: PipelineRecoveryService | None,
    worker: PipelineWorkerCoordinator | None,
    source_storage_router: SourceStorageRouter | None,
    convergence_service: StorageConvergenceService | None,
    logger: BoundLogger,
) -> None:
    log = logger

    # D32: schema must be confirmed current before ANY Source inspection,
    # storage convergence, or normal worker/business processing -- Alembic
    # is never invoked automatically; an operator runs it explicitly.
    if postgres_readiness_checker is not None:
        result = await postgres_readiness_checker.check()
        if not result.ready:
            log.warning("bootstrap_schema_not_ready", failures=result.failures)
            raise RuntimeError("schema not current")

    if neo4j_resource is not None:
        await _bootstrap_neo4j(neo4j_resource)

    if worker is not None and worker.enabled:
        # ADR-0009 SS T / PRD 21.1: probe PostgreSQL connectivity before the
        # worker is allowed to begin claiming.
        await _probe_postgres(session_factory)

        # ADR-0009 SS I "Startup behavior" / SM-507: stale-run recovery must
        # finish completely before the worker's poll loop begins claiming.
        if recovery is not None:
            recovered = await recovery.recover_startup()
            log.info("pipeline_recovery_startup_complete", recovered=recovered)

        # Started while the process is still BOOTSTRAP_MAINTENANCE (claim
        # policy NONE) -- the poll loop is alive but claims nothing until the
        # state transition below broadens what it may claim (D31/D43).
        await worker.start()

    if settings.storage_backend == "s3":
        holder.transition(ProcessState.STORAGE_CONVERGING)
        # Final fail-closed audit: start this phase's narrowing set empty --
        # any set left over from an earlier, now-abandoned bootstrap attempt
        # (this same holder instance, retried by `_run_bootstrap`) must never
        # carry forward as if it were current.
        holder.set_recovery_owned_run_ids(())
        log.info("process_state_transition", state=ProcessState.STORAGE_CONVERGING.value)

        if source_storage_router is not None:
            # D21: exercise real put/get/delete capability, not merely that
            # credentials parse -- failure here keeps the process in
            # STORAGE_CONVERGING/NOT_READY and is retried by the outer loop.
            await source_storage_router.probe()

        if convergence_service is not None:
            await _run_convergence_to_fixed_point(convergence_service, holder, log)

    holder.transition(ProcessState.OPERATIONAL)
    log.info("process_state_transition", state=ProcessState.OPERATIONAL.value)


async def _run_convergence_to_fixed_point(
    convergence_service: StorageConvergenceService,
    holder: ProcessStateHolder,
    log: BoundLogger,
) -> None:
    """ADR-0011 D7/D31: repeat classify/converge -> let eligible recovery-
    owned runs progress through the unmodified worker/engine -> reclassify,
    until no Case-A migration remains, every classified Case-B lineage has
    reached its own legitimate durable terminal state (D31: `succeeded`,
    `failed`, or `cancelled` all count), and no integrity failure remains.
    Every iteration re-derives its decision from a fresh
    ``StorageConvergenceService.converge()`` call (itself always reading
    PostgreSQL fresh) -- never from an in-memory snapshot captured at the
    start of this loop, which durable run state could invalidate at any
    time. Bounded polling, not a CPU-tight loop.

    Final fail-closed audit: publishes this pass's own Case-B lineage's
    ``pipeline_run_id`` set to ``holder`` after every single ``converge()``
    call (not only once a fixed point is reached) -- this is exactly the
    narrowing input ``PipelineRunClaimer`` consults under
    ``ClaimPolicy.RECOVERY_ONLY`` (``services.process_state``,
    ``services.pipeline_queue_claimer``), so a genuinely recovery-owned run
    becomes claimable as soon as its own lineage is classified, without
    waiting for every other Case-B lineage/integrity condition in this same
    pass to also resolve.
    """

    while True:
        result = await convergence_service.converge()
        holder.set_recovery_owned_run_ids(
            lineage.pipeline_run_id for lineage in result.recovery_owned_case_b
        )
        if result.integrity_failures:
            log.warning(
                "storage_convergence_integrity_failures",
                count=len(result.integrity_failures),
            )
            await asyncio.sleep(CONVERGENCE_POLL_INTERVAL_SECONDS)
            continue

        pending = [lineage for lineage in result.recovery_owned_case_b if not lineage.is_terminal]
        if pending:
            log.info("storage_convergence_awaiting_recovery_owned", pending=len(pending))
            await asyncio.sleep(CONVERGENCE_POLL_INTERVAL_SECONDS)
            continue

        log.info(
            "storage_convergence_fixed_point_reached",
            migrated=result.migrated,
            already_converged=result.already_converged,
            local_duplicates_cleaned=result.local_duplicates_cleaned,
            recovery_owned_lineages=len(result.recovery_owned_case_b),
        )
        return


def _safe_application_metadata(settings: Settings) -> dict[str, str]:
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "app_env": settings.app_env,
        "config_fingerprint": settings.config_fingerprint(),
    }


async def _bootstrap_neo4j(resource: Neo4jResource) -> None:
    await resource.driver.execute_query(NEO4J_STARTUP_PROBE_QUERY, database_=resource.database)
    await ensure_neo4j_schema(resource)


async def _probe_postgres(session_factory: AsyncSessionFactory) -> None:
    session = session_factory()
    try:
        await session.execute(POSTGRES_STARTUP_PROBE_QUERY)
    finally:
        await session.close()
