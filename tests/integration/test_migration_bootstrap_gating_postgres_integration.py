"""Real-PostgreSQL proof that Neo4j bootstrap, the PostgreSQL worker probe,
pipeline recovery, worker start, and storage convergence never begin before
ADR-0015's migration gate (``migration_bootstrap.ensure_schema_current()``)
has genuinely and legitimately completed (backlog SM-905 Part B; ADR-0011
D31/D32's ordering requirement).

Every scenario below wires `sofias_memory.lifespan._attempt_bootstrap`
directly to a real `MigrationBootstrap` against the dedicated PostgreSQL
database, paired with "exploding" stand-ins for every phase that must never
run early: touching any attribute of the fake Neo4j resource, calling the
fake worker/recovery/storage-router/convergence-service, or invoking the
private `_probe_postgres` helper (patched in, mirroring this repository's
existing `test_lifespan_bootstrap.py` monkeypatch pattern) all raise
`AssertionError` immediately. A scenario that fails before ever reaching
those phases proves they were never touched simply by not raising that
`AssertionError`; the one scenario where migration legitimately succeeds
(an actively-running migration) proves both "not before" (the exploding
stand-ins are still silent while the migration child is alive) and "exactly
here, not later" (the very next thing that happens once migration finishes
is the -- now expected -- explosion inside Neo4j bootstrap, the first phase
after the migration gate) in one continuous proof.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.infrastructure.postgres.advisory_lock_keys import MIGRATION_BOOTSTRAP_KEY
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.lifespan import _attempt_bootstrap
from sofias_memory.observability.logging import get_logger
from sofias_memory.services.migration_bootstrap import (
    MigrationBootstrap,
    MigrationClassificationFailedError,
    MigrationExecutionFailedError,
    MigrationLockTimeoutError,
    MigrationSchemaInvalidError,
)
from sofias_memory.services.process_state import ProcessStateHolder
from tests.integration.test_migration_bootstrap_postgres_integration import (
    CountingMigrationRunner,
    create_arbitrary_base_table,
    dedicated_database_url,  # noqa: F401 - reused as a pytest fixture by name
    migration_bootstrap_settings,
)
from tests.integration.test_migration_bootstrap_postgres_integration import (
    _FailOnceThenExplodeRunner as FailOnceThenExplodeRunner,
)


class _ExplodingNeo4jResource:
    """Any attribute access explodes -- proves `_bootstrap_neo4j` was never
    given a chance to run against this stand-in."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"Neo4j resource must not be touched before the migration gate passes (.{name})"
        )


class _ExplodingRecovery:
    async def recover_startup(self) -> int:
        raise AssertionError("pipeline recovery must not start before the migration gate passes")


class _ExplodingWorker:
    enabled = True

    async def start(self) -> None:
        raise AssertionError("worker must not start before the migration gate passes")


class _ExplodingSourceStorageRouter:
    async def probe(self) -> None:
        raise AssertionError("storage probe must not run before the migration gate passes")


class _ExplodingConvergenceService:
    async def converge(self) -> object:
        raise AssertionError("storage convergence must not run before the migration gate passes")


async def _exploding_probe_postgres(session_factory: object) -> None:
    del session_factory
    raise AssertionError("PostgreSQL worker probe must not run before the migration gate passes")


async def _attempt_bootstrap_with_exploding_dependents(
    *, settings: object, migration_bootstrap: MigrationBootstrap
) -> None:
    await _attempt_bootstrap(
        settings=settings,  # type: ignore[arg-type]
        holder=ProcessStateHolder(),
        session_factory=(lambda: None),  # type: ignore[arg-type,return-value]
        migration_bootstrap=migration_bootstrap,
        neo4j_resource=_ExplodingNeo4jResource(),  # type: ignore[arg-type]
        recovery=_ExplodingRecovery(),  # type: ignore[arg-type]
        worker=_ExplodingWorker(),  # type: ignore[arg-type]
        source_storage_router=_ExplodingSourceStorageRouter(),  # type: ignore[arg-type]
        convergence_service=_ExplodingConvergenceService(),  # type: ignore[arg-type]
        logger=get_logger(__name__),
    )


async def _attempt_bootstrap_storage_only_with_exploding_storage(
    *, settings: object, migration_bootstrap: MigrationBootstrap
) -> None:
    """Isolates the storage-convergence phase specifically: Neo4j/worker/
    recovery are simply disabled (``None``, a legitimately supported
    configuration -- see ``_attempt_bootstrap``'s own ``if ... is not
    None`` guards) so the only way execution can reach the exploding
    storage stand-ins is by skipping past the migration gate first."""

    await _attempt_bootstrap(
        settings=settings,  # type: ignore[arg-type]
        holder=ProcessStateHolder(),
        session_factory=(lambda: None),  # type: ignore[arg-type,return-value]
        migration_bootstrap=migration_bootstrap,
        neo4j_resource=None,
        recovery=None,
        worker=None,
        source_storage_router=_ExplodingSourceStorageRouter(),  # type: ignore[arg-type]
        convergence_service=_ExplodingConvergenceService(),  # type: ignore[arg-type]
        logger=get_logger(__name__),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_never_starts_early_while_migration_actively_running(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uses the real ``OsSubprocessMigrationRunner`` (via
    ``CountingMigrationRunner``) against a genuinely pristine schema, so the
    migration actually succeeds and legitimately reaches Neo4j bootstrap
    afterward -- unlike a fake runner, which would only prove "never starts
    early" for the uninteresting reason that post-migration verification
    itself then fails. Real `alembic upgrade head` against this project's
    full migration set takes on the order of a second or more, giving a
    real, non-instantaneous window in which to prove Neo4j/worker/storage
    are still untouched before it completes."""

    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _exploding_probe_postgres)

    settings = migration_bootstrap_settings(dedicated_database_url, database_migration_mode="auto")
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    migration_bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)

    task = asyncio.ensure_future(
        _attempt_bootstrap_with_exploding_dependents(
            settings=settings, migration_bootstrap=migration_bootstrap
        )
    )
    try:
        # The real Alembic subprocess needs a moment to even spawn; poll
        # briefly for genuine in-flight evidence without over-fitting to a
        # single environment's exact timing.
        observed_in_flight = False
        for _ in range(20):
            await asyncio.sleep(0.1)
            if task.done():
                break
            observed_in_flight = True
        assert observed_in_flight, "migration completed too fast to observe an in-flight window"

        # The very next phase this process reaches once migration
        # genuinely succeeds must be Neo4j bootstrap (the exploding
        # stand-in) -- proving the ordering is exact, not merely
        # "eventually".
        with pytest.raises(AssertionError, match="Neo4j resource must not be touched"):
            await asyncio.wait_for(task, timeout=30.0)

        assert runner.invocations == 1
    finally:
        await checker.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_never_starts_early_during_lock_wait(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _exploding_probe_postgres)

    holder_engine = create_async_engine(dedicated_database_url, pool_pre_ping=True)
    async with holder_engine.connect() as holder_connection:
        held = await holder_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        assert bool(held.scalar_one()) is True
        await holder_connection.rollback()

        settings = migration_bootstrap_settings(
            dedicated_database_url, database_migration_mode="auto"
        )
        checker = PostgresReadinessChecker(settings)
        runner = CountingMigrationRunner()
        migration_bootstrap = MigrationBootstrap(
            settings,
            readiness_checker=checker,
            runner=runner,
            lock_acquire_deadline_seconds=0.5,
            lock_acquire_poll_interval_seconds=0.05,
        )
        try:
            with pytest.raises(MigrationLockTimeoutError):
                await _attempt_bootstrap_with_exploding_dependents(
                    settings=settings, migration_bootstrap=migration_bootstrap
                )
            assert runner.invocations == 0
        finally:
            await checker.dispose()

        unlocked = await holder_connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        assert bool(unlocked.scalar_one()) is True
        await holder_connection.commit()
    await holder_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_never_starts_early_when_schema_fails_closed(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _exploding_probe_postgres)
    await create_arbitrary_base_table(dedicated_database_url)  # UNVERSIONED_NON_EMPTY_SCHEMA

    settings = migration_bootstrap_settings(dedicated_database_url, database_migration_mode="auto")
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    migration_bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        with pytest.raises(MigrationSchemaInvalidError):
            await _attempt_bootstrap_with_exploding_dependents(
                settings=settings, migration_bootstrap=migration_bootstrap
            )
        assert runner.invocations == 0
    finally:
        await checker.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_never_starts_early_while_sticky_failed(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sofias_memory.lifespan._probe_postgres", _exploding_probe_postgres)

    settings = migration_bootstrap_settings(dedicated_database_url, database_migration_mode="auto")
    checker = PostgresReadinessChecker(settings)
    runner = FailOnceThenExplodeRunner()
    migration_bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    try:
        with pytest.raises(MigrationExecutionFailedError):
            await _attempt_bootstrap_with_exploding_dependents(
                settings=settings, migration_bootstrap=migration_bootstrap
            )
        # Sticky now: a second attempt must be a read-only probe, never
        # touching Neo4j/worker/storage, and never re-invoking Alembic.
        with pytest.raises(MigrationClassificationFailedError):
            await _attempt_bootstrap_with_exploding_dependents(
                settings=settings, migration_bootstrap=migration_bootstrap
            )
        assert runner.invocations == 1
    finally:
        await checker.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_convergence_never_starts_early_while_migration_actively_running(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    """Backlog SM-905 Part C: with ``STORAGE_BACKEND=s3`` (ADR-0011), the
    storage-convergence phase must not begin until the migration gate has
    genuinely completed either -- isolated here from Neo4j/worker/recovery
    (disabled, ``None``) so a failure can only mean the storage phase itself
    ran too early."""

    settings = migration_bootstrap_settings(
        dedicated_database_url,
        database_migration_mode="auto",
        storage_backend="s3",
        storage_s3_bucket="sofias-memory-sm905-gating-test",
        storage_s3_region="us-east-1",
    )
    checker = PostgresReadinessChecker(settings)
    runner = CountingMigrationRunner()
    migration_bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)

    task = asyncio.ensure_future(
        _attempt_bootstrap_storage_only_with_exploding_storage(
            settings=settings, migration_bootstrap=migration_bootstrap
        )
    )
    try:
        observed_in_flight = False
        for _ in range(20):
            await asyncio.sleep(0.1)
            if task.done():
                break
            observed_in_flight = True
        assert observed_in_flight, "migration completed too fast to observe an in-flight window"

        with pytest.raises(AssertionError, match="storage probe must not run"):
            await asyncio.wait_for(task, timeout=30.0)

        assert runner.invocations == 1
    finally:
        await checker.dispose()
