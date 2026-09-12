"""Automatic serialized migration bootstrap (ADR-0015, SM-902/SM-903).

SM-901 answers "what schema state is PostgreSQL in?" (schema classification
only, `infrastructure/postgres/migration_state.py`). SM-902 answered the next
question -- "given that state and `DATABASE_MIGRATION_MODE`, may this process
safely run `alembic upgrade head` now?" -- and acted on it for the first
time: session-level advisory serialization, a fresh state re-read after lock
acquisition, `alembic upgrade head` as a real OS subprocess, and post-
migration verification.

SM-903 hardens the full process lifecycle around that single automatic
attempt:

- **Sticky failure**: once a migration-execution/post-verification failure
  has occurred, this instance never invokes Alembic again for the rest of
  this process's lifetime -- `self._migration_failed_this_process` gates
  `ensure_schema_current()` to a read-only probe
  (`_probe_sticky_readiness`) instead. If an operator repairs the database
  out-of-band and the probe subsequently observes it as current, this same
  process resumes normally without a restart. A process restart always
  starts a brand-new `MigrationBootstrap` instance with no sticky marker.
- **Graceful shutdown**: once an Alembic child has been spawned,
  `{child process, advisory lock ownership}` form one critical section
  (`_run_migration_critical_section`) that a caller's cancellation can never
  abandon -- the child is always supervised to natural completion, its exit
  classified, post-verification performed, and the lock released, before
  cancellation is allowed to propagate. Implemented via `asyncio.shield`
  around an independently-running supervising task; no execution timeout is
  introduced.
- **Hard-termination / orphan safety**: `OsSubprocessMigrationRunner`
  implements ADR-0015's orphan-safety invariant via **Property A** (the
  migration child cannot outlive the supervisor that owns the advisory
  lock), using Linux's `PR_SET_PDEATHSIG` so the kernel itself kills the
  Alembic child the instant this process dies for any reason -- including a
  hard `SIGKILL`/crash that no Python cleanup code can observe. This is a
  Linux-specific guarantee (matching this project's actual release runtime,
  a Linux Docker image); it is a no-op on non-Linux platforms, which is
  documented, not silently promised.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Callable, Sequence
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
    process's lifetime (SM-903): `MigrationBootstrap` itself sets
    `self._migration_failed_this_process = True` when this is raised, which
    makes every subsequent `ensure_schema_current()` call on this instance a
    read-only probe (`_probe_sticky_readiness`) instead of a fresh automatic
    attempt -- `lifespan._run_bootstrap`'s ordinary 5-second retry loop keeps
    running, but never invokes Alembic again unless this same process
    observes (via the probe) that the database has been repaired out-of-
    band."""


class MigrationPostVerificationFailedError(MigrationBootstrapError):
    """ADR-0015 failure class 2, part B: the Alembic subprocess exited zero,
    but PostgreSQL is not actually at the expected exact head afterward.
    Same stickiness as :class:`MigrationExecutionFailedError` (SM-903) --
    exit code 0 is never treated as sufficient proof of success on its
    own."""


class MigrationRunner(Protocol):
    """The one seam between policy/orchestration and actually spawning
    Alembic -- lets unit tests substitute a counting/fake runner while
    production always uses :class:`OsSubprocessMigrationRunner`."""

    async def upgrade_head(self, *, database_url: str) -> int:
        """Run `alembic upgrade head` against `database_url`. Returns the
        child process's exit code; never raises for a non-zero exit."""


def _set_parent_death_signal_sigkill() -> None:
    """`preexec_fn` for the migration child (Linux only, SM-903 orphan
    safety, ADR-0015 Property A): runs in the forked child, before `exec`,
    and asks the kernel to send it `SIGKILL` the instant its parent (this
    supervisor process) dies for *any* reason -- crash, `SIGKILL`, or a
    normal exit that never got a chance to run Python cleanup code. This is
    the mechanism that makes "the migration child cannot outlive the
    supervisor that owns the advisory lock" a kernel-level guarantee rather
    than a best-effort promise: it does not depend on this process's own
    signal handlers, `finally` blocks, or asyncio shutdown code ever
    running.

    `PR_SET_PDEATHSIG` (`prctl(2)`) is Linux-specific; there is no POSIX
    portable equivalent. This is called only when `sys.platform == "linux"`
    (see `OsSubprocessMigrationRunner.upgrade_head`) -- on any other
    platform (e.g. a developer's `uv run` on Windows/macOS) no parent-death
    guarantee is installed, and that boundary is documented rather than
    silently promised. It matches this project's actual release runtime, a
    Linux Docker image (`AGENTS.md`).
    """

    import ctypes

    PR_SET_PDEATHSIG = 1
    SIGKILL = 9
    # A plain literal, not `signal.SIGKILL`: that attribute is only defined
    # by typeshed's Linux/POSIX stubs, and this module must still type-check
    # cleanly under a Windows mypy run (this function's only caller already
    # gates it to `sys.platform == "linux"` at runtime).
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, SIGKILL)


class OsSubprocessMigrationRunner:
    """`alembic upgrade head` as a real OS subprocess (ADR-0015) -- never
    `alembic.command.upgrade(...)` in-process, never a shell.

    Invoked by default as `<this process's own interpreter> -m alembic
    upgrade head`: the same installed Alembic package/version this process
    already imports runs, with no dependency on an `alembic` console-script
    entry point being on `PATH` -- correct identically in a source checkout
    (`uv run ...`) and inside the release Docker image. `cwd` is pinned to
    `REPO_ROOT` so the child finds `alembic.ini`/`migrations/` deterministically,
    never depending on the operator's/`uvicorn`'s own working directory.

    `command` is overridable (SM-903) strictly as a test seam: it lets
    `tests/integration`'s hard-termination/orphan-safety proof spawn a real,
    deliberately long-running child through this exact class -- the same
    `preexec_fn`/subprocess-construction code path production migrations
    use -- without actually invoking Alembic or mutating a database. The
    default is always the real `alembic upgrade head` invocation.
    """

    def __init__(self, *, command: Sequence[str] = ("-m", "alembic", "upgrade", "head")) -> None:
        self._command = tuple(command)

    async def upgrade_head(self, *, database_url: str) -> int:
        env = os.environ.copy()
        # ADR-0015 / backlog SM-902 S17: never blindly trust an inherited
        # DATABASE_URL that could differ from this process's own
        # Settings.database_url (e.g. a stale OS env var shadowed by a
        # `.env` file) -- force it explicitly so the child always targets
        # the exact same database this running instance is configured for.
        env["DATABASE_URL"] = database_url
        preexec_fn = _set_parent_death_signal_sigkill if sys.platform == "linux" else None
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *self._command,
            cwd=str(REPO_ROOT),
            env=env,
            preexec_fn=preexec_fn,
        )
        # No execution timeout wraps this await, by design (ADR-0015): a
        # legitimate migration may run for as long as it needs to. Orphan
        # safety for a hard-killed *supervisor* is handled entirely by
        # `preexec_fn` above, never by a timeout on this side.
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
        # SM-903 sticky-failure marker: in-memory only, never a durable
        # table/column, and never anything but instance-scoped -- a brand
        # new `MigrationBootstrap` (a real process restart) always starts
        # with this `False`, which is exactly how a restart is allowed a
        # fresh automatic attempt even after a prior process's sticky
        # failure (ADR-0015).
        self._migration_failed_this_process = False

    async def ensure_schema_current(self) -> None:
        """D32 gate: raises unless the schema is confirmed current (or was
        just migrated to current) for this application version. Replaces
        `_attempt_bootstrap`'s previous direct
        `postgres_readiness_checker.check()` call at the exact same position
        in the startup sequence.

        SM-903: once this instance has observed a sticky migration-execution
        or post-verification failure, every subsequent call here -- for the
        rest of this process's lifetime -- is routed to a read-only probe
        instead of a fresh automatic attempt, no matter how many times
        `lifespan._run_bootstrap`'s outer loop keeps calling this. Checked
        before the `verify_only`/`auto` mode branch below because it is a
        process-lifetime property of *this instance*, orthogonal to mode."""

        if self._migration_failed_this_process:
            await self._probe_sticky_readiness()
            return
        if self._settings.database_migration_mode == "verify_only":
            await self._ensure_schema_current_verify_only()
            return
        await self._ensure_schema_current_auto()

    async def _probe_sticky_readiness(self) -> None:
        """SM-903 sticky-failure recovery path: a read-only re-check of
        PostgreSQL, never the advisory lock, never Alembic. Two outcomes:

        - Still not ready (or the check itself raises): re-raises as an
          ordinary `MigrationClassificationFailedError`, which
          `lifespan._run_bootstrap`'s existing generic `except Exception`
          clause retries on the normal interval -- the outer loop keeps
          running, but never touches the lock or spawns Alembic while this
          flag remains set.
        - Ready: an operator has repaired the database out-of-band while
          this same process was alive. The sticky marker is cleared and
          this call returns normally, letting `ensure_schema_current`'s
          caller (`_attempt_bootstrap`) proceed past the D32 gate exactly as
          if this had been an ordinary successful `NO_OP` -- Neo4j bootstrap,
          recovery, worker start, and storage convergence all continue in
          this same process, with no restart required.
        """

        self._logger.info("migration_sticky_probe")
        try:
            result = await self._readiness_checker.check()
        except Exception as exc:  # noqa: BLE001 - sticky probe failure is ordinary/retryable
            self._logger.warning(
                "bootstrap_schema_not_ready", exception_type=type(exc).__name__, sticky=True
            )
            raise MigrationClassificationFailedError(
                "schema not current (sticky recovery probe)"
            ) from exc
        if not result.ready:
            self._logger.warning(
                "bootstrap_schema_not_ready", failures=result.failures, sticky=True
            )
            raise MigrationClassificationFailedError("schema not current (sticky recovery probe)")

        self._migration_failed_this_process = False
        self._logger.info("migration_sticky_recovered")

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
        """Classifies under the held lock, then either releases it directly
        (`NO_OP`/`FAIL_CLOSED`/any classification failure -- no Alembic
        child was ever spawned) or, for `MIGRATE`, hands off to
        `_run_migration_critical_section` (SM-903), which owns the lock
        release itself once the child has actually terminated.

        A cleanup failure while releasing the lock after a pre-`MIGRATE`
        failure is allowed to propagate normally as an ordinary error --
        nothing was attempted against the schema, so there is no sticky
        error it could mask."""

        try:
            action, state = await self._classify_under_lock(lock_connection, code_heads=code_heads)

            if action is MigrationAction.NO_OP:
                self._logger.info("migration_not_required", schema_state=state.value)
                await self._verify_ready_or_raise(post_migration=False)
                await self._release_lock(lock_connection)
                return

            if action is MigrationAction.FAIL_CLOSED:
                self._logger.warning("migration_schema_invalid", schema_state=state.value)
                raise MigrationSchemaInvalidError(f"schema state {state.value} is fail-closed")
        except BaseException:
            await self._release_lock(lock_connection)
            raise

        # action is MIGRATE. From here on, `{child process, advisory lock
        # ownership}` form one critical section (ADR-0015 / backlog SM-903):
        # this process's own cancellation/shutdown must never abandon a live
        # child or release the lock out from under it -- see
        # `_run_migration_critical_section` for how that is enforced. Lock
        # release (ordinary or sticky-preserving) happens entirely inside
        # it, never here.
        await self._run_migration_critical_section(lock_connection, state)

    async def _classify_under_lock(
        self, lock_connection: AsyncConnection, *, code_heads: frozenset[str]
    ) -> tuple[MigrationAction, MigrationSchemaState]:
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
        return migration_policy(mode="auto", state=state), state

    async def _run_migration_critical_section(
        self, lock_connection: AsyncConnection, state: MigrationSchemaState
    ) -> None:
        """SM-903 graceful-shutdown critical section: from the moment the
        Alembic child is spawned (inside `_run_upgrade_and_release`) until
        it has terminated, been classified, (on success) post-verified, and
        the advisory lock has been released -- this process's own
        cancellation must never abandon the child, close the lock
        connection, or release the lock early.

        `_run_upgrade_and_release` runs as its own `Task`, independent of
        this coroutine's own cancellation. The ordinary (never-cancelled)
        path is a single `await asyncio.shield(task)`: it returns/raises
        exactly what the task itself returns/raises, completely unchanged
        from SM-902.

        If this coroutine's caller is cancelled while the task is still
        running (e.g. `lifespan`'s `bootstrap_task.cancel()` during
        application shutdown), `shield` raises `CancelledError` here while
        the task itself keeps running untouched. That is caught, logged
        once, and this coroutine then keeps re-shielding the *same* task
        (swallowing anything it raises -- a repeated cancellation, or the
        task's own sticky failure, whose side effects, sticky flag and lock
        release, already happened inside `_run_upgrade_and_release`) until
        it is actually done. Only then is the *original* cancellation
        re-raised, so the caller's own shutdown can proceed -- never the
        task's own exception, which would misrepresent what happened as an
        ordinary retryable failure instead of a completed, safely-closed
        critical section. No execution timeout is introduced anywhere
        here."""

        task: asyncio.Task[None] = asyncio.ensure_future(
            self._run_upgrade_and_release(lock_connection, state)
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                self._logger.info("migration_shutdown_waiting_for_child")
                while not task.done():
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.shield(task)
                self._logger.info("migration_child_supervision_completed")
            # Mark the task's own outcome retrieved (never let it replace
            # this cancellation as what the caller observes) -- asyncio
            # would otherwise log an unretrieved-exception warning for a
            # sticky failure that raced with shutdown.
            if not task.cancelled():
                task.exception()
            raise

    async def _run_upgrade_and_release(
        self, lock_connection: AsyncConnection, state: MigrationSchemaState
    ) -> None:
        """The actual critical-section body, run as an independent `Task` so
        `_run_migration_critical_section` can shield it from this process's
        own cancellation. Mirrors SM-902's original sticky-preserving unlock
        semantics exactly, just scoped to the `MIGRATE` action only (`NO_OP`/
        `FAIL_CLOSED` never reach here -- see `_migrate_and_release`)."""

        try:
            await self._run_upgrade(state)
        except (MigrationExecutionFailedError, MigrationPostVerificationFailedError) as sticky_exc:
            self._migration_failed_this_process = True
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
