"""Async SQLAlchemy session factories and lifecycle helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory


def create_session_factory(engine: AsyncEngine) -> AsyncSessionFactory:
    """Create a typed async session factory.

    Sessions are short-lived and owned by the caller. ``autoflush=False`` keeps
    write boundaries explicit for future transactional outbox work.
    """

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(session_factory: AsyncSessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session and always close it without committing implicitly."""

    session = session_factory()
    try:
        yield session
    except Exception:
        if session.in_transaction():
            await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def transaction_scope(session_factory: AsyncSessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session inside an explicit SQLAlchemy transaction."""

    async with session_factory() as session, session.begin():
        yield session
