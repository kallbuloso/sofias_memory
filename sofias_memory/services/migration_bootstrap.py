"""Automatic serialized migration bootstrap (ADR-0015, SM-902).

SM-901 answers "what schema state is PostgreSQL in?" (schema classification
only, `infrastructure/postgres/migration_state.py`). This module answers the
next question: "given that state and `DATABASE_MIGRATION_MODE`, may this
process safely run `alembic upgrade head` now?" -- and, for the first time,
actually acts on the answer: session-level advisory serialization, a fresh
state re-read after lock acquisition, `alembic upgrade head` as a real OS
subprocess, and post-migration verification.

Explicitly out of scope here (SM-903): the full sticky-failure read-only
recovery loop, graceful-shutdown migration-critical-section shielding
(cancellation shielding around an in-flight child), and hard-termination/
orphan-child safety (process-group supervision, parent-death signalling).
SM-902's own failure semantics are the minimum necessary to avoid the
specific runaway behavior ADR-0015 forbids -- see
:class:`MigrationExecutionFailedError`/:class:`MigrationPostVerificationFailedError`
and `lifespan._run_bootstrap`'s dedicated `except` clause for them.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.advisory_lock_keys import MIGRATION_BOOTSTRAP_KEY
from sofias_memory.infrastructure.postgres.migration_state import (
    MigrationSchemaState,
    RevisionGraph,
    classify_migration_state,
    collect_migration_state_snapshot,
    load_revision_graph,
)
from sofias_memory.infrastructure.postgres.readiness import (
    PostgresReadinessChecker,
    load_code_heads,
)
from sofias_memory.observability.logging import get_logger

REPO_ROOT = Path(__file__).resolve().parents[2]
"""Resolved the same way `migration_state.load_revision_graph()` resolves
`alembic.ini`'s location -- from the installed package's own path, never the
operator's/`uvicorn`'s working directory (backlog SM-902 S16). One directory
level shallower than `migration_state.py`'s own `parents[3]` because this
module lives one directory closer to the repo root
(`sofias_memory/services/` vs. `sofias_memory/infrastructure/postgres/`) --
a small, deliberate duplication of the same pattern rather than reaching
into another module's private helper across module boundaries."""

LOCK_ACQUIRE_DEADLINE_SECONDS = 15.0
"""Bounded wait for the migration advisory lock, never an unbounded
`pg_advisory_lock()` block (ADR-0015). Modest on purpose: a timeout here
produces only a pre-migration transient failure that the existing outer
5-second `_run_bootstrap` loop retries unconditionally (nothing has been
attempted against the schema yet), and two concurrent automatic bootstraps
racing at startup is already a rare event in this project's documented
single-replica deployment model -- 15s gives a concurrent participant's own
classification + (if any) `alembic upgrade head` a real chance to finish
inside one wait, without turning a stuck lock into a long silent hang; if it
doesn't finish in time, the waiting participant simply retries the whole
outer attempt again 5 seconds later and waits again."""

LOCK_ACQUIRE_POLL_INTERVAL_SECONDS = 0.5
"""How often `pg_try_advisory_lock` is retried while waiting -- frequent
enough that a lock freed mid-wait is picked up quickly, infrequent enough to
never resemble a busy-wait."""


class MigrationAction(StrEnum):
    """The migration policy's three possible outcomes for one (mode, schema
    state) pair -- pure, no I/O (Feature Contract v0.6.0 S5)."""

    NO_OP = "no_op"
    MIGRATE = "migrate"
    FAIL_CLOSED = "fail_closed"


_MIGRATE_ELIGIBLE_STATES = frozenset(
    {MigrationSchemaState.PRISTINE_FRESH_SCHEMA, MigrationSchemaState.KNOWN_ANCESTOR}
)


def migration_policy(
    *, mode: Literal["auto", "verify_only"], state: MigrationSchemaState
) -> MigrationAction:
    """Pure policy function, no I/O: `(mode, schema_state) -> MigrationAction`.

    Frozen behavior (Feature Contract v0.6.0 S5 / backlog SM-902 S5):

    ```text
    State                         auto          verify_only
    PRISTINE_FRESH_SCHEMA         MIGRATE       FAIL_CLOSED
    UNVERSIONED_NON_EMPTY_SCHEMA  FAIL_CLOSED   FAIL_CLOSED
    EXACT_HEAD                    NO_OP         NO_OP
    KNOWN_ANCESTOR                MIGRATE       FAIL_CLOSED
    CODE_MULTIPLE_HEADS           FAIL_CLOSED   FAIL_CLOSED
    VERSION_TABLE_EMPTY           FAIL_CLOSED   FAIL_CLOSED
    DATABASE_MULTIPLE_REVISIONS   FAIL_CLOSED   FAIL_CLOSED
    KNOWN_NON_ANCESTOR            FAIL_CLOSED   FAIL_CLOSED
    UNRECOGNIZED_REVISION         FAIL_CLOSED   FAIL_CLOSED
    ```

    `verify_only`'s row is only ever exercised as a pure function here for
    completeness/testability -- the real `verify_only` runtime path
    (`MigrationBootstrap._ensure_schema_current_verify_only`) never calls
    this function at all, since it reproduces the pre-ADR-0015 contract by
    delegating directly to `PostgresReadinessChecker`, never invoking
    Alembic or this classifier under any circumstance."""

    if state is MigrationSchemaState.EXACT_HEAD:
        return MigrationAction.NO_OP
    if state in _MIGRATE_ELIGIBLE_STATES:
        return MigrationAction.MIGRATE if mode == "auto" else MigrationAction.FAIL_CLOSED
    return MigrationAction.FAIL_CLOSED


class MigrationBootstrapError(RuntimeError):
    """Base for every failure `MigrationBootstrap.ensure_schema_current()` can
    raise."""


class MigrationClassificationFailedError(MigrationBootstrapError):
    """Pre-migration transient failure (ADR-0015 failure class 1): PostgreSQL
    was unreachable, or schema state could not be observed, before any
    Alembic subprocess was spawned -- also raised for the ordinary
    "schema not current" case in `verify_only` mode and for a not-ready
    `EXACT_HEAD` (e.g. missing extensions). Safe to retry unconditionally by
    the existing outer `_run_bootstrap` loop; nothing was attempted against
    the schema."""


class MigrationLockTimeoutError(MigrationClassificationFailedError):
    """Pre-migration transient failure: the advisory lock could not be
    acquired before `LOCK_ACQUIRE_DEADLINE_SECONDS` elapsed. No Alembic
    subprocess was spawned."""


class MigrationSchemaInvalidError(MigrationBootstrapError):
    """ADR-0015 failure class 3 (schema-invalid/classification failure): the
    freshly-classified state is fail-closed for the current mode. No
    Alembic subprocess is ever spawned for these states; behavior is
    unchanged from the pre-existing `revision_mismatch`-shaped fail-closed
    retry -- safe to retry unconditionally, same as class 1."""


class MigrationExecutionFailedError(MigrationBootstrapError):
    """ADR-0015 failure class 2 (migration execution failure), part A: the
    Alembic subprocess exited non-zero. Sticky for the remainder of this
    process's lifetime -- `lifespan._run_bootstrap` catches this
    specifically and stops retrying rather than re-invoking Alembic on the
    ordinary 5-second interval (SM-902's minimal distinction; SM-903 adds
    the full continued read-only probe in its place)."""


class MigrationPostVerificationFailedError(MigrationBootstrapError):
    """ADR-0015 failure class 2, part B: the Alembic subprocess exited zero,
    but PostgreSQL is not actually at the expected exact head afterward.
    Same stickiness as :class:`MigrationExecutionFailedError` -- exit code 0
    is never treated as sufficient proof of success on its own."""


class MigrationRunner(Protocol):
    """The one seam between policy/orchestration and actually spawning
    Alembic -- lets unit tests substitute a counting/fake runner while
    production always uses :class:`OsSubprocessMigrationRunner`."""

    async def upgrade_head(self, *, database_url: str) -> int:
        """Run `alembic upgrade head` against `database_url`. Returns the
        child process's exit code; never raises for a non-zero exit."""


class OsSubprocessMigrationRunner:
    """`alembic upgrade head` as a real OS subprocess (ADR-0015) -- never
    `alembic.command.upgrade(...)` in-process, never a shell.

    Invoked as `<this process's own interpreter> -m alembic upgrade head`:
    the same installed Alembic package/version this process already imports
    runs, with no dependency on an `alembic` console-script entry point
    being on `PATH` -- correct identically in a source checkout (`uv run
    ...`) and inside the release Docker image. `cwd` is pinned to
    `REPO_ROOT` so the child finds `alembic.ini`/`migrations/` deterministically,
    never depending on the operator's/`uvicorn`'s own working directory.
    """

    async def upgrade_head(self, *, database_url: str) -> int:
        env = os.environ.copy()
        # ADR-0015 / backlog SM-902 S17: never blindly trust an inherited
        # DATABASE_URL that could differ from this process's own
        # Settings.database_url (e.g. a stale OS env var shadowed by a
        # `.env` file) -- force it explicitly so the child always targets
        # the exact same database this running instance is configured for.
        env["DATABASE_URL"] = database_url
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            cwd=str(REPO_ROOT),
            env=env,
        )
        # No execution timeout wraps this await, by design (ADR-0015): a
        # legitimate migration may run for as long as it needs to.
        return await process.wait()


def _create_dedicated_lock_engine(settings: Settings) -> AsyncEngine:
    """A dedicated `AsyncEngine` for the migration advisory-lock connection
    only -- never the runtime business engine, never
    `PostgresReadinessChecker`'s own engine (ADR-0015: "never the same
    connection or pool used for business queries or the pre-existing
    readiness-check connection"). `NullPool` means every checkout is a
    genuinely fresh physical connection that is never handed out to any
    other caller while it is checked out -- the session-level lock this
    connection holds can never be silently released by an unrelated pool
    reclaiming it."""

    return create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)


class MigrationBootstrap:
    """ADR-0015 automatic serialized migration bootstrap orchestrator --
    the object `lifespan._attempt_bootstrap` calls in place of today's
    direct `PostgresReadinessChecker.check()` gate."""

    def __init__(
        self,
        settings: Settings,
        *,
        readiness_checker: PostgresReadinessChecker,
        runner: MigrationRunner | None = None,
        lock_engine_factory: Callable[[Settings], AsyncEngine] = _create_dedicated_lock_engine,
        revision_graph_loader: Callable[[], RevisionGraph] = load_revision_graph,
        code_heads_loader: Callable[[], frozenset[str]] = load_code_heads,
        lock_acquire_deadline_seconds: float = LOCK_ACQUIRE_DEADLINE_SECONDS,
        lock_acquire_poll_interval_seconds: float = LOCK_ACQUIRE_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._readiness_checker = readiness_checker
        self._runner = runner or OsSubprocessMigrationRunner()
        self._lock_engine_factory = lock_engine_factory
        self._revision_graph_loader = revision_graph_loader
        self._code_heads_loader = code_heads_loader
        self._deadline_seconds = lock_acquire_deadline_seconds
        self._poll_interval_seconds = lock_acquire_poll_interval_seconds
        self._logger = get_logger(__name__)

    async def ensure_schema_current(self) -> None:
        """D32 gate: raises unless the schema is confirmed current (or was
        just migrated to current) for this application version. Replaces
        `_attempt_bootstrap`'s previous direct
        `postgres_readiness_checker.check()` call at the exact same position
        in the startup sequence."""

        if self._settings.database_migration_mode == "verify_only":
            await self._ensure_schema_current_verify_only()
            return
        await self._ensure_schema_current_auto()

    async def _ensure_schema_current_verify_only(self) -> None:
        """`verify_only`: reproduces the pre-ADR-0015 contract exactly --
        the same read-only check already performed before this feature
        existed, never an advisory lock, never a migration subprocess,
        never any migration-state classification at all."""

        try:
            result = await self._readiness_checker.check()
        except Exception as exc:  # noqa: BLE001 - always a pre-migration transient failure here
            self._logger.warning("bootstrap_schema_not_ready", exception_type=type(exc).__name__)
            raise MigrationClassificationFailedError("schema not current") from exc
        if not result.ready:
            self._logger.warning("bootstrap_schema_not_ready", failures=result.failures)
            raise MigrationClassificationFailedError("schema not current")

    async def _ensure_schema_current_auto(self) -> None:
        self._logger.info("migration_bootstrap_started", mode="auto")
        code_heads = self._code_heads_loader()
        lock_engine = self._lock_engine_factory(self._settings)
        try:
            async with lock_engine.connect() as lock_connection:
                acquired = await self._acquire_lock_with_deadline(lock_connection)
                if not acquired:
                    self._logger.warning("migration_lock_timeout")
                    raise MigrationLockTimeoutError("advisory lock acquisition timed out")
                self._logger.info("migration_lock_acquired")
                await self._migrate_and_release(lock_connection, code_heads=code_heads)
        finally:
            await lock_engine.dispose()

    async def _migrate_and_release(
        self, lock_connection: AsyncConnection, *, code_heads: frozenset[str]
    ) -> None:
        """Runs the migration under the held lock, then always attempts to
        release it -- but a sticky migration-execution/post-verification
        failure (ADR-0015 failure class 2) must remain the exception that
        ultimately propagates, even if the unlock cleanup itself also fails
        (connection loss, a failed `pg_advisory_unlock`/commit). A cleanup
        failure must never silently replace a sticky failure with an
        ordinary retryable one -- that would let `_run_bootstrap` retry
        Alembic on the generic 5-second interval, exactly the runaway
        behavior ADR-0015 forbids. For every other failure here
        (classification/schema-invalid/lock/readiness transient, or
        cancellation), no automatic DDL was ever started, so a cleanup
        failure is allowed to propagate normally as an ordinary error --
        unchanged from before this restructuring."""

        try:
            await self._migrate_under_lock(lock_connection, code_heads=code_heads)
        except (MigrationExecutionFailedError, MigrationPostVerificationFailedError) as sticky_exc:
            await self._release_lock_preserving_sticky_error(lock_connection, sticky_exc)
            raise
        except BaseException:
            await self._release_lock(lock_connection)
            raise

        await self._release_lock(lock_connection)

    async def _release_lock_preserving_sticky_error(
        self, connection: AsyncConnection, sticky_error: MigrationBootstrapError
    ) -> None:
        try:
            await self._release_lock(connection)
        except Exception as cleanup_exc:  # noqa: BLE001 - sticky_error must remain what propagates
            self._logger.error(
                "migration_lock_release_failed", exception_type=type(cleanup_exc).__name__
            )
            raise sticky_error from cleanup_exc

    async def _migrate_under_lock(
        self, lock_connection: AsyncConnection, *, code_heads: frozenset[str]
    ) -> None:
        # ADR-0015: always re-derive classification from PostgreSQL AFTER
        # acquiring the lock -- never reuse anything observed before
        # waiting began. A fresh RevisionGraph is loaded here too, not
        # cached across calls, for the same reason.
        revision_graph = self._revision_graph_loader()
        snapshot = await collect_migration_state_snapshot(lock_connection, code_heads=code_heads)
        # Read-only observation only -- end the implicit transaction before
        # the (potentially long) external Alembic subprocess runs. Session-
        # level advisory locks are unaffected by COMMIT/ROLLBACK.
        await lock_connection.rollback()

        state = classify_migration_state(snapshot, revision_graph=revision_graph)
        action = migration_policy(mode="auto", state=state)

        if action is MigrationAction.NO_OP:
            self._logger.info("migration_not_required", schema_state=state.value)
            await self._verify_ready_or_raise(post_migration=False)
            return

        if action is MigrationAction.FAIL_CLOSED:
            self._logger.warning("migration_schema_invalid", schema_state=state.value)
            raise MigrationSchemaInvalidError(f"schema state {state.value} is fail-closed")

        await self._run_upgrade(state)

    async def _run_upgrade(self, state: MigrationSchemaState) -> None:
        self._logger.info("migration_upgrade_started", schema_state=state.value)
        started = time.monotonic()
        exit_code = await self._runner.upgrade_head(
            database_url=self._settings.database_url.get_secret_value()
        )
        elapsed_seconds = time.monotonic() - started

        if exit_code != 0:
            self._logger.error(
                "migration_upgrade_failed", exit_code=exit_code, elapsed_seconds=elapsed_seconds
            )
            raise MigrationExecutionFailedError(
                f"alembic upgrade head exited with code {exit_code}"
            )

        self._logger.info(
            "migration_upgrade_succeeded", exit_code=exit_code, elapsed_seconds=elapsed_seconds
        )
        await self._verify_ready_or_raise(post_migration=True)

    async def _verify_ready_or_raise(self, *, post_migration: bool) -> None:
        # Exit code 0 is never treated as sufficient proof on its own --
        # PostgreSQL is always re-read via the same readiness authority
        # `verify_only` mode and today's pre-ADR-0015 gate already use.
        try:
            result = await self._readiness_checker.check()
        except Exception as exc:  # noqa: BLE001 - context decides sticky vs. ordinary retryable
            if post_migration:
                self._logger.error(
                    "migration_post_verification_failed", exception_type=type(exc).__name__
                )
                raise MigrationPostVerificationFailedError(
                    "post-migration verification raised an exception"
                ) from exc
            self._logger.warning("bootstrap_schema_not_ready", exception_type=type(exc).__name__)
            raise MigrationClassificationFailedError("schema not current") from exc

        if not result.ready:
            if post_migration:
                self._logger.error("migration_post_verification_failed", failures=result.failures)
                raise MigrationPostVerificationFailedError(
                    "post-migration verification failed: " + ",".join(result.failures)
                )
            self._logger.warning("bootstrap_schema_not_ready", failures=result.failures)
            raise MigrationClassificationFailedError("schema not current")
        if post_migration:
            self._logger.info("migration_bootstrap_verified")

    async def _acquire_lock_with_deadline(self, connection: AsyncConnection) -> bool:
        deadline = time.monotonic() + self._deadline_seconds
        waiting_logged = False
        while True:
            result = await connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
            )
            acquired = bool(result.scalar_one())
            # Never hold an open transaction while waiting/sleeping, and
            # never while the external Alembic subprocess subsequently runs
            # -- true whether this attempt succeeded or not (backlog SM-902
            # S9). Session-level advisory locks are unaffected by this.
            await connection.rollback()
            if acquired:
                return True
            if not waiting_logged:
                # Emitted once per acquisition attempt (on the first
                # unsuccessful try), never on every poll -- avoids log spam
                # for a lock held for many poll intervals.
                self._logger.info("migration_lock_waiting")
                waiting_logged = True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._poll_interval_seconds)

    async def _release_lock(self, connection: AsyncConnection) -> None:
        result = await connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_BOOTSTRAP_KEY}
        )
        released = bool(result.scalar_one())
        await connection.commit()
        if not released:
            self._logger.warning("migration_lock_unlock_returned_false")
