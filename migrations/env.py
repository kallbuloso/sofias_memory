from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    Base,
    create_async_engine_from_settings,
    dispose_async_engine,
)

config = context.config
target_metadata = Base.metadata


def configure_logging() -> None:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)


def configure_migration_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    settings = load_settings()
    database_url = settings.database_url.get_secret_value()

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations_online() -> None:
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations_with_connection)
    finally:
        await dispose_async_engine(engine)


def run_migrations_with_connection(connection: Connection) -> None:
    configure_migration_context(connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations_online())


configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
