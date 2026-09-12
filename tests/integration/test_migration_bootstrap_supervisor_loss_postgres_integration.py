"""Real-OS-process hard-termination/orphan-safety proof (backlog SM-903 Part
5, ADR-0015).

Every other proof in this repository for the migration bootstrap's
lifecycle (SM-902's own suite, and SM-903's graceful-shutdown proof in
``test_migration_bootstrap_postgres_integration.py``) exercises cancellation
cooperatively, inside one Python process. That is real evidence for the
*graceful* path, but it says nothing about a genuinely hard-killed
supervisor -- one whose event loop, `finally` blocks, and lock-release code
all vanish without ever running, exactly what ADR-0015's orphan-safety
invariant exists to cover: "an automatic Alembic migration must never remain
capable of mutating PostgreSQL after the serialization protection for that
execution has stopped existing."

This module supplies that missing evidence. It launches
``_migration_bootstrap_supervisor_child.py`` as a real child OS process
("supervisor A"), which acquires the real advisory lock and spawns its own
real OS child ("the grandchild") through the exact production
``OsSubprocessMigrationRunner``/`PR_SET_PDEATHSIG` code path (only the
invoked command is swapped for a long-running, non-Alembic stand-in). Once
the grandchild is confirmed alive, supervisor A is sent a real `SIGKILL` --
no graceful shutdown -- and this test proves:

- **Property A holds**: the grandchild does not outlive its hard-killed
  supervisor (this repository's chosen orphan-safety mechanism, a Linux
  `PR_SET_PDEATHSIG`, is what makes this true -- see
  ``OsSubprocessMigrationRunner`` in ``services/migration_bootstrap.py``).
- **Fresh legitimate recovery**: a new participant (B) can subsequently
  acquire real migration protection and complete a genuine migration,
  without ever assuming supervisor A's aborted attempt had completed
  anything -- distinct from, and never conflated with, the broader SM-905
  release-image/matrix hardening this repository will add later.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.services.migration_bootstrap import MigrationBootstrap
from tests.integration.test_migration_bootstrap_postgres_integration import (
    CountingMigrationRunner,
    dedicated_database_url,  # noqa: F401 - reused as a pytest fixture by name
    migration_bootstrap_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_CHILD_SCRIPT = (
    REPO_ROOT / "tests" / "integration" / "_migration_bootstrap_supervisor_child.py"
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_NEO4J_PASSWORD = "test-neo4j-password"
TEST_LLM_API_KEY = "sk-test-llm-api-key"
TEST_TIMEOUT = 30.0


def _supervisor_env(database_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["APP_ENV"] = "test"
    env["API_KEY"] = EXPECTED_API_KEY
    env["DATABASE_URL"] = database_url
    env["NEO4J_PASSWORD"] = TEST_NEO4J_PASSWORD
    env["LLM_API_KEY"] = TEST_LLM_API_KEY
    return env


def _spawn_supervisor(database_url: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(SUPERVISOR_CHILD_SCRIPT)],
        cwd=str(REPO_ROOT),
        env=_supervisor_env(database_url),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


async def _read_line_containing(
    process: subprocess.Popen[str], marker: str, *, timeout: float = TEST_TIMEOUT
) -> str:
    """Read the supervisor's (and, by inheritance, its grandchild's) stdout
    until a line containing ``marker`` appears."""

    assert process.stdout is not None
    loop = asyncio.get_event_loop()

    def _read_one() -> str:
        assert process.stdout is not None
        return process.stdout.readline()

    async def _scan() -> str:
        while True:
            line = await loop.run_in_executor(None, _read_one)
            if line == "":
                raise AssertionError(
                    f"supervisor process exited before printing a line containing {marker!r}"
                )
            if marker in line:
                return line.strip()

    return await asyncio.wait_for(_scan(), timeout=timeout)


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else -- still alive.
        return True
    return True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hard_killed_supervisor_does_not_orphan_the_migration_child(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    if sys.platform != "linux":
        # Honest platform boundary, not a weakened assertion (ADR-0015 /
        # backlog SM-903 Part 3): this repository's chosen orphan-safety
        # mechanism (`PR_SET_PDEATHSIG`) is a Linux-only kernel guarantee,
        # matching this project's actual release runtime (a Linux Docker
        # image). A supervisor process spawned by *this* interpreter on a
        # non-Linux platform (e.g. a Windows dev shell) would never install
        # the guarantee at all, so failing here would misrepresent "the
        # mechanism doesn't apply on this platform" as "the mechanism is
        # broken". Run this test's real proof from a Linux Python
        # interpreter (e.g. via WSL/`uv run --python 3.12` there) against
        # the same dedicated database.
        pytest.skip("Property A (PR_SET_PDEATHSIG) is Linux-only; run this test on Linux")

    supervisor = _spawn_supervisor(dedicated_database_url)
    try:
        await _read_line_containing(supervisor, "SUPERVISOR_STARTED")
        await _read_line_containing(supervisor, "migration_lock_acquired")
        pid_line = await _read_line_containing(supervisor, "GRANDCHILD_PID=")
        grandchild_pid = int(pid_line.split("GRANDCHILD_PID=", 1)[1].strip())
        # Confirm the grandchild is genuinely running (not merely printed a
        # PID moments before exiting) before the kill.
        await _read_line_containing(supervisor, "GRANDCHILD_HEARTBEAT")
        assert _process_is_alive(grandchild_pid), "grandchild process was not actually running"

        # Hard-kill the supervisor -- never a graceful `worker.stop()`/
        # cancellation. No Python cleanup code in the supervisor ever runs
        # from this point on.
        if hasattr(signal, "SIGKILL"):
            supervisor.send_signal(signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback, not this project's release runtime
            supervisor.kill()
        supervisor.wait(timeout=TEST_TIMEOUT)

        # Property A (ADR-0015 / backlog SM-903): the migration child cannot
        # outlive the supervisor that owned its advisory-lock protection.
        # `PR_SET_PDEATHSIG` delivers this near-instantly at the kernel
        # level; a short poll window absorbs scheduling jitter without
        # weakening the assertion itself.
        grandchild_dead = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not _process_is_alive(grandchild_pid):
                grandchild_dead = True
                break
            await asyncio.sleep(0.1)
        assert grandchild_dead, (
            "grandchild migration process outlived its hard-killed supervisor -- "
            "Property A (PR_SET_PDEATHSIG) did not hold"
        )

        # Fresh legitimate recovery: a new participant must be able to
        # acquire real migration protection and perform a genuine
        # migration -- never assuming supervisor A's aborted attempt (which
        # never ran real Alembic at all) completed anything. The advisory
        # lock is released by PostgreSQL itself once supervisor A's own
        # connection dies; this may lag slightly behind the grandchild's own
        # kernel-level death confirmed above, which is the safe ordering
        # (protection can only ever outlast the child, never the reverse).
        settings_b = migration_bootstrap_settings(dedicated_database_url)
        checker_b = PostgresReadinessChecker(settings_b)
        runner_b = CountingMigrationRunner()
        bootstrap_b = MigrationBootstrap(
            settings_b,
            readiness_checker=checker_b,
            runner=runner_b,
            lock_acquire_deadline_seconds=20.0,
            lock_acquire_poll_interval_seconds=0.2,
        )
        try:
            await bootstrap_b.ensure_schema_current()
            assert runner_b.invocations == 1
            assert (await checker_b.check()).ready is True
        finally:
            await checker_b.dispose()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=TEST_TIMEOUT)
