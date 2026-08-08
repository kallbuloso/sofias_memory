"""Type aliases for PostgreSQL infrastructure primitives."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type AsyncSessionFactory = async_sessionmaker[AsyncSession]
