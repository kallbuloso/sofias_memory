from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sofias_memory.config import Settings
from sofias_memory.infrastructure.neo4j import Neo4jResource, ensure_neo4j_schema
from sofias_memory.infrastructure.postgres import AsyncSessionFactory, dispose_async_engine
from sofias_memory.observability.logging import configure_logging, get_logger
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService
from sofias_memory.services.pipeline_worker import PipelineWorkerCoordinator

NEO4J_STARTUP_PROBE_QUERY = "RETURN 1 AS ok"
POSTGRES_STARTUP_PROBE_QUERY = text("SELECT 1")


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


def app_pipeline_worker(app: FastAPI) -> PipelineWorkerCoordinator:
    coordinator = getattr(app.state, "pipeline_worker", None)
    if not isinstance(coordinator, PipelineWorkerCoordinator):
        raise RuntimeError("pipeline worker coordinator is not configured")
    return coordinator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app_settings(app)
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    safe_metadata = _safe_application_metadata(settings)

    logger.info("application_starting", **safe_metadata)
    try:
        neo4j_resource = getattr(app.state, "neo4j_resource", None)
        if neo4j_resource is not None:
            try:
                await _bootstrap_neo4j(cast(Neo4jResource, neo4j_resource))
            except Exception as exc:
                logger.error("neo4j_startup_failed", exception_type=type(exc).__name__)
                raise

        worker = cast(PipelineWorkerCoordinator | None, getattr(app.state, "pipeline_worker", None))
        if worker is not None and worker.enabled:
            # ADR-0009 SS T / PRD 21.1: the worker only starts once PostgreSQL
            # is proven healthy. `create_async_engine_from_settings` never
            # opens a connection by itself, so a small startup probe -- not a
            # second readiness framework, just the same query pattern already
            # used for Neo4j above -- is what actually proves connectivity
            # before the worker is allowed to begin claiming.
            session_factory = app_postgres_session_factory(app)
            try:
                await _probe_postgres(session_factory)
            except Exception as exc:
                logger.error("postgres_startup_probe_failed", exception_type=type(exc).__name__)
                raise

            # ADR-0009 SS I "Startup behavior" / SM-507: stale-run recovery
            # must finish completely before the worker's poll loop begins
            # claiming (recovery_finished < first_claim). A PostgreSQL
            # failure during this pass aborts startup entirely -- it must
            # never be swallowed and let the worker start over possibly
            # stale state (SM-507 SS 30).
            recovery = cast(
                PipelineRecoveryService | None, getattr(app.state, "pipeline_recovery", None)
            )
            if recovery is not None:
                try:
                    recovered = await recovery.recover_startup()
                except Exception as exc:
                    logger.error(
                        "pipeline_recovery_startup_failed", exception_type=type(exc).__name__
                    )
                    raise
                logger.info("pipeline_recovery_startup_complete", recovered=recovered)

            await worker.start()

        yield
    finally:
        worker = cast(PipelineWorkerCoordinator | None, getattr(app.state, "pipeline_worker", None))
        if worker is not None:
            # ADR-0009 SS T: stop accepting new claims and let in-flight work
            # reach a safe checkpoint *before* Neo4j/PostgreSQL are closed --
            # closing those out from under a still-unwinding execution task
            # would turn a graceful shutdown into a broken-connection crash.
            # No-op internally if the worker was never started (disabled).
            await worker.stop()
        neo4j_resource = getattr(app.state, "neo4j_resource", None)
        if neo4j_resource is not None:
            await neo4j_resource.close()
        postgres_readiness_checker = getattr(app.state, "postgres_readiness_checker", None)
        if postgres_readiness_checker is not None:
            await postgres_readiness_checker.dispose()
        postgres_engine = getattr(app.state, "postgres_engine", None)
        if postgres_engine is not None:
            await dispose_async_engine(cast(AsyncEngine, postgres_engine))
        logger.info("application_shutdown", **safe_metadata)


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
