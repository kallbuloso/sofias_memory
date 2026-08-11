from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sofias_memory.config import Settings
from sofias_memory.observability.logging import configure_logging, get_logger


def app_settings(app: FastAPI) -> Settings:
    settings = getattr(app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("application settings are not configured")
    return settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app_settings(app)
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    safe_metadata = _safe_application_metadata(settings)

    logger.info("application_starting", **safe_metadata)
    try:
        yield
    finally:
        neo4j_resource = getattr(app.state, "neo4j_resource", None)
        if neo4j_resource is not None:
            await neo4j_resource.close()
        postgres_readiness_checker = getattr(app.state, "postgres_readiness_checker", None)
        if postgres_readiness_checker is not None:
            await postgres_readiness_checker.dispose()
        logger.info("application_shutdown", **safe_metadata)


def _safe_application_metadata(settings: Settings) -> dict[str, str]:
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "app_env": settings.app_env,
        "config_fingerprint": settings.config_fingerprint(),
    }
