from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from sofias_memory.config import Settings
from sofias_memory.infrastructure.neo4j import Neo4jResource, ensure_neo4j_schema
from sofias_memory.infrastructure.postgres import AsyncSessionFactory, dispose_async_engine
from sofias_memory.observability.logging import configure_logging, get_logger

NEO4J_STARTUP_PROBE_QUERY = "RETURN 1 AS ok"


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
        yield
    finally:
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
