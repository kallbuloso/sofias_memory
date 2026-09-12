"""Real-PostgreSQL tests for ADR-0015/SM-902's automatic serialized migration
bootstrap (``sofias_memory.services.migration_bootstrap``).

Exercises the *production* ``OsSubprocessMigrationRunner`` (a real OS
subprocess running the real, installed Alembic) against a dedicated,
discardable PostgreSQL database -- never the normal development/production
database. Requires a database whose name is exactly
``sofias_memory_migration_bootstrap_test``; any other database name is
refused (fail-safe), matching this project's existing dedicated-disposable-
database integration discipline (see e.g.
``tests/integration/test_dataset_delete_postgres_integration.py``).

Out of scope here (SM-903): graceful-shutdown critical-section shielding and
hard-termination/orphan-child safety. This file proves ADR-0015's SM-902
gate: classification-under-lock, exactly one automatic upgrade under
concurrency, lock-timeout/unlock/connection-loss lock semantics, and
post-migration verification -- all against real PostgreSQL and a real
subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Mapping
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.advisory_lock_keys import MIGRATION_BOOTSTRAP_KEY
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.services.migration_bootstrap import (
    MigrationBootstrap,
    MigrationLockTimeoutError,
    MigrationSchemaInvalidError,
    OsSubprocessMigrationRunner,
)
from tests.integration.test_postgres_migration_gate import (
    alembic_config,
    single_code_head,
    single_down_revision,
)

MIGRATION_BOOTSTRAP_POSTGRES_TESTS_ENV = (
    "SOFIAS_MEMORY_RUN_MIGRATION_BOOTSTRAP_POSTGRES_INTEGRATION"
)
MIGRATION_BOOTSTRAP_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_MIGRATION_BOOTSTRAP_TEST_DATABASE_URL"
MIGRATION_BOOTSTRAP_TEST_DATABASE_NAME = "sofias_memory_migration_bootstrap_test"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_NEO4J_PASSWORD = "test-neo4j-password"
TEST_LLM_API_KEY = "sk-test-llm-api-key"


def migration_bootstrap_test_database_url(env: Mapping[str, str]) -> str:
    """Fail-safe by construction: refuses to run against anything other than
    the one dedicated, exactly-named discardable database (backlog SM-902
    S31) -- never the ordinary local dev/`cognee_db`/production database."""

    if env.get(MIGRATION_BOOTSTRAP_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {MIGRATION_BOOTSTRAP_POSTGRES_TESTS_ENV}=1 to run migration bootstrap "
            "PostgreSQL integration tests"
        )
    database_url = env.get(MIGRATION_BOOTSTRAP_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {MIGRATION_BOOTSTRAP_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("migration bootstrap PostgreSQL test database URL is invalid")
    if parsed_url.database != MIGRATION_BOOTSTRAP_TEST_DATABASE_NAME:
        pytest.skip(
            "migration bootstrap PostgreSQL tests require the exact dedicated database "
            f"{MIGRATION_BOOTSTRAP_TEST_DATABASE_NAME}"
        )
    return database_url


async def reset_dedicated_database(database_url: str) -> None:
    """Full reset of the dedicated test database's own ``public`` schema --
    safe here specifically because the caller has already validated the URL
    points at the one exactly-named dedicated database above. Never used
    against any other database."""

    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def dedicated_database_url() -> AsyncIterator[str]:
    database_url = migration_bootstrap_test_database_url(os.environ)
    await reset_dedicated_database(database_url)
    yield database_url


def migration_bootstrap_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": database_url,
        "neo4j_password": TEST_NEO4J_PASSWORD,
        "llm_api_key": TEST_LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class CountingMigrationRunner:
    """A real "counting integration wrapper" (backlog SM-902 S27/S37) --
    every invocation is counted, but each one delegates to the actual
    production :class:`OsSubprocessMigrationRunner`, so a real OS subprocess
    running the real, installed Alembic is what actually executes."""

    def __init__(self) -> None:
        self.invocations = 0
        self._inner = OsSubprocessMigrationRunner()

    async def upgrade_head(self, *, database_url: str) -> int:
        self.invocations += 1
        return await self._inner.upgrade_head(database_url=database_url)


async def read_alembic_version(database_url: str) -> frozenset[str]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            table_exists = await connection.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = 'alembic_version'
                    """
                )
            )
            if table_exists.first() is None:
                return frozenset()
            rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(str(row.version_num) for row in rows)
    finally:
        await engine.dispose()


async def upgrade_in_process_to(database_url: str, revision: str) -> None:
    """Test-fixture-only setup: prepares a known starting revision using
    Alembic's own in-process API against the dedicated database, deliberately
    never the bootstrap's own subprocess runner (backlog SM-902 S34 -- "não
    usar downgrade do bootstrap para construir o fixture"). Mirrors the exact
    pattern ``test_postgres_migration_gate.py`` already uses to point
    ``migrations/env.py`` (which calls ``load_settings()`` internally) at a
    specific database via ``DATABASE_URL``."""

    from alembic import command

    previous = os.environ.get("DATABASE_URL")
    previous_api_key = os.environ.get("API_KEY")
    previous_neo4j = os.environ.get("NEO4J_PASSWORD")
    previous_llm = os.environ.get("LLM_API_KEY")
    os.environ["DATABASE_URL"] = database_url
    os.environ["API_KEY"] = EXPECTED_API_KEY
    os.environ["NEO4J_PASSWORD"] = TEST_NEO4J_PASSWORD
    os.environ["LLM_API_KEY"] = TEST_LLM_API_KEY
    try:
        # command.upgrade() -> migrations/env.py's run_migrations_online()
        # calls asyncio.run() internally (the same nested-event-loop
        # incompatibility ADR-0015 documents for the production subprocess
        # runner) -- this test fixture calls it from a pytest-asyncio test
        # already running inside an event loop, so it must run in a
        # separate thread with its own fresh loop, exactly like the real
        # operator's synchronous CLI invocation would.
        await asyncio.to_thread(command.upgrade, alembic_config(), revision)
    finally:
        _restore_env("DATABASE_URL", previous)
        _restore_env("API_KEY", previous_api_key)
        _restore_env("NEO4J_PASSWORD", previous_neo4j)
        _restore_env("LLM_API_KEY", previous_llm)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


async def create_arbitrary_base_table(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE some_app_table (id int)"))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# S33: fresh, genuinely pristine database
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_pristine_database_migrates_automatically_to_head(
    dedicated_database_url: str,
) -> None:
    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        await bootstrap.ensure_schema_current()

        assert runner.invocations == 1
        result = await checker.check()
        assert result.ready is True
        assert await read_alembic_version(dedicated_database_url) == frozenset(
            {single_code_head(alembic_config())}
        )
    finally:
        await checker.dispose()


# ---------------------------------------------------------------------------
# S34: known ancestor
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_known_ancestor_database_migrates_automatically_to_head(
    dedicated_database_url: str,
) -> None:
    config = alembic_config()
    head = single_code_head(config)
    ancestor = single_down_revision(config, head)
    await upgrade_in_process_to(dedicated_database_url, ancestor)
    assert await read_alembic_version(dedicated_database_url) == frozenset({ancestor})

    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        await bootstrap.ensure_schema_current()

        assert runner.invocations == 1
        assert await read_alembic_version(dedicated_database_url) == frozenset({head})
        assert (await checker.check()).ready is True
    finally:
        await checker.dispose()


# ---------------------------------------------------------------------------
# S35: exact head -- zero subprocess
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_head_database_invokes_zero_subprocess(dedicated_database_url: str) -> None:
    config = alembic_config()
    head = single_code_head(config)
    await upgrade_in_process_to(dedicated_database_url, head)

    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        await bootstrap.ensure_schema_current()

        assert runner.invocations == 0
        assert (await checker.check()).ready is True
    finally:
        await checker.dispose()


# ---------------------------------------------------------------------------
# S36: unversioned non-empty -- fail closed, zero subprocess, proven directly
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unversioned_non_empty_database_fails_closed_with_zero_subprocess(
    dedicated_database_url: str,
) -> None:
    await create_arbitrary_base_table(dedicated_database_url)

    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        with pytest.raises(MigrationSchemaInvalidError, match="unversioned_non_empty_schema"):
            await bootstrap.ensure_schema_current()

        assert runner.invocations == 0
        assert await read_alembic_version(dedicated_database_url) == frozenset()
    finally:
        await checker.dispose()


# ---------------------------------------------------------------------------
# S37: two concurrent automatic bootstraps -- exactly one real upgrade
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_bootstraps_perform_exactly_one_real_upgrade(
    dedicated_database_url: str,
) -> None:
    config = alembic_config()
    head = single_code_head(config)

    settings_a = migration_bootstrap_settings(dedicated_database_url)
    settings_b = migration_bootstrap_settings(dedicated_database_url)
    checker_a = PostgresReadinessChecker(settings_a)
    checker_b = PostgresReadinessChecker(settings_b)
    runner_a = CountingMigrationRunner()
    runner_b = CountingMigrationRunner()
    bootstrap_a = MigrationBootstrap(settings_a, readiness_checker=checker_a, runner=runner_a)
    bootstrap_b = MigrationBootstrap(settings_b, readiness_checker=checker_b, runner=runner_b)
    try:
        await asyncio.gather(
            bootstrap_a.ensure_schema_current(), bootstrap_b.ensure_schema_current()
        )

        assert runner_a.invocations + runner_b.invocations == 1
        assert await read_alembic_version(dedicated_database_url) == frozenset({head})
        assert (await checker_a.check()).ready is True
    finally:
        await checker_a.dispose()
        await checker_b.dispose()


# ---------------------------------------------------------------------------
# S38: lock-wait timeout
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_acquisition_timeout_invokes_zero_subprocess_and_releases_connection(
    dedicated_database_url: str,
) -> None:
    holder_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
    async with holder_engine.connect() as holder_connection:
        held = await holder_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        assert bool(held.scalar_one()) is True
        await holder_connection.rollback()

        settings = migration_bootstrap_settings(dedicated_database_url)
        checker = PostgresReadinessChecker(settings)
        runner = CountingMigrationRunner()
        bootstrap = MigrationBootstrap(
            settings,
            readiness_checker=checker,
            runner=runner,
            lock_acquire_deadline_seconds=0.5,
            lock_acquire_poll_interval_seconds=0.05,
        )
        try:
            with pytest.raises(MigrationLockTimeoutError):
                await bootstrap.ensure_schema_current()

            assert runner.invocations == 0
        finally:
            await checker.dispose()

        unlocked = await holder_connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        assert bool(unlocked.scalar_one()) is True
        await holder_connection.commit()
    await holder_engine.dispose()


# ---------------------------------------------------------------------------
# S39: normal unlock
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_bootstrap_releases_the_advisory_lock(
    dedicated_database_url: str,
) -> None:
    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        await bootstrap.ensure_schema_current()
    finally:
        await checker.dispose()

    probe_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
    try:
        async with probe_engine.connect() as probe_connection:
            result = await probe_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
            )
            assert bool(result.scalar_one()) is True
            await probe_connection.rollback()
            unlocked = await probe_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
            )
            assert bool(unlocked.scalar_one()) is True
            await probe_connection.commit()
    finally:
        await probe_engine.dispose()


# ---------------------------------------------------------------------------
# S40: connection loss releases the session-level lock
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connection_loss_releases_the_session_level_lock(
    dedicated_database_url: str,
) -> None:
    engine_a: AsyncEngine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
    connection_a = await engine_a.connect()
    try:
        lock_result = await connection_a.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        assert bool(lock_result.scalar_one()) is True
        await connection_a.rollback()

        terminator_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
        terminated = False
        try:
            pid_result = await connection_a.execute(text("SELECT pg_backend_pid()"))
            backend_pid = int(cast(int, pid_result.scalar_one()))
            async with terminator_engine.connect() as terminator_connection:
                outcome = await terminator_connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid}
                )
                terminated = bool(outcome.scalar_one())
                await terminator_connection.rollback()
        finally:
            await terminator_engine.dispose()

        if not terminated:
            # Fail-safe reported limit (backlog SM-902 S40): this role may
            # lack pg_terminate_backend privilege against its own dedicated
            # test database -- proceed without it; the outer `finally`
            # below still explicitly closes connection_a, which is what
            # actually exercises the connection-loss guarantee in that case.
            pass
    finally:
        # Explicitly close connection_a in every case -- including when
        # pg_terminate_backend() already succeeded server-side, where this
        # close is local Python/asyncpg-side hygiene rather than what
        # triggers the loss; the backend may already be gone, so a failure
        # closing an already-terminated connection is expected and ignored.
        with contextlib.suppress(Exception):
            await connection_a.close()
        await engine_a.dispose()

    probe_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
    try:
        async with probe_engine.connect() as probe_connection:
            reacquired = await probe_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
            )
            assert bool(reacquired.scalar_one()) is True
            await probe_connection.rollback()
            unlocked = await probe_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
            )
            assert bool(unlocked.scalar_one()) is True
            await probe_connection.commit()
    finally:
        await probe_engine.dispose()


# ---------------------------------------------------------------------------
# S42: subprocess environment -- child Settings parsing does not need real
# Neo4j/LLM connectivity, only valid-shaped disposable values
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subprocess_environment_satisfies_child_settings_without_real_neo4j_or_llm(
    dedicated_database_url: str,
) -> None:
    """``migrations/env.py`` calls ``load_settings()`` -- the child process
    must be able to construct a valid ``Settings`` instance without ever
    connecting to Neo4j or an LLM provider. This is exercised implicitly by
    every other real-subprocess scenario above; this test names the
    requirement explicitly and re-proves the simplest case."""

    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        await bootstrap.ensure_schema_current()
        assert runner.invocations == 1
    finally:
        await checker.dispose()
