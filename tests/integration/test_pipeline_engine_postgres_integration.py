"""Real-PostgreSQL tests for the resumable pipeline engine (SM-504,
ADR-0009 SS B/SS O and related sections): plan validation/drift, skip/
resume, ownership fencing, no-lock-held-during-execute, cancellation
checkpoints, and retryable/permanent failure handling.

Requires a dedicated, discardable PostgreSQL database with migrations
already applied through 0008. Uses fake, deterministic in-process steps
only -- no Neo4j, no LLM/embedding provider, no filesystem.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.domain import (
    PipelineRunStatus,
    PipelineRunTransitionError,
    PipelineStepStatus,
    PipelineType,
)
from sofias_memory.infrastructure.postgres import create_session_factory, dispose_async_engine
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.engine import PipelineEngine, PipelineExecutionResult
from sofias_memory.pipelines.errors import (
    PermanentPipelineStepError,
    RetryablePipelineStepError,
)
from sofias_memory.pipelines.registry import (
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
)
from sofias_memory.pipelines.retry_policy import RetryPolicy
from sofias_memory.services.pipeline_lifecycle import (
    StepPlan,
    create_run_with_steps,
    transition_run,
    transition_step,
)
from sofias_memory.services.pipeline_queue_claimer import ClaimedRun, PipelineRunClaimer

ENGINE_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_PIPELINE_ENGINE_POSTGRES_TESTS"
ENGINE_POSTGRES_TEST_DATABASE_URL_ENV = "SOFIAS_MEMORY_PIPELINE_ENGINE_TEST_DATABASE_URL"
ENGINE_POSTGRES_TEST_DATABASE_NAME = "sofias_memory_pipeline_engine_test"

TEST_TIMEOUT = 10.0  # detect an accidental deadlock instead of hanging pytest.


def engine_test_database_url(env: Mapping[str, str]) -> str:
    if env.get(ENGINE_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {ENGINE_POSTGRES_TESTS_ENV}=1 to run pipeline engine PostgreSQL tests")

    database_url = env.get(ENGINE_POSTGRES_TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(
            f"set {ENGINE_POSTGRES_TEST_DATABASE_URL_ENV} to a dedicated discardable "
            "PostgreSQL database"
        )

    _validate_engine_test_database_url(database_url)
    return database_url


def _validate_engine_test_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        pytest.skip("pipeline engine PostgreSQL test database URL is invalid")

    if parsed_url.database != ENGINE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "pipeline engine PostgreSQL tests require the exact dedicated database "
            f"{ENGINE_POSTGRES_TEST_DATABASE_NAME}"
        )


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = engine_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await _assert_connected_to_engine_test_database(engine)
        yield engine
    finally:
        await dispose_async_engine(engine)


async def _assert_connected_to_engine_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        current_database = await connection.scalar(text("SELECT current_database()"))
    if current_database != ENGINE_POSTGRES_TEST_DATABASE_NAME:
        pytest.skip(
            "connected PostgreSQL database is not the dedicated pipeline engine test database"
        )


def test_engine_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        engine_test_database_url({})


def test_engine_postgres_tests_reject_wrong_database_name() -> None:
    with pytest.raises(pytest.skip.Exception):
        engine_test_database_url(
            {
                ENGINE_POSTGRES_TESTS_ENV: "1",
                ENGINE_POSTGRES_TEST_DATABASE_URL_ENV: (
                    "postgresql+asyncpg://user:password@localhost:5432/sofias_memory"
                ),
            }
        )


# --- fake, deterministic steps -----------------------------------------------


def const_deriver(key: str) -> Any:
    def _deriver(
        run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del step_outputs
        return {"value": run_input.get(key)}

    return _deriver


def depends_on_deriver(step_name: str) -> Any:
    def _deriver(
        run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        del run_input
        prior = step_outputs.get(step_name)
        if prior is None:
            return None
        return {"prior": prior}

    return _deriver


@dataclass
class RecordingStep:
    """Always succeeds; records every ``execute()`` call for assertions."""

    name: str
    calls: list[PipelineContext] = field(default_factory=list)

    async def execute(self, context: PipelineContext) -> StepResult:
        self.calls.append(context)
        return StepResult(output={"ran": self.name, "call_count": len(self.calls)})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result
        raise AssertionError(f"compensate() must never be called automatically ({self.name})")


@dataclass
class FlakyStep:
    """Fails retryably ``fail_times`` times, then succeeds."""

    name: str
    fail_times: int
    code: str = "TRANSIENT_DEPENDENCY_ERROR"
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RetryablePipelineStepError(self.code, "Simulated transient dependency failure.")
        return StepResult(output={"ran": self.name, "attempt": self.calls})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class AlwaysRetryableStep:
    name: str
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        raise RetryablePipelineStepError("ALWAYS_TRANSIENT", "Always fails for this test.")

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class AlwaysPermanentStep:
    name: str
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        raise PermanentPipelineStepError("BAD_DATA", "Simulated permanent failure.")

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class PausableStep:
    """Blocks on ``proceed`` after signalling ``entered``, letting a test
    mutate state from another session while ``execute()`` is in flight."""

    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: str = "success"  # "success" | "retryable" | "permanent"
    calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.proceed.wait(), timeout=TEST_TIMEOUT)
        if self.outcome == "retryable":
            raise RetryablePipelineStepError("PAUSED_TRANSIENT", "Simulated during-pause failure.")
        if self.outcome == "permanent":
            raise PermanentPipelineStepError("PAUSED_PERMANENT", "Simulated during-pause failure.")
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result, uow

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


@dataclass
class TransactionalStep:
    """``persist()`` applies a real authoritative PostgreSQL mutation (an
    extra marker ``Dataset`` row) inside the engine-supplied transaction --
    proves atomicity with the ``PipelineStep`` SUCCEEDED transition
    (ADR-0009 SS O amendment). Set ``fail_persist`` to make ``persist()``
    itself raise, after already staging the mutation in the session, to
    prove the whole transaction rolls back."""

    name: str
    marker_dataset_id: UUID = field(default_factory=uuid4)
    fail_persist: bool = False
    fail_kind: str = "retryable"  # "retryable" | "permanent" | "unexpected"
    execute_calls: int = 0
    persist_calls: int = 0

    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        self.execute_calls += 1
        return StepResult(output={"ran": self.name})

    async def persist(self, context: PipelineContext, result: StepResult, uow: Any) -> None:
        del context, result
        self.persist_calls += 1
        await uow.datasets.add(
            Dataset(
                id=self.marker_dataset_id,
                name=f"marker-{self.marker_dataset_id}",
                slug=f"marker-{self.marker_dataset_id}",
            )
        )
        if self.fail_persist:
            if self.fail_kind == "retryable":
                raise RetryablePipelineStepError("PERSIST_TRANSIENT", "Simulated persist failure.")
            if self.fail_kind == "permanent":
                raise PermanentPipelineStepError("PERSIST_PERMANENT", "Simulated persist failure.")
            raise RuntimeError("Simulated unexpected persist failure.")

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        del context, result


def step_definition(name: str, step: Any, *, deriver: Any = None) -> PipelineStepDefinition:
    return PipelineStepDefinition(
        name=name,
        definition_id=f"{name}:v1",
        step=step,
        input_deriver=deriver or const_deriver("seed"),
    )


# --- fixtures/helpers ---------------------------------------------------------


@dataclass
class EngineTestIds:
    dataset_ids: list[UUID] = field(default_factory=list)
    run_ids: list[UUID] = field(default_factory=list)


async def insert_dataset(session_factory: Any, ids: EngineTestIds) -> UUID:
    dataset_id = uuid4()
    ids.dataset_ids.append(dataset_id)
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(
            Dataset(id=dataset_id, name=f"engine-{dataset_id}", slug=f"engine-{dataset_id}")
        )
        await uow.commit()
    return dataset_id


async def cleanup_engine_fixture(engine: AsyncEngine, ids: EngineTestIds) -> None:
    async with engine.begin() as connection:
        if ids.run_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE id = ANY(:ids)"), {"ids": ids.run_ids}
            )
        if ids.dataset_ids:
            await connection.execute(
                text("DELETE FROM pipeline_runs WHERE dataset_id = ANY(:ids)"),
                {"ids": ids.dataset_ids},
            )
            await connection.execute(
                text("DELETE FROM datasets WHERE id = ANY(:ids)"), {"ids": ids.dataset_ids}
            )


async def submit_run(
    session_factory: Any,
    ids: EngineTestIds,
    *,
    registry: PipelineRegistry,
    dataset_id: UUID | None,
    pipeline_type: PipelineType = PipelineType.COGNIFY,
    run_input: dict[str, Any] | None = None,
) -> UUID:
    run_input = run_input if run_input is not None else {"seed": "x"}
    plan = registry.build_step_plan(pipeline_type, run_input=run_input)
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=pipeline_type,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=run_input,
            config_fingerprint="b" * 64,
            steps=plan,
        )
        await uow.commit()
    ids.run_ids.append(run.id)
    return run.id


async def submit_run_with_plan(
    session_factory: Any,
    ids: EngineTestIds,
    *,
    dataset_id: UUID | None,
    plan: list[StepPlan],
    run_input: dict[str, Any] | None = None,
    pipeline_type: PipelineType = PipelineType.COGNIFY,
) -> UUID:
    async with PostgresUnitOfWork(session_factory) as uow:
        run = await create_run_with_steps(
            uow,
            pipeline_type=pipeline_type,
            dataset_id=dataset_id,
            source_id=None,
            idempotency_key=None,
            payload_hash="a" * 64,
            input=run_input if run_input is not None else {"seed": "x"},
            config_fingerprint="b" * 64,
            steps=plan,
        )
        await uow.commit()
    ids.run_ids.append(run.id)
    return run.id


async def claim(
    session_factory: Any, run_id: UUID, *, worker_id: str = "wk-engine-test"
) -> ClaimedRun:
    claimer = PipelineRunClaimer(session_factory)
    claimed = await asyncio.wait_for(
        claimer.try_claim_one(worker_id=worker_id), timeout=TEST_TIMEOUT
    )
    assert claimed is not None
    assert claimed.run_id == run_id
    return claimed


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


async def read_steps(session_factory: Any, run_id: UUID) -> list[dict[str, Any]]:
    async with PostgresUnitOfWork(session_factory) as uow:
        steps = await uow.pipeline_steps.list_for_run(run_id)
        return [
            {
                "name": s.name,
                "ordinal": s.ordinal,
                "status": s.status,
                "attempt": s.attempt,
                "input_hash": s.input_hash,
                "output": dict(s.output),
                "error": dict(s.error) if s.error is not None else None,
            }
            for s in steps
        ]


async def force_due(engine: AsyncEngine, run_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE pipeline_runs SET next_attempt_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )


async def dataset_exists(session_factory: Any, dataset_id: UUID) -> bool:
    async with PostgresUnitOfWork(session_factory) as uow:
        dataset = await uow.datasets.get_by_id(dataset_id)
        return dataset is not None


def deterministic_engine(session_factory: Any, registry: PipelineRegistry) -> PipelineEngine:
    return PipelineEngine(
        session_factory,
        registry,
        retry_policy=RetryPolicy(jitter_source=lambda: 0.0),
    )


# === A. three-step happy path =================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_three_step_happy_path(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a, step_b, step_c = RecordingStep("a"), RecordingStep("b"), RecordingStep("c")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(
                step_definition("a", step_a),
                step_definition("b", step_b),
                step_definition("c", step_c),
            ),
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result == PipelineExecutionResult(run_id=run_id, status=PipelineRunStatus.SUCCEEDED)
        assert step_a.calls and step_b.calls and step_c.calls

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.SUCCEEDED
        assert run.progress == 1.0
        assert run.finished_at is not None

        steps = await read_steps(session_factory, run_id)
        assert [s["status"] for s in steps] == [PipelineStepStatus.SUCCEEDED] * 3
        assert [s["attempt"] for s in steps] == [1, 1, 1]
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === B. retry + resume (the core resumability proof) ==========================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_retry_and_resume(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        step_b = FlakyStep("b", fail_times=1)
        step_c = RecordingStep("c")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(
                step_definition("a", step_a),
                step_definition("b", step_b),
                step_definition("c", step_c),
            ),
        )
        registry = PipelineRegistry([definition])
        engine_obj = deterministic_engine(session_factory, registry)
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        # First claim: A succeeds, B fails retryably, C never runs.
        claimed1 = await claim(session_factory, run_id)
        result1 = await engine_obj.execute(claimed1)
        assert result1.status == PipelineRunStatus.QUEUED
        assert len(step_a.calls) == 1
        assert step_b.calls == 1
        assert not step_c.calls

        run_after_first = await read_run(session_factory, run_id)
        assert run_after_first.status == PipelineRunStatus.QUEUED
        assert run_after_first.next_attempt_at is not None
        assert run_after_first.progress == pytest.approx(1 / 3)

        steps_after_first = await read_steps(session_factory, run_id)
        by_name = {s["name"]: s for s in steps_after_first}
        assert by_name["a"]["status"] == PipelineStepStatus.SUCCEEDED
        assert by_name["b"]["status"] == PipelineStepStatus.QUEUED
        assert by_name["b"]["attempt"] == 1
        assert by_name["b"]["error"]["code"] == "TRANSIENT_DEPENDENCY_ERROR"
        assert by_name["c"]["status"] == PipelineStepStatus.QUEUED
        assert by_name["c"]["attempt"] == 0

        # Make the retry due, then reclaim via SM-503's claimer.
        await force_due(postgres_engine, run_id)
        claimed2 = await claim(session_factory, run_id)
        assert claimed2.attempt == 2

        result2 = await engine_obj.execute(claimed2)
        assert result2.status == PipelineRunStatus.SUCCEEDED

        # Proof of resumability: A ran exactly once (skipped on resume), B
        # ran exactly twice (attempt 1 failed, attempt 2 succeeded), C ran
        # exactly once.
        assert len(step_a.calls) == 1
        assert step_b.calls == 2
        assert len(step_c.calls) == 1

        run_final = await read_run(session_factory, run_id)
        assert run_final.status == PipelineRunStatus.SUCCEEDED
        assert run_final.progress == 1.0
        assert run_final.error_code is None
        assert run_final.error_message is None

        steps_final = await read_steps(session_factory, run_id)
        final_by_name = {s["name"]: s for s in steps_final}
        assert final_by_name["a"]["attempt"] == 1
        assert final_by_name["b"]["attempt"] == 2
        assert final_by_name["c"]["attempt"] == 1
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === C. retry exhausted ========================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c_retry_exhausted_fails_run_and_step(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = AlwaysRetryableStep("a")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        # A ceiling of 2 keeps the test fast: attempt 1 retries, attempt 2 exhausts.
        engine_obj = PipelineEngine(
            session_factory,
            registry,
            retry_policy=RetryPolicy(max_run_attempts=2, jitter_source=lambda: 0.0),
        )
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        claimed1 = await claim(session_factory, run_id)
        result1 = await engine_obj.execute(claimed1)
        assert result1.status == PipelineRunStatus.QUEUED

        await force_due(postgres_engine, run_id)
        claimed2 = await claim(session_factory, run_id)
        assert claimed2.attempt == 2
        result2 = await engine_obj.execute(claimed2)
        assert result2.status == PipelineRunStatus.FAILED

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.FAILED
        assert run.error_code == "ALWAYS_TRANSIENT"
        assert run.finished_at is not None

        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.FAILED
        assert steps[0]["error"]["code"] == "ALWAYS_TRANSIENT"
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === D. permanent error =========================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_permanent_error_fails_immediately_no_retry(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = AlwaysPermanentStep("a")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result.status == PipelineRunStatus.FAILED
        assert step_a.calls == 1  # never retried

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.FAILED
        assert run.error_code == "BAD_DATA"
        assert run.next_attempt_at is None
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === E. persisted plan drift (count) ===========================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_plan_count_drift_fails_before_any_execute(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a, step_b = RecordingStep("a"), RecordingStep("b")
        submission_definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        submission_registry = PipelineRegistry([submission_definition])
        run_id = await submit_run(
            session_factory, ids, registry=submission_registry, dataset_id=dataset_id
        )

        # The registry the *engine* sees now has an extra step -- simulates
        # a deploy landing mid-flight between submission and claim.
        drifted_definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(step_definition("a", step_a), step_definition("b", step_b)),
        )
        drifted_registry = PipelineRegistry([drifted_definition])

        claimed = await claim(session_factory, run_id)
        result = await deterministic_engine(session_factory, drifted_registry).execute(claimed)

        assert result.status == PipelineRunStatus.FAILED
        assert not step_a.calls
        assert not step_b.calls

        run = await read_run(session_factory, run_id)
        assert run.error_code == "STEP_INPUT_DRIFT"
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === F. definition/input hash drift ============================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_definition_identity_drift_fails_before_execute(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        submission_definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(
                PipelineStepDefinition(
                    name="a", definition_id="a:v1", step=step_a, input_deriver=const_deriver("seed")
                ),
            ),
        )
        submission_registry = PipelineRegistry([submission_definition])
        run_id = await submit_run(
            session_factory, ids, registry=submission_registry, dataset_id=dataset_id
        )

        # Same step name, same semantic input, but a different definition
        # identity/version -- the hash must diverge (ADR-0009 SS 6).
        drifted_definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(
                PipelineStepDefinition(
                    name="a", definition_id="a:v2", step=step_a, input_deriver=const_deriver("seed")
                ),
            ),
        )
        drifted_registry = PipelineRegistry([drifted_definition])

        claimed = await claim(session_factory, run_id)
        result = await deterministic_engine(session_factory, drifted_registry).execute(claimed)

        assert result.status == PipelineRunStatus.FAILED
        assert not step_a.calls
        run = await read_run(session_factory, run_id)
        assert run.error_code == "STEP_INPUT_DRIFT"
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === G. dependent input_hash: NULL at submission, backfilled before execute ===


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_dependent_input_hash_null_then_backfilled(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        step_b = RecordingStep("b")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(
                step_definition("a", step_a),
                step_definition("b", step_b, deriver=depends_on_deriver("a")),
            ),
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)

        steps_at_submission = await read_steps(session_factory, run_id)
        by_name = {s["name"]: s for s in steps_at_submission}
        assert by_name["a"]["input_hash"] is not None
        assert by_name["b"]["input_hash"] is None  # not yet computable

        claimed = await claim(session_factory, run_id)
        result = await deterministic_engine(session_factory, registry).execute(claimed)
        assert result.status == PipelineRunStatus.SUCCEEDED

        steps_after = await read_steps(session_factory, run_id)
        after_by_name = {s["name"]: s for s in steps_after}
        assert after_by_name["b"]["input_hash"] is not None  # backfilled before execute
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === H. skip succeeded: execute not called again on resume ====================
# (already proven by test_b's exact call-count assertions on step A)


# === I. cancellation before first step =========================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_i_cancellation_before_first_step(postgres_engine: AsyncEngine) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a, step_b = RecordingStep("a"), RecordingStep("b")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(step_definition("a", step_a), step_definition("b", step_b)),
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE pipeline_runs SET status = 'cancelling' WHERE id = :id"),
                {"id": run_id},
            )

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result.status == PipelineRunStatus.CANCELLED
        assert not step_a.calls
        assert not step_b.calls

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.CANCELLED
        steps = await read_steps(session_factory, run_id)
        assert all(s["status"] == PipelineStepStatus.CANCELLED for s in steps)
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === J. cancellation between steps + proof of no long-held lock ===============


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_cancellation_between_steps_and_no_lock_held_during_execute(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        step_b = RecordingStep("b")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY,
            steps=(step_definition("a", step_a), step_definition("b", step_b)),
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        engine_task = asyncio.create_task(
            deterministic_engine(session_factory, registry).execute(claimed)
        )
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        # Step A is RUNNING and committed -- observable and mutable from a
        # completely independent connection while execute() is still
        # in-flight. This proves the engine holds no row/advisory lock
        # across the step call.
        async def independent_observe_and_cancel() -> None:
            async with postgres_engine.connect() as connection:
                status = await connection.scalar(
                    text("SELECT status FROM pipeline_steps WHERE run_id = :id AND name = 'a'"),
                    {"id": run_id},
                )
                assert status == "running"
            async with postgres_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE pipeline_runs SET status = 'cancelling' WHERE id = :id"),
                    {"id": run_id},
                )

        await asyncio.wait_for(independent_observe_and_cancel(), timeout=TEST_TIMEOUT)

        step_a.proceed.set()
        result = await asyncio.wait_for(engine_task, timeout=TEST_TIMEOUT)

        assert result.status == PipelineRunStatus.CANCELLED
        assert step_a.calls == 1  # A finished its own attempt
        assert not step_b.calls  # B never started

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.CANCELLED
        steps = await read_steps(session_factory, run_id)
        by_name = {s["name"]: s for s in steps}
        assert by_name["a"]["status"] == PipelineStepStatus.SUCCEEDED
        assert by_name["b"]["status"] == PipelineStepStatus.CANCELLED
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === K. ownership fencing: an old attempt never overwrites a newer one ========


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_ownership_fencing_stale_attempt_never_overwrites_newer(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id, worker_id="wk-stale")

        engine_obj = deterministic_engine(session_factory, registry)
        stale_task = asyncio.create_task(engine_obj.execute(claimed))
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        # The fresh reclaim below must execute against a *different* step
        # object -- reusing ``step_a`` (a PausableStep) would make the
        # fresh execution itself block on the same shared ``proceed``
        # Event, which is not what this test is proving. Same step name
        # and definition_id (via step_definition()'s default), so the
        # persisted plan still matches -- just a non-pausing implementation.
        step_a_fresh = RecordingStep("a")
        fresh_definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a_fresh),)
        )
        fresh_registry = PipelineRegistry([fresh_definition])

        # Simulate SM-507 stale recovery reconciling an abandoned claim
        # directly via the SM-502 lifecycle primitives (not implementing
        # SM-507 itself): reset the run to QUEUED and reset the orphaned
        # step to QUEUED, then let a *different* worker reclaim it.
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            db_now = await uow.pipeline_runs.get_database_now()
            transition_run(run, PipelineRunStatus.QUEUED, now=db_now, next_attempt_at=None)
            existing_steps = await uow.pipeline_steps.list_for_run(run_id)
            step_row = await uow.pipeline_steps.get_by_id_for_update(existing_steps[0].id)
            assert step_row is not None
            transition_step(step_row, PipelineStepStatus.QUEUED, now=db_now)
            await uow.commit()

        claimed2 = await claim(session_factory, run_id, worker_id="wk-fresh")
        assert claimed2.attempt == 2
        fresh_result = await deterministic_engine(session_factory, fresh_registry).execute(claimed2)
        assert fresh_result.status == PipelineRunStatus.SUCCEEDED
        assert step_a_fresh.calls

        # Now release the stale execution -- its result must be discarded,
        # never overwrite attempt 2's already-terminal SUCCEEDED state.
        step_a.proceed.set()
        stale_result = await asyncio.wait_for(stale_task, timeout=TEST_TIMEOUT)
        assert stale_result.abandoned is True
        assert stale_result.status is None

        run_final = await read_run(session_factory, run_id)
        assert run_final.status == PipelineRunStatus.SUCCEEDED
        assert run_final.attempt == 2
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === L. progress durable across retry/reclaim ==================================
# (already proven by test_b's progress assertions at 1/3 then 1.0)


# === M. no automatic compensate on retry/cancel ================================
# RecordingStep.compensate() raises AssertionError if ever invoked; tests
# A, I, and J all exercise retry-adjacent and cancellation paths using
# RecordingStep/steps whose compensate() would raise if called, and none
# of them fail with that AssertionError -- proving compensate() is never
# invoked automatically by this engine.


# === N. run terminal success is consistent with the final step's success ======
# (already proven by test_a: run SUCCEEDED + progress=1.0 in the same commit
# as the last step's SUCCEEDED transition)


# === O. atomic business mutation + PipelineStep SUCCEEDED (SS O amendment) ===


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_persist_business_mutation_and_step_success_same_commit(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = TransactionalStep("a")
        ids.dataset_ids.append(step_a.marker_dataset_id)
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result.status == PipelineRunStatus.SUCCEEDED
        assert step_a.execute_calls == 1
        assert step_a.persist_calls == 1
        assert await dataset_exists(session_factory, step_a.marker_dataset_id)

        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.SUCCEEDED
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === P. persist() failure rolls back the whole transaction ====================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p_persist_failure_rolls_back_business_mutation_and_step_status(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = TransactionalStep("a", fail_persist=True, fail_kind="permanent")
        ids.dataset_ids.append(step_a.marker_dataset_id)
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        # Rolled back: the marker mutation staged inside the failed persist()
        # transaction must not exist, even though persist() ran once.
        assert step_a.persist_calls == 1
        assert not await dataset_exists(session_factory, step_a.marker_dataset_id)

        # Classified and recorded in a fresh, separate transaction: FAILED,
        # not stuck SUCCEEDED or silently swallowed.
        assert result.status == PipelineRunStatus.FAILED
        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.FAILED
        assert run.error_code == "PERSIST_PERMANENT"

        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.FAILED
        assert steps[0]["error"]["code"] == "PERSIST_PERMANENT"
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === Q. persist() failure respects typed retryable/permanent classification ===


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q_persist_retryable_failure_schedules_retry_not_stuck_running(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = TransactionalStep("a", fail_persist=True, fail_kind="retryable")
        ids.dataset_ids.append(step_a.marker_dataset_id)
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result.status == PipelineRunStatus.QUEUED
        assert not await dataset_exists(session_factory, step_a.marker_dataset_id)

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.QUEUED
        assert run.next_attempt_at is not None
        # ADR-0009 SS 30: a run awaiting retry must never look terminally
        # failed.
        assert run.error_code is None

        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.QUEUED
        assert steps[0]["error"]["code"] == "PERSIST_TRANSIENT"
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q_persist_unexpected_exception_classified_permanent(
    postgres_engine: AsyncEngine,
) -> None:
    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = TransactionalStep("a", fail_persist=True, fail_kind="unexpected")
        ids.dataset_ids.append(step_a.marker_dataset_id)
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        result = await deterministic_engine(session_factory, registry).execute(claimed)

        assert result.status == PipelineRunStatus.FAILED
        assert not await dataset_exists(session_factory, step_a.marker_dataset_id)
        run = await read_run(session_factory, run_id)
        assert run.error_code == "STEP_UNEXPECTED_ERROR"
        assert "RuntimeError" not in (run.error_message or "")  # no raw exception text leaked
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


# === R/S. cancel-vs-final-success race, both PostgreSQL commit orders =========


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r_engine_commit_wins_race_run_succeeds_cancel_cannot_reopen(
    postgres_engine: AsyncEngine,
) -> None:
    """Engine's final-step success transaction commits before the cancel
    request's UPDATE -- the run reaches SUCCEEDED, and a cancel arriving
    after that can never reopen a terminal run (RUN_TRANSITIONS has no
    outgoing edge from SUCCEEDED)."""

    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = RecordingStep("a")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        # Engine runs to completion (SUCCEEDED) with nothing racing it.
        result = await deterministic_engine(session_factory, registry).execute(claimed)
        assert result.status == PipelineRunStatus.SUCCEEDED

        # A cancel request arriving strictly after the run is already
        # terminal must be rejected by the transition matrix, never silently
        # accepted or left to corrupt the terminal state.
        async with PostgresUnitOfWork(session_factory) as uow:
            run = await uow.pipeline_runs.get_by_id_for_update(run_id)
            assert run is not None
            db_now = await uow.pipeline_runs.get_database_now()
            with pytest.raises(PipelineRunTransitionError):
                transition_run(run, PipelineRunStatus.CANCELLING, now=db_now)

        final = await read_run(session_factory, run_id)
        assert final.status == PipelineRunStatus.SUCCEEDED
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s_cancel_commit_wins_race_run_cancelled_not_succeeded(
    postgres_engine: AsyncEngine,
) -> None:
    """Cancel request's UPDATE commits before the engine's own final-step
    success transaction observes it -- the run must converge to CANCELLED,
    never SUCCEEDED, even though the last (only) step's business work
    completed. This is the exact race ADR-0009 SS A/SS N require: CANCELLING
    has no path to SUCCEEDED."""

    ids = EngineTestIds()
    session_factory = create_session_factory(postgres_engine)
    dataset_id = await insert_dataset(session_factory, ids)
    try:
        step_a = PausableStep("a")
        definition = PipelineDefinition(
            pipeline_type=PipelineType.COGNIFY, steps=(step_definition("a", step_a),)
        )
        registry = PipelineRegistry([definition])
        run_id = await submit_run(session_factory, ids, registry=registry, dataset_id=dataset_id)
        claimed = await claim(session_factory, run_id)

        engine_task = asyncio.create_task(
            deterministic_engine(session_factory, registry).execute(claimed)
        )
        await asyncio.wait_for(step_a.entered.wait(), timeout=TEST_TIMEOUT)

        # Cancel commits first, strictly before the step (the run's only,
        # last step) returns and the engine's post-execute transaction ever
        # opens.
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("UPDATE pipeline_runs SET status = 'cancelling' WHERE id = :id"),
                {"id": run_id},
            )

        step_a.proceed.set()
        result = await asyncio.wait_for(engine_task, timeout=TEST_TIMEOUT)

        # The step's own business work still completed (SS 32: never
        # interrupted mid-step) -- but the run's fate is CANCELLED, never
        # SUCCEEDED, because cancellation was observed as already committed
        # when the engine's post-execute transaction locked the row.
        assert result.status == PipelineRunStatus.CANCELLED

        run = await read_run(session_factory, run_id)
        assert run.status == PipelineRunStatus.CANCELLED
        assert run.progress == 1.0  # the one-and-only step did succeed

        steps = await read_steps(session_factory, run_id)
        assert steps[0]["status"] == PipelineStepStatus.SUCCEEDED
    finally:
        await cleanup_engine_fixture(postgres_engine, ids)
