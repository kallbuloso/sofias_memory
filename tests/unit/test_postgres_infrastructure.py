from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from io import StringIO
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.observability.logging import configure_logging

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_LLM_API_KEY = "sk-fake-test-key"
VALID_NEO4J_PASSWORD = "fake-neo4j-password"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "llm_api_key": VALID_LLM_API_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture()
def engine() -> Iterator[AsyncEngine]:
    from sofias_memory.infrastructure.postgres import create_async_engine_from_settings

    created_engine = create_async_engine_from_settings(make_settings())
    yield created_engine
    created_engine.sync_engine.dispose()


def test_import_postgres_layer_does_not_create_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

    def fail_if_called(*args: object, **kwargs: object) -> AsyncEngine:
        raise AssertionError("engine factory should not run during import")

    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", fail_if_called)
    for module_name in list(sys.modules):
        if module_name.startswith("sofias_memory.infrastructure.postgres"):
            sys.modules.pop(module_name)

    try:
        importlib.import_module("sofias_memory.infrastructure.postgres")
    finally:
        for module_name in list(sys.modules):
            if module_name.startswith("sofias_memory.infrastructure.postgres"):
                sys.modules.pop(module_name)


def test_no_engine_or_session_global_is_created_on_import() -> None:
    import sofias_memory.infrastructure.postgres.engine as engine_module
    import sofias_memory.infrastructure.postgres.session as session_module

    assert not any(isinstance(value, AsyncEngine) for value in vars(engine_module).values())
    assert not any(isinstance(value, AsyncSession) for value in vars(session_module).values())


@pytest.mark.asyncio
async def test_engine_factory_uses_explicit_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sofias_memory.infrastructure.postgres.engine as engine_module

    original_create_async_engine = engine_module.create_async_engine
    captured_url = ""
    captured_kwargs: dict[str, object] = {}

    def create_async_engine_spy(url: str, **kwargs: object) -> AsyncEngine:
        nonlocal captured_url, captured_kwargs
        captured_url = url
        captured_kwargs = kwargs
        return original_create_async_engine(url, **kwargs)

    monkeypatch.setattr(engine_module, "create_async_engine", create_async_engine_spy)
    settings = make_settings(database_pool_size=3, database_max_overflow=4)

    created_engine = engine_module.create_async_engine_from_settings(settings)
    try:
        assert captured_url == VALID_DATABASE_URL
        assert captured_kwargs["pool_size"] == 3
        assert captured_kwargs["max_overflow"] == 4
        assert captured_kwargs["pool_pre_ping"] is True
    finally:
        await created_engine.dispose()


def test_engine_uses_postgresql_asyncpg_url(engine: AsyncEngine) -> None:
    assert engine.url.drivername == "postgresql+asyncpg"


def test_engine_factory_returns_async_engine(engine: AsyncEngine) -> None:
    assert isinstance(engine, AsyncEngine)


def test_sessionmaker_produces_async_session(engine: AsyncEngine) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory

    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        session.sync_session.close()


def test_sessionmaker_configuration(engine: AsyncEngine) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory

    session_factory = create_session_factory(engine)

    assert session_factory.kw["expire_on_commit"] is False
    assert session_factory.kw["autoflush"] is False


def test_two_sessions_are_independent(engine: AsyncEngine) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory

    session_factory = create_session_factory(engine)
    first = session_factory()
    second = session_factory()
    try:
        assert first is not second
    finally:
        first.sync_session.close()
        second.sync_session.close()


@pytest.mark.asyncio
async def test_session_scope_closes_session(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory, session_scope

    closed_sessions: list[AsyncSession] = []
    original_close = AsyncSession.close

    async def close_spy(self: AsyncSession) -> None:
        closed_sessions.append(self)
        await original_close(self)

    monkeypatch.setattr(AsyncSession, "close", close_spy)
    session_factory = create_session_factory(engine)

    async with session_scope(session_factory) as session:
        scoped_session = session

    assert closed_sessions == [scoped_session]


@pytest.mark.asyncio
async def test_session_scope_closes_session_after_exception(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory, session_scope

    closed_sessions: list[AsyncSession] = []
    original_close = AsyncSession.close

    async def close_spy(self: AsyncSession) -> None:
        closed_sessions.append(self)
        await original_close(self)

    monkeypatch.setattr(AsyncSession, "close", close_spy)
    session_factory = create_session_factory(engine)

    with pytest.raises(RuntimeError, match="boom"):
        async with session_scope(session_factory) as session:
            scoped_session = session
            raise RuntimeError("boom")

    assert closed_sessions == [scoped_session]


@pytest.mark.asyncio
async def test_transaction_scope_yields_session(engine: AsyncEngine) -> None:
    from sofias_memory.infrastructure.postgres import create_session_factory, transaction_scope

    session_factory = create_session_factory(engine)

    async with transaction_scope(session_factory) as session:
        assert isinstance(session, AsyncSession)
        assert session.in_transaction()


@pytest.mark.asyncio
async def test_dispose_async_engine_disposes_engine() -> None:
    from sofias_memory.infrastructure.postgres import (
        create_async_engine_from_settings,
        dispose_async_engine,
    )

    created_engine = create_async_engine_from_settings(make_settings())

    await dispose_async_engine(created_engine)

    assert created_engine.sync_engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_database_url_does_not_appear_in_repr_or_logs() -> None:
    from sofias_memory.infrastructure.postgres import (
        create_async_engine_from_settings,
        dispose_async_engine,
    )

    stream = StringIO()
    configure_logging("INFO", stream=stream)
    settings = make_settings()

    created_engine = create_async_engine_from_settings(settings)
    try:
        rendered_engine = repr(created_engine)
        rendered_url = str(created_engine.url)
    finally:
        await dispose_async_engine(created_engine)

    assert "db-secret" not in rendered_engine
    assert "db-secret" not in rendered_url
    assert VALID_DATABASE_URL not in stream.getvalue()


@pytest.mark.asyncio
async def test_engine_factory_does_not_mutate_settings() -> None:
    from sofias_memory.infrastructure.postgres import (
        create_async_engine_from_settings,
        dispose_async_engine,
    )

    settings = make_settings(database_pool_size=7, database_max_overflow=8)
    before = settings.model_dump()

    created_engine = create_async_engine_from_settings(settings)
    try:
        assert settings.model_dump() == before
    finally:
        await dispose_async_engine(created_engine)


def test_declarative_base_has_stable_naming_convention() -> None:
    from sofias_memory.infrastructure.postgres import NAMING_CONVENTION, Base

    assert Base.metadata.naming_convention == cast(dict[str, str], NAMING_CONVENTION)
    assert NAMING_CONVENTION["pk"] == "pk_%(table_name)s"
    assert NAMING_CONVENTION["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
