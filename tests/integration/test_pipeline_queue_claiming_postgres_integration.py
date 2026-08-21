"""Real-PostgreSQL tests for ADR-0009 SS D queue claiming (SM-503):
FOR UPDATE SKIP LOCKED row identity, transaction-level advisory-lock
arbitration, persisted conflict revalidation, and global-barrier fairness.

Does not exercise worker polling, pipeline execution, or Runs API -- those
are later stories. Requires a dedicated, discardable PostgreSQL database with
migrations already applied through 0008.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.advisory_lock_keys import dataset_lock_key
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.services.pipeline_lifecycle import transition_run
from sofias_memory.services.pipeline_queue_claimer import (
    DEFAULT_CANDIDATE_SCAN_LIMIT,
    PipelineRunClaimer,
)

CLAIMING_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_PIPELINE_CLAIMING_POSTGRES_TESTS"
CLAIMING_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_PIPELINE_CLAIMING_TEST_DATABASE_URL"
CLAIMING_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_pipeline_claiming_test"

CLAIM_TIMEOUT = 10.0  # detect an accidental deadlock instead of hanging pytest.

# Anchored to real wall-clock time (minus a safety margin), not a fixed
# historical constant: eligibility predicates compare against PostgreSQL's
# own now(), so a "future" next_attempt_at must genuinely be after the real
# now() at test-run time, and a "due" one genuinely before it.
BASE_TIME = datetime.now(UTC) - timedelta(hours=1)


def _at(offset_seconds: float) -> datetime:
    return BASE_TIME + timedelta(seconds=offset_seconds)


def _future(seconds: float = 3600.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _past(seconds: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def claiming_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(CLAIMING_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(
            f"set {CLAIMING_POSTGRES_TESTS_ENV}=1 to run pipeline claiming PostgreSQL tests"
        )

    database_url = env.get(CLAIMING_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {CLAIMING_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_claiming_test_database_url(database_url)
    return database_url


def _validate_claiming_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("pipeline claiming PostgreSQL test database URL is invalid")

    if parsed_url.database != CLAIMING_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "pipeline claiming PostgreSQL tests require the exact dedicated database "
            f"{CLAIMING_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = claiming_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_claiming_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_claiming_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != CLAIMING_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated pipeline claiming test database"
        )


def test_claiming_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        claiming_test_database_url({})


def test_claiming_postgres_tests_skip_without_dedicated_url() -> None:
    with pytest.raises(pytest.skip.Exception):
        claiming_test_database_url({CLAIMING_POSTGRES_TESTS_ENV: "1"})


def test_claiming_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        claiming_test_database_url(
            {
                CLAIMING_POSTGRES_TESTS_ENV: "1",
                CLAIMING_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fixtures ----------------------------------------------------------------


@dataclass
class ClaimingIds:
    dataset_ids: list[UUID] = field(default_factory=list)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_dataset(engine: AsyncEngine, ids: ClaimingIds) -> UUID:
    dataset_id = uuid4()
    ids.dataset_ids.append(dataset_id)
    session_factory = create_session_factory(engine)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(id=dataset_id, name=f"claim-{dataset_id}", slug=f"claim-{dataset_id}")
        )
        await uow.commit()
    return dataset_id


async def cleanup_claiming_fixture(engine: AsyncEngine, ids: ClaimingIds) -> None:
    async with engine.begin() as connection:
        if ids.dataset_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE dataset_id = ANY(:ids)"),
                {"ids": ids.dataset_ids},
            )
        if ids.run_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"),
                {"ids": ids.run_ids},
            )
        if ids.dataset_ids:
            await connection.execute(
                text("DELETE FROM datasets WHERE id = ANY(:ids)"),
                {"ids": ids.dataset_ids},
            )


async def insert_run(
    session_factory,
    ids: ClaimingIds,
    *,
    dataset_id: UUID | None,
    status: PipelineRunStatus = PipelineRunStatus.QUEUED,
    created_at: datetime,
    next_attempt_at: datetime | None = None,
    run_id: UUID | None = None,
    payload_seed: str = "a",
) -> UUID:
    run_id = run_id or uuid4()
    ids.run_ids.append(run_id)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = PipelineRun(
            id=run_id,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=dataset_id,
            source_id=None,
            status=status,
            idempotency_key=None,
            payload_hash=(payload_seed * 64)[:64],
            input={},
            progress=0.0,
            current_step=None,
            attempt=0,
            worker_id=None,
            heartbeat_at=None,
            config_fingerprint="c" * 64,
            error_code=None,
            error_message=None,
            metrics={},
            created_at=created_at,
            started_at=None,
            finished_at=None,
            next_attempt_at=next_attempt_at,
            retry_of_run_id=None,
        )
        await uow.pipeline_runs.add(run)
        await uow.commit()
    return run_id


async def read_run(session_factory, run_id: UUID) -> PipelineRun:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id(run_id)
        assert run is not None
        # detach a plain snapshot so callers can use attributes after the
        # session that loaded them closes.
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


async def terminalize(session_factory, run_id: UUID) -> None:
    """Test-only fixture helper: force a run straight to SUCCEEDED so a later
    scenario's dataset/global slot frees up. Never used to fake pipeline
    execution semantics -- only to set up subsequent claim scenarios."""

    async with PostgresUnitOfWork(session_factory) as uow:
        run = await uow.pipeline_runs.get_by_id_for_update(run_id)
        assert run is not None
        transition_run(run, PipelineRunStatus.SUCCEEDED, now=datetime.now(UTC))
        await uow.commit()


async def claim_once(claimer: PipelineRunClaimer, *, worker_id: str = "wk-test"):
    return await asyncio.wait_for(claimer.try_claim_one(worker_id=worker_id), timeout=CLAIM_TIMEOUT)


# --- A. same row double claim -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_same_row_double_claim(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_id = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run_id = await insert_run(session_factory, ids, dataset_id=dataset_id, created_at=_at(0))
        claimer = PipelineRunClaimer(session_factory)

        results = await asyncio.wait_for(
            asyncio.gather(
                claim_once(claimer, worker_id="wk-1"),
                claim_once(claimer, worker_id="wk-2"),
            ),
            timeout=CLAIM_TIMEOUT,
        )

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1
        assert non_none[0].run_id == run_id

        final = await read_run(session_factory, run_id)
        assert final.status == PipelineRunStatus.RUNNING
        assert final.attempt == 1
        assert final.worker_id in ("wk-1", "wk-2")
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- B. same dataset, different rows -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_same_dataset_different_rows(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_id = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run1 = await insert_run(session_factory, ids, dataset_id=dataset_id, created_at=_at(0))
        run2 = await insert_run(session_factory, ids, dataset_id=dataset_id, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        results = await asyncio.wait_for(
            asyncio.gather(
                claim_once(claimer, worker_id="wk-1"), claim_once(claimer, worker_id="wk-2")
            ),
            timeout=CLAIM_TIMEOUT,
        )
        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1

        statuses = {
            run1: (await read_run(session_factory, run1)).status,
            run2: (await read_run(session_factory, run2)).status,
        }
        running = [rid for rid, status in statuses.items() if status == PipelineRunStatus.RUNNING]
        queued = [rid for rid, status in statuses.items() if status == PipelineRunStatus.QUEUED]
        assert len(running) == 1
        assert len(queued) == 1
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- C. different datasets -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c_different_datasets_both_claimable_concurrently(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run_a = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(0))
        run_b = await insert_run(session_factory, ids, dataset_id=dataset_b, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        results = await asyncio.wait_for(
            asyncio.gather(
                claim_once(claimer, worker_id="wk-1"), claim_once(claimer, worker_id="wk-2")
            ),
            timeout=CLAIM_TIMEOUT,
        )
        claimed_ids = {r.run_id for r in results if r is not None}
        assert claimed_ids == {run_a, run_b}

        assert (await read_run(session_factory, run_a)).status == PipelineRunStatus.RUNNING
        assert (await read_run(session_factory, run_b)).status == PipelineRunStatus.RUNNING
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- D. existing running dataset: scan skips to a free dataset ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_scan_skips_conflicted_dataset_and_finds_free_one(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(0),
        )
        a2 = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        b1 = await insert_run(session_factory, ids, dataset_id=dataset_b, created_at=_at(2))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == b1

        assert (await read_run(session_factory, a2)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- E. cancelling dataset also blocks ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_cancelling_dataset_blocks_claim(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            status=PipelineRunStatus.CANCELLING,
            created_at=_at(0),
        )
        a2 = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is None
        assert (await read_run(session_factory, a2)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- F. global vs dataset race: never both RUNNING in conflict --------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_global_vs_dataset_race_never_both_running(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        g = await insert_run(session_factory, ids, dataset_id=None, created_at=_at(0))
        a = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        await asyncio.wait_for(
            asyncio.gather(
                claim_once(claimer, worker_id="wk-1"), claim_once(claimer, worker_id="wk-2")
            ),
            timeout=CLAIM_TIMEOUT,
        )

        g_status = (await read_run(session_factory, g)).status
        a_status = (await read_run(session_factory, a)).status
        running_count = sum(status == PipelineRunStatus.RUNNING for status in (g_status, a_status))
        assert running_count == 1  # exactly one, never both
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- G. global blocked while a dataset is running -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_global_blocked_while_dataset_running(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(0),
        )
        g = await insert_run(session_factory, ids, dataset_id=None, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is None
        assert (await read_run(session_factory, g)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- H. global running blocks dataset -----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_h_global_running_blocks_dataset(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=None,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(0),
        )
        a = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is None
        assert (await read_run(session_factory, a)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- I. global cancelling blocks dataset --------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_i_global_cancelling_blocks_dataset(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=None,
            status=PipelineRunStatus.CANCELLING,
            created_at=_at(0),
        )
        a = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is None
        assert (await read_run(session_factory, a)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- J. fairness --------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_fairness_starvation_guard(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        a1 = await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(0),
        )
        g = await insert_run(session_factory, ids, dataset_id=None, created_at=_at(1))
        # B uses its own dataset (not dataset_a) so the test isolates
        # fairness-vs-global from plain same-dataset exclusion with A1.
        dataset_b = await insert_dataset(postgres_engine, ids)
        b = await insert_run(session_factory, ids, dataset_id=dataset_b, created_at=_at(2))
        claimer = PipelineRunClaimer(session_factory)

        # While A1 is RUNNING, neither G (blocked by dataset conflict) nor B
        # (blocked by fairness relative to G) can be claimed.
        result = await claim_once(claimer)
        assert result is None
        assert (await read_run(session_factory, g)).status == PipelineRunStatus.QUEUED
        assert (await read_run(session_factory, b)).status == PipelineRunStatus.QUEUED

        # A1 terminalizes: G becomes claimable.
        await terminalize(session_factory, a1)
        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == g
        assert (
            await read_run(session_factory, b)
        ).status == PipelineRunStatus.QUEUED  # still blocked

        # Only after G itself terminalizes does B become claimable.
        await terminalize(session_factory, g)
        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == b
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- K. older dataset precedence ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_older_dataset_run_precedes_younger_global(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        d = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(0))
        await insert_run(session_factory, ids, dataset_id=None, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == d  # older dataset candidate, unaffected by younger global
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- L. global backoff ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_l_global_backoff_does_not_block_then_blocks_once_due(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        g = await insert_run(
            session_factory,
            ids,
            dataset_id=None,
            created_at=_at(0),
            next_attempt_at=_future(),  # not eligible yet: real now() is well before this
        )
        d = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        claimer = PipelineRunClaimer(session_factory)

        # G is not eligible (backoff): D can be claimed freely.
        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == d
        await terminalize(session_factory, d)  # free dataset_a so it never confuses later checks

        # G becomes due.
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE pipeline_runs SET next_attempt_at = :past WHERE id = :id"),
                {"past": _past(), "id": g},
            )

        e = await insert_run(session_factory, ids, dataset_id=dataset_b, created_at=_at(2))

        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == g  # oldest eligible candidate now

        result = await claim_once(claimer)
        assert result is None  # E still cannot overtake the now-RUNNING G
        assert (await read_run(session_factory, e)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- M. retry eligibility -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_retry_eligibility_future_ignored_due_claimed(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        not_due = await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            created_at=_at(0),
            next_attempt_at=_future(),
        )
        due = await insert_run(
            session_factory, ids, dataset_id=dataset_b, created_at=_at(1), next_attempt_at=_past()
        )
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == due

        claimed = await read_run(session_factory, due)
        assert claimed.attempt == 1
        assert claimed.next_attempt_at is None

        assert (await read_run(session_factory, not_due)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- N. commit visibility -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_commit_visibility_from_a_different_session(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run_id = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(0))
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer, worker_id="wk-visible")
        assert result is not None

        # Fresh, independent connection/session reading committed state.
        async with postgres_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, worker_id, attempt, heartbeat_at "
                        "FROM pipeline_runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
            ).one()
        assert row.status == "running"
        assert row.worker_id == "wk-visible"
        assert row.attempt == 1
        assert row.heartbeat_at is not None
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- O. lock release -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_advisory_lock_released_after_commit_but_persisted_state_still_blocks(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run1 = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(0))
        claimer = PipelineRunClaimer(session_factory)
        result = await claim_once(claimer)
        assert result is not None
        assert result.run_id == run1

        # The advisory lock itself is free again post-commit.
        async with postgres_engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": dataset_lock_key(dataset_a)},
            )
            assert acquired is True
            await connection.rollback()  # release

        # But a second queued run for the same dataset is still blocked --
        # by persisted RUNNING state, not by the (now free) advisory lock.
        run2 = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1))
        result2 = await claim_once(claimer)
        assert result2 is None
        assert (await read_run(session_factory, run2)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- P. advisory try is non-blocking ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p_advisory_try_is_non_blocking(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_locked = await insert_dataset(postgres_engine, ids)
    dataset_free = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        locked_run = await insert_run(
            session_factory, ids, dataset_id=dataset_locked, created_at=_at(0)
        )
        free_run = await insert_run(
            session_factory, ids, dataset_id=dataset_free, created_at=_at(1)
        )
        claimer = PipelineRunClaimer(session_factory)

        holder = await postgres_engine.connect()
        try:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": dataset_lock_key(dataset_locked)},
            )  # blocking acquire in the *setup* connection; held open, not committed

            result = await claim_once(claimer)  # must not hang despite the held lock
            assert result is not None
            assert result.run_id == free_run  # locked-dataset candidate was skipped

            assert (await read_run(session_factory, locked_run)).status == PipelineRunStatus.QUEUED
        finally:
            await holder.rollback()
            await holder.close()
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- Q. deterministic tie --------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q_deterministic_tie_break_by_id_in_candidate_listing(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        tied_at = _at(5)
        low_id = UUID("00000000-0000-0000-0000-000000000001")
        high_id = UUID("ffffffff-ffff-ffff-ffff-fffffffffffe")
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            created_at=tied_at,
            run_id=high_id,
            payload_seed="1",
        )
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_b,
            created_at=tied_at,
            run_id=low_id,
            payload_seed="2",
        )

        async with PostgresUnitOfWork(session_factory) as uow:
            candidate_ids = await uow.pipeline_runs.list_eligible_candidate_ids(limit=10)
        assert candidate_ids.index(low_id) < candidate_ids.index(high_id)

        # Fairness tie-break: a global with a smaller id at the same
        # created_at precedes; a global with a larger id does not.
        async with PostgresUnitOfWork(session_factory) as uow:
            blocks = await uow.pipeline_runs.exists_eligible_global_with_precedence(
                before_created_at=tied_at, before_id=high_id
            )
        assert blocks is False  # no *global* candidate exists yet

        global_low = UUID("00000000-0000-0000-0000-000000000002")
        await insert_run(
            session_factory,
            ids,
            dataset_id=None,
            created_at=tied_at,
            run_id=global_low,
            payload_seed="3",
        )
        async with PostgresUnitOfWork(session_factory) as uow:
            blocks_higher = await uow.pipeline_runs.exists_eligible_global_with_precedence(
                before_created_at=tied_at, before_id=high_id
            )
            blocks_lower = await uow.pipeline_runs.exists_eligible_global_with_precedence(
                before_created_at=tied_at, before_id=UUID("00000000-0000-0000-0000-000000000000")
            )
        assert blocks_higher is True  # global_low < high_id at the same timestamp
        assert blocks_lower is False  # nothing precedes the smallest possible id at this timestamp
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- R. congested dataset does not starve an unrelated free dataset ---------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r_congested_dataset_does_not_head_of_line_block_free_dataset(
    postgres_engine: AsyncEngine,
) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    dataset_b = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_a,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(0),
        )

        # Strictly more queued runs for dataset A than the discovery scan
        # limit -- every one of them would fail the persisted same-dataset
        # conflict check, but only the oldest should ever need to be looked
        # at for B1 to still be discoverable.
        congestion_count = DEFAULT_CANDIDATE_SCAN_LIMIT + 5
        a_queued_ids = [
            await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(1 + i))
            for i in range(congestion_count)
        ]

        b1 = await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_b,
            created_at=_at(1 + congestion_count + 1),  # created after all of A's congestion
        )
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)

        assert result is not None
        assert result.run_id == b1
        for a_id in a_queued_ids:
            assert (await read_run(session_factory, a_id)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- T. more congested distinct scopes than the scan limit does not starve --
# --- a free scope -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_t_more_congested_scopes_than_scan_limit_does_not_starve_free_dataset(
    postgres_engine: AsyncEngine,
) -> None:
    """Distinct from R: R proves many queued runs of the *same* dataset don't
    starve another dataset. T proves that even one committed-conflicted
    representative *per* dataset, across more distinct datasets than the scan
    limit, doesn't starve a free dataset either -- every one of those
    representatives is certain to fail revalidation (its dataset is already
    RUNNING), so a flat top-N-by-created_at scan could fill the whole window
    with them and never reach the free dataset's candidate."""

    ids = ClaimingIds()
    session_factory = create_session_factory(postgres_engine)

    blocked_queued_ids: list[UUID] = []
    for i in range(DEFAULT_CANDIDATE_SCAN_LIMIT):
        dataset_id = await insert_dataset(postgres_engine, ids)
        await insert_run(
            session_factory,
            ids,
            dataset_id=dataset_id,
            status=PipelineRunStatus.RUNNING,
            created_at=_at(2 * i),
        )
        blocked_queued_ids.append(
            await insert_run(
                session_factory,
                ids,
                dataset_id=dataset_id,
                created_at=_at(2 * i + 1),
            )
        )

    dataset_u = await insert_dataset(postgres_engine, ids)
    u1 = await insert_run(
        session_factory,
        ids,
        dataset_id=dataset_u,
        created_at=_at(2 * DEFAULT_CANDIDATE_SCAN_LIMIT + 1),  # created after all blocked scopes
    )

    try:
        claimer = PipelineRunClaimer(session_factory)

        result = await claim_once(claimer)

        assert result is not None
        assert result.run_id == u1
        for blocked_id in blocked_queued_ids:
            assert (await read_run(session_factory, blocked_id)).status == PipelineRunStatus.QUEUED
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)


# --- S. PostgreSQL is the claim's time authority -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s_claim_timestamps_derive_from_postgresql_now(postgres_engine: AsyncEngine) -> None:
    ids = ClaimingIds()
    dataset_a = await insert_dataset(postgres_engine, ids)
    try:
        session_factory = create_session_factory(postgres_engine)
        run_id = await insert_run(session_factory, ids, dataset_id=dataset_a, created_at=_at(0))
        claimer = PipelineRunClaimer(session_factory)

        async with postgres_engine.connect() as connection:
            db_before = await connection.scalar(text("SELECT now()"))

        result = await claim_once(claimer)
        assert result is not None

        async with postgres_engine.connect() as connection:
            db_after = await connection.scalar(text("SELECT now()"))

        claimed = await read_run(session_factory, run_id)
        assert claimed.started_at is not None
        assert claimed.heartbeat_at is not None
        assert db_before <= claimed.started_at <= db_after
        assert db_before <= claimed.heartbeat_at <= db_after
        # Same transition_run(RUNNING) call sets both from the one db_now
        # value it was given -- they must be exactly equal, not just close.
        assert claimed.started_at == claimed.heartbeat_at
    finally:
        await cleanup_claiming_fixture(postgres_engine, ids)
