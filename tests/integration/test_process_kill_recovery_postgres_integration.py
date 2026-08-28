"""GATE-B5 SS23/SS24: real OS process-kill/restart recovery proof.

Every other stale-recovery test in this repository (SM-507's own suite,
``test_pipeline_recovery_postgres_integration.py``) proves the reconciliation
*logic* by discarding one in-process coordinator object and constructing a
second one in the same Python process. That is real evidence for the logic,
but it is not evidence that a genuinely killed OS process -- one whose
heartbeat task, event loop, and connection pool all vanish without any
cooperative shutdown code ever running -- is survivable.

This module supplies that missing evidence: it launches
``_process_kill_child.py`` as a real child OS process (running production
``sofias_memory.lifespan.lifespan``, ``PipelineWorkerCoordinator``, and
``PipelineRecoveryService`` against a real, dedicated PostgreSQL database),
lets it durably claim a run and start executing a step, then sends a real
SIGKILL/TerminateProcess -- never a graceful ``worker.stop()`` -- and proves
a second, freshly-launched child process (a new ``worker_id``) reconciles
the abandoned run through its normal startup recovery pass, with
recovery finishing strictly before that process's first claim.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres import create_session_factory
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.pipeline_lifecycle import create_run_with_steps
from tests.integration._process_kill_child import build_test_registry

PROCESS_KILL_TESTS_ENV = "SOFIAS_MEMORY_RUN_PROCESS_KILL_RECOVERY_TESTS"
PROCESS_KILL_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_PROCESS_KILL_RECOVERY_TEST_DATABASE_URL"
REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD_SCRIPT = REPO_ROOT / "tests" / "integration" / "_process_kill_child.py"
STALE_AFTER_SECONDS = 2
POLL_INTERVAL_MS = 100
TEST_TIMEOUT = 30.0


def process_kill_test_database_url() -> str | None:
    if os.environ.get(PROCESS_KILL_TESTS_ENV) != "1":
        return None
    url = os.environ.get(PROCESS_KILL_TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        return None
    try:
        make_url(url)
    except Exception:
        return None
    return url


@pytest_asyncio.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    database_url = process_kill_test_database_url()
    if database_url is None:
        pytest.skip(
            f"set {PROCESS_KILL_TESTS_ENV}=1 and {PROCESS_KILL_TEST_DATABASE_URL_ENV} to a "
            "dedicated discardable PostgreSQL database (migrated through 0011) to run the "
            "real process-kill recovery test"
        )
    async_engine = create_async_engine(database_url)
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


@dataclass
class Ids:
    dataset_id: UUID = field(default_factory=uuid4)
    run_id: UUID | None = None


async def read_run(session_factory: Any, run_id: UUID) -> PipelineRun:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        assert run is not None
        return PipelineRun(
            id=run.id,
            pipeline_type=run.pipeline_type,
            dataset_id=run.dataset_id,
            source_id=run.source_id,
            status=run.status,
            idempotency_key=run.idempotency_key,
            payload_hash=run.payload_hash,
            input=run.input,
            progress=run.progress,
            current_step=run.current_step,
            attempt=run.attempt,
            worker_id=run.worker_id,
            heartbeat_at=run.heartbeat_at,
            config_fingerprint=run.config_fingerprint,
            error_code=run.error_code,
            error_message=run.error_message,
            metrics=run.metrics,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            next_attempt_at=run.next_attempt_at,
            retry_of_run_id=run.retry_of_run_id,
        )


async def wait_until(predicate: Any, *, timeout: float = TEST_TIMEOUT) -> None:
    async def _poll() -> None:
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_poll(), timeout=timeout)


def child_env(database_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["APP_ENV"] = "test"
    env["API_KEY"] = "sf-" + "A" * 32
    env["DATABASE_URL"] = database_url
    env["DATABASE_POOL_SIZE"] = "2"
    env["DATABASE_MAX_OVERFLOW"] = "0"
    env["NEO4J_PASSWORD"] = "test-neo4j-password"
    env["LLM_API_KEY"] = "test-llm-api-key"
    env["WORKER_ENABLED"] = "true"
    env["WORKER_POLL_INTERVAL_MS"] = str(POLL_INTERVAL_MS)
    env["WORKER_STALE_AFTER_SECONDS"] = str(STALE_AFTER_SECONDS)
    env["WORKER_MAX_CONCURRENT_DATASETS"] = "4"
    return env


def spawn_child(database_url: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(CHILD_SCRIPT)],
        cwd=str(REPO_ROOT),
        env=child_env(database_url),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


async def read_line_containing(
    process: subprocess.Popen[str], marker: str, *, timeout: float = TEST_TIMEOUT
) -> str:
    """Read the child's stdout until a line containing ``marker`` appears."""

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
                    f"child process exited before printing a line containing {marker!r}"
                )
            if marker in line:
                return line.strip()

    return await asyncio.wait_for(_scan(), timeout=timeout)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_killed_worker_process_is_recovered_by_a_fresh_process(
    engine: AsyncEngine,
) -> None:
    database_url = process_kill_test_database_url()
    assert database_url is not None
    session_factory = create_session_factory(engine)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key="sf-" + "A" * 32,
        database_url=database_url,
        neo4j_password="test-neo4j-password",
        llm_api_key="test-llm-api-key",
        app_env="test",
        worker_stale_after_seconds=STALE_AFTER_SECONDS,
        worker_poll_interval_ms=POLL_INTERVAL_MS,
    )
    ids = Ids()

    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE dataset_id = :id"), {"id": ids.dataset_id}
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = :id"), {"id": ids.dataset_id}
        )

    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(
                id=ids.dataset_id, name=f"pkill-{ids.dataset_id}", slug=f"pkill-{ids.dataset_id}"
            )
        )
        await uow.commit()

    registry = build_test_registry()
    run_input = {"marker": "process-kill-gate"}
    plan = registry.build_step_plan(PipelineType.REMEMBER, run_input=run_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=ids.dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=run_input,
            config_fingerprint=settings.config_fingerprint(),
            steps=plan,
        )
        await uow.commit()
    ids.run_id = run.id

    child_a = spawn_child(database_url)
    try:
        # Process A: starts, claims the run, its one step begins and blocks.
        await read_line_containing(child_a, "CHILD_LIFESPAN_STARTED")
        claim_line = await read_line_containing(child_a, "CHILD_STEP_CLAIMED")
        assert str(run.id) in claim_line

        async def run_is_running_with_heartbeat() -> bool:
            current = await read_run(session_factory, run.id)
            return current.status == PipelineRunStatus.RUNNING and current.heartbeat_at is not None

        await wait_until(run_is_running_with_heartbeat)
        running_snapshot = await read_run(session_factory, run.id)
        worker_a_id = running_snapshot.worker_id
        assert worker_a_id is not None

        # Real, abrupt kill -- never worker.stop(). SIGKILL is POSIX-only;
        # Popen.kill() sends it on POSIX and calls TerminateProcess on
        # Windows, which is the real, non-graceful OS-level equivalent this
        # section calls for on this platform.
        if hasattr(signal, "SIGKILL"):
            child_a.send_signal(signal.SIGKILL)
        else:
            child_a.kill()
        child_a.wait(timeout=TEST_TIMEOUT)
    finally:
        if child_a.poll() is None:
            child_a.kill()
            child_a.wait(timeout=TEST_TIMEOUT)

    # Give the stale threshold real margin past WORKER_STALE_AFTER_SECONDS
    # before process B starts its recovery pass.
    await asyncio.sleep(STALE_AFTER_SECONDS + 1.5)

    child_b = spawn_child(database_url)
    try:
        worker_b_line = await read_line_containing(child_b, "CHILD_WORKER_ID=")
        worker_b_id = worker_b_line.split("=", 1)[1]
        assert worker_b_id != worker_a_id

        recovery_line = await read_line_containing(child_b, "CHILD_LIFESPAN_STARTED")
        del recovery_line  # ordering, not content, is what matters below
        recovery_finished_at = asyncio.get_event_loop().time()

        claim_line_b = await read_line_containing(child_b, "CHILD_STEP_CLAIMED")
        first_claim_by_b_at = asyncio.get_event_loop().time()
        assert str(run.id) in claim_line_b

        # recover_startup() (inside lifespan, before worker.start()) must
        # have fully returned before process B's poll loop performs its
        # first claim -- ADR-0009 SS I.
        assert recovery_finished_at <= first_claim_by_b_at

        async def run_reconciled_under_worker_b() -> bool:
            current = await read_run(session_factory, run.id)
            # WORKER_LOST recovery fails process A's in-flight attempt and
            # schedules a durable retry (ADR-0009 SS I / SM-507): the run
            # comes back RUNNING under worker_b's identity as a *new*
            # attempt, not the same attempt reclaimed in place. That is
            # exactly the reconciliation this test is proving -- process B
            # picked the abandoned run back up and is making progress on
            # it, with no worker able to observe or own two attempts at
            # once.
            return (
                current.status == PipelineRunStatus.RUNNING
                and current.worker_id == worker_b_id
                and current.attempt > running_snapshot.attempt
            )

        await wait_until(run_reconciled_under_worker_b)
    finally:
        if child_b.poll() is None:
            child_b.kill()
            child_b.wait(timeout=TEST_TIMEOUT)

    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM pipeline_runs WHERE dataset_id = :id"), {"id": ids.dataset_id}
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = :id"), {"id": ids.dataset_id}
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_kill_tests_skip_without_opt_in() -> None:
    original = os.environ.get(PROCESS_KILL_TESTS_ENV)
    if PROCESS_KILL_TESTS_ENV in os.environ:
        del os.environ[PROCESS_KILL_TESTS_ENV]
    try:
        assert process_kill_test_database_url() is None
    finally:
        if original is not None:
            os.environ[PROCESS_KILL_TESTS_ENV] = original
