"""Non-pytest child-process entrypoint (backlog SM-903 Part 5): plays
"supervisor A" in the hard-termination/orphan-safety proof -- acquires the
real PostgreSQL migration advisory lock and spawns a deliberately long-
running, real OS subprocess as its Alembic-shaped migration child, through
the exact same `OsSubprocessMigrationRunner` production migrations use (only
the invoked command itself is swapped out, via that class's `command=`
constructor seam -- never a mock/fake of the subprocess-spawning code path
itself).

The parent test (`test_migration_bootstrap_supervisor_loss_postgres_integration.py`)
launches this as a real child OS process, waits for the grandchild migration
process to report its own PID and confirm it is actually running, then sends
this process a real `SIGKILL` -- no graceful shutdown, no Python cleanup code
ever runs. This is exactly the scenario ADR-0015's orphan-safety invariant
exists for, and this repository's chosen mechanism (Property A,
`PR_SET_PDEATHSIG`, see `OsSubprocessMigrationRunner`) is what the parent
test verifies actually prevents the grandchild from outliving this process.

Mirrors the existing `_process_kill_child.py`/
`test_process_kill_recovery_postgres_integration.py` real-OS-process-kill
pattern already used elsewhere in this repository (GATE-B5 SS23/SS24).
"""

from __future__ import annotations

import asyncio
import os

from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.observability.logging import configure_logging
from sofias_memory.services.migration_bootstrap import (
    MigrationBootstrap,
    OsSubprocessMigrationRunner,
)

# A real OS subprocess (this same Python interpreter, `-c ...`) that prints
# its own PID once, then heartbeats at a short, fixed interval for up to 60
# seconds -- long enough for the parent test to observe it running, kill
# this supervisor, and then confirm the heartbeats actually stop. It never
# touches PostgreSQL and is never real Alembic; it stands in only for "a
# real, independently-scheduled OS child process that is currently alive".
GRANDCHILD_SCRIPT = (
    "import os, sys, time\n"
    "print('GRANDCHILD_PID=' + str(os.getpid()), flush=True)\n"
    "for _ in range(600):\n"
    "    print('GRANDCHILD_HEARTBEAT', flush=True)\n"
    "    time.sleep(0.1)\n"
)


def _settings_from_env() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=os.environ["API_KEY"],
        database_url=os.environ["DATABASE_URL"],
        neo4j_password=os.environ["NEO4J_PASSWORD"],
        llm_api_key=os.environ["LLM_API_KEY"],
        app_env="test",
    )


async def main() -> None:
    settings = _settings_from_env()
    configure_logging("INFO")
    checker = PostgresReadinessChecker(settings)
    runner = OsSubprocessMigrationRunner(command=("-c", GRANDCHILD_SCRIPT))
    bootstrap = MigrationBootstrap(settings, readiness_checker=checker, runner=runner)
    print("SUPERVISOR_STARTED", flush=True)
    try:
        # This call acquires the real advisory lock, then blocks on the
        # grandchild's `process.wait()` for as long as the grandchild lives
        # (up to its own 60s self-limit) -- the parent test SIGKILLs this
        # entire process well before that, while this await is in flight.
        await bootstrap.ensure_schema_current()
        print("SUPERVISOR_UNEXPECTEDLY_COMPLETED", flush=True)
    finally:
        await checker.dispose()


if __name__ == "__main__":
    asyncio.run(main())
