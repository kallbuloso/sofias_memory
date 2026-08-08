"""Async SQLAlchemy engine factory for PostgreSQL."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.config import Settings


def create_async_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Create an AsyncEngine owned by the caller.

    The database URL is a secret in Settings and is extracted only at the boundary
    where SQLAlchemy needs it. This function does not connect to PostgreSQL by
    itself and does not log the URL.
    """

    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
    )


async def dispose_async_engine(engine: AsyncEngine) -> None:
    """Dispose an engine explicitly when its owning application is shutting down."""

    await engine.dispose()
