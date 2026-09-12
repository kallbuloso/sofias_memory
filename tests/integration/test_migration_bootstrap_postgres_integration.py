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

This file proves ADR-0015's SM-902 gate: classification-under-lock, exactly
one automatic upgrade under concurrency, lock-timeout/unlock/connection-loss
lock semantics, and post-migration verification -- all against real
PostgreSQL and a real subprocess. It also proves SM-903's graceful-shutdown
advisory-lock property directly (a second bootstrap cannot acquire migration
protection while a first bootstrap's critical section is still open).
Hard-termination/orphan-child safety (SM-903 Property A, `PR_SET_PDEATHSIG`)
has its own dedicated real-OS-process proof in
``tests/integration/test_migration_bootstrap_supervisor_loss_postgres_integration.py``.
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
    MigrationClassificationFailedError,
    MigrationExecutionFailedError,
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


# ---------------------------------------------------------------------------
# SM-903 Part 1: sticky failure -- multi-cycle no-new-Alembic and manual
# out-of-band recovery, both against real PostgreSQL.
# ---------------------------------------------------------------------------


class _FailOnceThenExplodeRunner:
    """Fails its first invocation, then treats any further invocation as a
    test bug -- the real proof that stickiness holds is that this runner is
    never called a second time, no matter how many more times
    `ensure_schema_current()` is called afterward."""

    def __init__(self) -> None:
        self.invocations = 0

    async def upgrade_head(self, *, database_url: str) -> int:
        self.invocations += 1
        if self.invocations > 1:
            raise AssertionError("alembic must never be invoked a second time while sticky")
        return 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sticky_failure_survives_multiple_cycles_then_recovers_without_restart(
    dedicated_database_url: str,
) -> None:
    """Backlog SM-903 (a)+(b), against real PostgreSQL throughout: a failed
    Alembic invocation -> several outer-loop-equivalent
    `ensure_schema_current()` calls -> the real advisory lock is never
    re-acquired and Alembic is never re-invoked -- then an operator repairs
    the database out-of-band (a real `alembic upgrade head` run, deliberately
    never through the bootstrap's own runner), and the *same*
    `MigrationBootstrap` instance observes this via its next real read-only
    readiness probe and proceeds normally, with no restart."""

    settings = migration_bootstrap_settings(dedicated_database_url)
    checker = PostgresReadinessChecker(settings)
    runner = _FailOnceThenExplodeRunner()
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        with pytest.raises(MigrationExecutionFailedError):
            await bootstrap.ensure_schema_current()
        assert runner.invocations == 1

        # The real advisory lock must be fully free while sticky -- proven
        # by acquiring it from a completely independent connection during
        # several outer-loop-equivalent sticky-probe cycles.
        probe_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
        try:
            for _ in range(3):
                with pytest.raises(MigrationClassificationFailedError):
                    await bootstrap.ensure_schema_current()

                async with probe_engine.connect() as probe_connection:
                    held = await probe_connection.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": MIGRATION_BOOTSTRAP_KEY},
                    )
                    assert bool(held.scalar_one()) is True, (
                        "sticky probe must never hold the real advisory lock"
                    )
                    await probe_connection.rollback()
                    released = await probe_connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
                    )
                    assert bool(released.scalar_one()) is True
                    await probe_connection.commit()
        finally:
            await probe_engine.dispose()

        assert runner.invocations == 1  # still never re-invoked

        # An operator repairs the database out-of-band -- a real Alembic
        # run through Alembic's own in-process API, deliberately never
        # through the bootstrap's own runner (mirrors `upgrade_in_process_to`'s
        # existing rationale elsewhere in this file: this is fixture-shaped
        # setup for the proof, not the thing being proven).
        config = alembic_config()
        head = single_code_head(config)
        await upgrade_in_process_to(dedicated_database_url, head)

        # The same process, same instance, no restart: the next call
        # observes the repair via a real readiness probe and proceeds.
        await bootstrap.ensure_schema_current()

        assert runner.invocations == 1  # never re-invoked, even after recovery
        assert bootstrap._migration_failed_this_process is False  # noqa: SLF001
        assert (await checker.check()).ready is True
    finally:
        await checker.dispose()


# ---------------------------------------------------------------------------
# SM-903 Part 2: graceful shutdown -- a second bootstrap cannot acquire
# migration protection while a first bootstrap's critical section is open.
# ---------------------------------------------------------------------------


class _ControllableMigrationRunner:
    """A runner whose `upgrade_head()` blocks until explicitly released --
    lets this test observe "a child is alive and the advisory lock is held,
    with a shutdown already pending" as a distinct, controllable moment,
    entirely independent of a real Alembic subprocess's actual duration."""

    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.started = asyncio.Event()
        self._release = asyncio.Event()

    async def upgrade_head(self, *, database_url: str) -> int:
        self.started.set()
        await self._release.wait()
        return self.exit_code

    def release(self) -> None:
        self._release.set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_bootstrap_cannot_acquire_lock_while_first_shutdown_pending(
    dedicated_database_url: str,
) -> None:
    """SM-903 Part 2: once bootstrap A's child is alive and A's own
    cancellation is pending (shutdown in progress, critical section not yet
    closed), the real advisory lock stays held -- a concurrent bootstrap B
    must be unable to acquire migration protection (and therefore unable to
    spawn a second Alembic) until A's critical section actually closes.
    Exercises the real PostgreSQL session-level advisory lock throughout."""

    settings_a = migration_bootstrap_settings(dedicated_database_url)
    checker_a = PostgresReadinessChecker(settings_a)
    runner_a = _ControllableMigrationRunner(exit_code=0)
    bootstrap_a = MigrationBootstrap(settings_a, readiness_checker=checker_a, runner=runner_a)

    task_a = asyncio.create_task(bootstrap_a.ensure_schema_current())
    try:
        await asyncio.wait_for(runner_a.started.wait(), timeout=10.0)

        task_a.cancel()
        # Give the cancellation a real chance to reach the shielded critical
        # section before B even tries -- A must still be alive and holding
        # the real lock the whole time.
        await asyncio.sleep(0.2)
        assert not task_a.done()

        settings_b = migration_bootstrap_settings(dedicated_database_url)
        checker_b = PostgresReadinessChecker(settings_b)
        runner_b = CountingMigrationRunner()
        bootstrap_b = MigrationBootstrap(
            settings_b,
            readiness_checker=checker_b,
            runner=runner_b,
            lock_acquire_deadline_seconds=1.0,
            lock_acquire_poll_interval_seconds=0.05,
        )
        try:
            with pytest.raises(MigrationLockTimeoutError):
                await bootstrap_b.ensure_schema_current()
            assert runner_b.invocations == 0
        finally:
            await checker_b.dispose()

        # Only now does A's child "finish" -- the critical section can
        # close, classify the (fake, non-zero-DDL) exit, and release the
        # real advisory lock.
        runner_a.release()
        with pytest.raises(asyncio.CancelledError):
            await task_a
    finally:
        await checker_a.dispose()

    # The lock is now free: a fresh bootstrap B can legitimately acquire
    # protection and perform the real migration (the database was never
    # actually touched by A's fake runner, so it is still eligible).
    checker_b2 = PostgresReadinessChecker(settings_b)
    bootstrap_b2 = MigrationBootstrap(settings_b, readiness_checker=checker_b2, runner=runner_b)
    try:
        await bootstrap_b2.ensure_schema_current()
        assert runner_b.invocations == 1
        assert (await checker_b2.check()).ready is True
    finally:
        await checker_b2.dispose()


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
