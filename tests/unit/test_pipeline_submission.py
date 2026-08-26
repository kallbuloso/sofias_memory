"""Unit coverage for the SM-509 shared submission service.

Uses an in-memory fake ``SubmissionUnitOfWork`` (same fake-vs-real split as
``DatasetService``/``RunService``) so the full submission algorithm --
idempotency resolution, the IntegrityError race recovery path, and the
worker-availability gate -- is exercised without a real PostgreSQL
connection. True concurrent-transaction races are proven separately in
``tests/integration/test_pipeline_submission_postgres_integration.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.pipelines.hashing import canonical_work_payload_hash
from sofias_memory.pipelines.registry import (
    PipelineDefinition,
    PipelineRegistry,
    PipelineStepDefinition,
    StepResult,
    UnknownPipelineTypeError,
)
from sofias_memory.services.pipeline_submission import (
    IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT,
    PipelineSubmissionService,
    SubmissionTargets,
    SubmissionUnitOfWork,
    UnitOfWorkFactory,
)

CONFIG_FINGERPRINT = "f" * 64


@dataclass
class NoOpStep:
    """Never actually invoked by submission -- only ``build_step_plan``
    (pure code, no execution) is exercised here."""

    async def execute(self, context: Any) -> StepResult:
        raise AssertionError("submission must never execute a step")

    async def persist(self, context: Any, result: StepResult, uow: Any) -> None:
        raise AssertionError("submission must never persist a step")

    async def compensate(self, context: Any, result: StepResult) -> None:
        raise AssertionError("submission must never compensate a step")


def const_deriver(run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]) -> Any:
    del step_outputs
    return {"seed": run_input.get("seed")}


def unresolvable_deriver(run_input: Mapping[str, Any], step_outputs: Mapping[str, Any]) -> None:
    del run_input, step_outputs
    return None


def make_test_registry(pipeline_type: PipelineType = PipelineType.REMEMBER) -> PipelineRegistry:
    """Minimal 2-step registered ``PipelineDefinition`` for SM-509 tests
    (Part Y): production registry stays empty (SM-510..513)."""

    steps = (
        PipelineStepDefinition(
            name="step_a",
            definition_id="step_a:v1",
            step=NoOpStep(),
            input_deriver=const_deriver,
        ),
        PipelineStepDefinition(
            name="step_b",
            definition_id="step_b:v1",
            step=NoOpStep(),
            input_deriver=unresolvable_deriver,
        ),
    )
    return PipelineRegistry([PipelineDefinition(pipeline_type=pipeline_type, steps=steps)])


@dataclass
class FakeWorker:
    enabled: bool = True
    is_running: bool = True


class FakeStore:
    def __init__(self) -> None:
        self.runs: list[PipelineRun] = []
        self.steps: list[PipelineStep] = []
        self.commits = 0
        self.fail_next_add: Exception | None = None
        self.prepare_calls = 0

    def add_run(self, run: PipelineRun) -> None:
        self.runs.append(run)


class FakePipelineRunRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return next((run for run in self._store.runs if run.id == run_id), None)

    async def get_by_idempotency_key(self, idempotency_key: str) -> PipelineRun | None:
        return next(
            (run for run in self._store.runs if run.idempotency_key == idempotency_key), None
        )

    async def add(self, run: PipelineRun) -> PipelineRun:
        if self._store.fail_next_add is not None:
            error = self._store.fail_next_add
            self._store.fail_next_add = None
            raise error
        self._store.runs.append(run)
        return run


class FakePipelineStepRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add_many(self, steps: list[PipelineStep]) -> list[PipelineStep]:
        self._store.steps.extend(steps)
        return steps


class FakeDatasetRepository:
    """Always "no Dataset row found" -- this suite never exercises the
    ADR-0010 D12 delete-intent barrier's Dataset-found branches; those are
    covered against real PostgreSQL in
    tests/integration/test_dataset_delete_postgres_integration.py."""

    async def get_by_id(self, dataset_id: UUID) -> None:
        del dataset_id
        return None


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.pipeline_runs = FakePipelineRunRepository(store)
        self.pipeline_steps = FakePipelineStepRepository(store)
        self.datasets = FakeDatasetRepository()

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(
    store: FakeStore,
    *,
    worker: FakeWorker | None = None,
    registry: PipelineRegistry | None = None,
) -> PipelineSubmissionService:
    def create_uow() -> SubmissionUnitOfWork:
        return cast(SubmissionUnitOfWork, FakeUnitOfWork(store))

    return PipelineSubmissionService(
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
        registry=registry or make_test_registry(),
        worker=cast(Any, worker or FakeWorker()),
        config_fingerprint=CONFIG_FINGERPRINT,
    )


async def default_prepare(uow: Any) -> SubmissionTargets:
    del uow
    return SubmissionTargets(dataset_id=None, source_id=None)


def tracking_prepare(store: FakeStore, *, dataset_id: UUID | None = None) -> Any:
    async def prepare(uow: Any) -> SubmissionTargets:
        del uow
        store.prepare_calls += 1
        return SubmissionTargets(dataset_id=dataset_id, source_id=None)

    return prepare


def fake_integrity_error(constraint_name: str | None) -> IntegrityError:
    """Builds an ``IntegrityError`` whose ``.orig`` carries a
    ``constraint_name`` attribute, the same shape asyncpg's real
    ``UniqueViolationError``/``ForeignKeyViolationError`` etc. expose --
    lets unit tests simulate a specific constraint violation without a real
    PostgreSQL connection (SM-509 audit Finding 2)."""

    class _FakeOrig(Exception):
        def __init__(self, name: str | None) -> None:
            super().__init__("simulated constraint violation")
            self.constraint_name = name

    return IntegrityError("insert", {}, _FakeOrig(constraint_name))


def existing_run(
    *,
    idempotency_key: str | None,
    payload_hash: str,
    status: PipelineRunStatus,
    run_id: UUID | None = None,
    pipeline_type: PipelineType = PipelineType.REMEMBER,
    dataset_id: UUID | None = None,
) -> PipelineRun:
    return PipelineRun(
        id=run_id or uuid4(),
        pipeline_type=pipeline_type,
        dataset_id=dataset_id,
        source_id=None,
        status=status,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        input={},
        progress=0.0,
        current_step=None,
        attempt=0,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint=CONFIG_FINGERPRINT,
        error_code=None,
        error_message=None,
        metrics={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=None,
    )


# --- SUBMISSION --------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_submission_creates_queued_run_with_full_step_plan() -> None:
    store = FakeStore()
    service = service_for(store)

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "hello"},
        idempotency_key=None,
        prepare=default_prepare,
    )

    assert outcome.created is True
    assert outcome.status == PipelineRunStatus.QUEUED
    assert store.commits == 1
    assert len(store.runs) == 1
    run = store.runs[0]
    assert run.status == PipelineRunStatus.QUEUED
    assert run.attempt == 0
    assert run.next_attempt_at is None
    assert run.config_fingerprint == CONFIG_FINGERPRINT

    steps = [step for step in store.steps if step.run_id == run.id]
    assert len(steps) == 2
    assert {step.status for step in steps} == {PipelineStepStatus.QUEUED}
    assert all(step.attempt == 0 for step in steps)
    ordered = sorted(steps, key=lambda step: step.ordinal)
    assert [step.name for step in ordered] == ["step_a", "step_b"]
    assert ordered[0].input_hash is not None  # derivable at submission time
    assert ordered[1].input_hash is None  # depends on step_a's future output


@pytest.mark.asyncio
async def test_new_submission_persists_target_ids_from_prepare_hook() -> None:
    store = FakeStore()
    dataset_id = uuid4()
    service = service_for(store)

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "x"},
        idempotency_key=None,
        prepare=tracking_prepare(store, dataset_id=dataset_id),
    )

    assert outcome.dataset_id == dataset_id
    assert store.prepare_calls == 1
    assert store.runs[0].dataset_id == dataset_id


@pytest.mark.asyncio
async def test_commit_happens_before_outcome_is_observable() -> None:
    store = FakeStore()
    service = service_for(store)

    assert store.commits == 0
    await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "x"},
        idempotency_key=None,
        prepare=default_prepare,
    )
    assert store.commits == 1


@pytest.mark.asyncio
async def test_registry_build_step_plan_is_the_source_of_the_persisted_plan() -> None:
    store = FakeStore()
    registry = make_test_registry()
    service = service_for(store, registry=registry)

    await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "x"},
        idempotency_key=None,
        prepare=default_prepare,
    )

    expected_plan = registry.build_step_plan(PipelineType.REMEMBER, run_input={"seed": "x"})
    persisted_names = sorted(step.name for step in store.steps)
    assert persisted_names == sorted(plan.name for plan in expected_plan)


@pytest.mark.asyncio
async def test_unregistered_pipeline_type_creates_no_run() -> None:
    store = FakeStore()
    registry = make_test_registry(pipeline_type=PipelineType.REMEMBER)
    service = service_for(store, registry=registry)

    with pytest.raises(UnknownPipelineTypeError):
        await service.submit(
            pipeline_type=PipelineType.COGNIFY,
            work_input={"seed": "x"},
            idempotency_key=None,
            prepare=default_prepare,
        )

    assert store.runs == []
    assert store.steps == []
    assert store.commits == 0


# --- HASH / IDENTITY ----------------------------------------------------------


def test_canonical_work_payload_hash_is_deterministic() -> None:
    payload = {"b": 2, "a": 1}
    assert canonical_work_payload_hash(payload) == canonical_work_payload_hash({"a": 1, "b": 2})


def test_wait_is_not_a_parameter_of_the_canonical_hash_function() -> None:
    import inspect

    signature = inspect.signature(canonical_work_payload_hash)
    assert "wait" not in signature.parameters


@pytest.mark.asyncio
async def test_same_key_and_hash_resolves_the_same_run_without_duplicate_steps() -> None:
    store = FakeStore()
    service = service_for(store)
    work_input = {"seed": "same"}

    first = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input,
        idempotency_key="client-key-1",
        prepare=default_prepare,
    )
    second = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input,
        idempotency_key="client-key-1",
        prepare=default_prepare,
    )

    assert first.run_id == second.run_id
    assert first.created is True
    assert second.created is False
    assert len(store.runs) == 1
    assert len([step for step in store.steps if step.run_id == first.run_id]) == 2


@pytest.mark.asyncio
async def test_same_key_different_hash_returns_409_and_does_not_mutate_original() -> None:
    store = FakeStore()
    service = service_for(store)

    original = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "A"},
        idempotency_key="client-key-2",
        prepare=default_prepare,
    )

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "B"},
            idempotency_key="client-key-2",
            prepare=default_prepare,
        )

    assert error.value.status_code == 409
    assert error.value.code.value == "IDEMPOTENCY_CONFLICT"
    assert len(store.runs) == 1
    reloaded = store.runs[0]
    assert reloaded.id == original.run_id
    assert reloaded.payload_hash == canonical_work_payload_hash({"seed": "A"})


@pytest.mark.asyncio
async def test_no_key_same_payload_creates_two_distinct_runs() -> None:
    store = FakeStore()
    service = service_for(store)
    work_input = {"seed": "dup"}

    first = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input,
        idempotency_key=None,
        prepare=default_prepare,
    )
    second = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input,
        idempotency_key=None,
        prepare=default_prepare,
    )

    assert first.run_id != second.run_id
    assert len(store.runs) == 2


@pytest.mark.asyncio
async def test_sys_prefixed_idempotency_key_is_rejected_with_400_and_zero_state() -> None:
    store = FakeStore()
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key="sys:retry:whatever",
            prepare=default_prepare,
        )

    assert error.value.status_code == 400
    assert error.value.code.value == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"
    assert store.runs == []
    assert store.steps == []
    assert store.commits == 0


# --- EXISTING RUN STATUS MATRIX ----------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        PipelineRunStatus.QUEUED,
        PipelineRunStatus.RUNNING,
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLING,
        PipelineRunStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_existing_run_status_matrix_resolves_without_creating_new_run(
    status: PipelineRunStatus,
) -> None:
    store = FakeStore()
    payload_hash = canonical_work_payload_hash({"seed": "matrix"})
    run = existing_run(idempotency_key="matrix-key", payload_hash=payload_hash, status=status)
    store.add_run(run)
    service = service_for(store)

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "matrix"},
        idempotency_key="matrix-key",
        prepare=default_prepare,
    )

    assert outcome.created is False
    assert outcome.run_id == run.id
    assert outcome.status == status
    assert len(store.runs) == 1


# --- CROSS-PIPELINE IDEMPOTENCY (SM-509 audit Finding 1) ---------------------


@pytest.mark.asyncio
async def test_same_key_same_hash_different_pipeline_type_is_a_409_not_a_replay() -> None:
    """``Idempotency-Key`` is GLOBAL across ``pipeline_runs`` (the partial
    unique index has no ``pipeline_type`` predicate) -- a matching
    ``payload_hash`` alone is not proof of "same work" if the caller also
    switched ``pipeline_type`` under the same key."""

    store = FakeStore()
    work_input = {"seed": "cross-pipeline"}
    payload_hash = canonical_work_payload_hash(work_input)
    original = existing_run(
        idempotency_key="cross-pipeline-key",
        payload_hash=payload_hash,
        status=PipelineRunStatus.SUCCEEDED,
        pipeline_type=PipelineType.REMEMBER,
    )
    store.add_run(original)
    registry = PipelineRegistry(
        [
            PipelineDefinition(
                pipeline_type=PipelineType.COGNIFY,
                steps=(
                    PipelineStepDefinition(
                        name="step_a",
                        definition_id="step_a:v1",
                        step=NoOpStep(),
                        input_deriver=const_deriver,
                    ),
                ),
            )
        ]
    )
    service = service_for(store, registry=registry)

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.COGNIFY,
            work_input=work_input,
            idempotency_key="cross-pipeline-key",
            prepare=default_prepare,
        )

    assert error.value.status_code == 409
    assert error.value.code.value == "IDEMPOTENCY_CONFLICT"
    assert len(store.runs) == 1
    assert store.runs[0].id == original.id
    assert store.runs[0].pipeline_type == PipelineType.REMEMBER  # untouched


# --- BUILD STEP PLAN ONLY FOR NEW WORK (SM-509 audit Finding 5) --------------


class SpyRegistry:
    """Wraps a real :class:`PipelineRegistry` and records whether
    ``build_step_plan`` was ever called, without changing its behavior."""

    def __init__(self, inner: PipelineRegistry) -> None:
        self._inner = inner
        self.build_step_plan_calls = 0

    def get(self, pipeline_type: PipelineType) -> Any:
        return self._inner.get(pipeline_type)

    def build_step_plan(self, pipeline_type: PipelineType, *, run_input: Any) -> Any:
        self.build_step_plan_calls += 1
        return self._inner.build_step_plan(pipeline_type, run_input=run_input)


@pytest.mark.asyncio
async def test_existing_terminal_match_never_calls_build_step_plan() -> None:
    """Even a registry that has no definition at all for this pipeline type
    must not matter for replaying an existing match -- the existing-run path
    never consults the registry (SM-509 audit Finding 5)."""

    store = FakeStore()
    payload_hash = canonical_work_payload_hash({"seed": "no-registry-needed"})
    run = existing_run(
        idempotency_key="no-registry-key",
        payload_hash=payload_hash,
        status=PipelineRunStatus.SUCCEEDED,
    )
    store.add_run(run)
    spy_registry = SpyRegistry(PipelineRegistry([]))  # empty: no definitions at all
    service = service_for(store, registry=cast(Any, spy_registry))

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "no-registry-needed"},
        idempotency_key="no-registry-key",
        prepare=default_prepare,
    )

    assert outcome.created is False
    assert outcome.run_id == run.id
    assert spy_registry.build_step_plan_calls == 0


# --- WORKER AVAILABILITY ------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_disabled_rejects_new_submission_with_503_and_zero_state() -> None:
    store = FakeStore()
    service = service_for(store, worker=FakeWorker(enabled=False, is_running=False))

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key=None,
            prepare=default_prepare,
        )

    assert error.value.status_code == 503
    assert error.value.code.value == "WORKER_DISABLED"
    assert store.runs == []
    assert store.steps == []
    assert store.commits == 0


@pytest.mark.asyncio
async def test_worker_unready_not_running_rejects_new_submission_with_503() -> None:
    store = FakeStore()
    service = service_for(store, worker=FakeWorker(enabled=True, is_running=False))

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key=None,
            prepare=default_prepare,
        )

    assert error.value.status_code == 503
    assert store.runs == []


@pytest.mark.asyncio
async def test_worker_disabled_never_falls_back_to_synchronous_execution() -> None:
    store = FakeStore()
    service = service_for(store, worker=FakeWorker(enabled=False, is_running=False))

    async def failing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        raise AssertionError("prepare() must never run when the worker gate rejects the request")

    with pytest.raises(SofiasMemoryError):
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key=None,
            prepare=failing_prepare,
        )


@pytest.mark.asyncio
async def test_worker_unavailable_still_replays_existing_terminal_run() -> None:
    """SM-509 Part I resolution: terminal safe-replay never needs new
    execution, so it is exempt from the worker-availability gate."""

    store = FakeStore()
    payload_hash = canonical_work_payload_hash({"seed": "terminal"})
    run = existing_run(
        idempotency_key="terminal-key",
        payload_hash=payload_hash,
        status=PipelineRunStatus.SUCCEEDED,
    )
    store.add_run(run)
    service = service_for(store, worker=FakeWorker(enabled=False, is_running=False))

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "terminal"},
        idempotency_key="terminal-key",
        prepare=default_prepare,
    )

    assert outcome.created is False
    assert outcome.run_id == run.id
    assert outcome.status == PipelineRunStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status",
    [
        PipelineRunStatus.QUEUED,
        PipelineRunStatus.RUNNING,
        PipelineRunStatus.CANCELLING,
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_worker_unavailable_still_resolves_any_existing_matching_run_status(
    status: PipelineRunStatus,
) -> None:
    """SM-509 audit Finding 3: the worker-availability gate applies only to
    creating NEW state -- resolving an already-existing matching run never
    needs the worker, in ANY of the six statuses, including QUEUED/RUNNING/
    CANCELLING which still depend on the worker to ever progress. PostgreSQL
    remains the authority either way; a caller using ``wait=true`` on a
    still-in-flight run just keeps waiting/times out normally."""

    store = FakeStore()
    payload_hash = canonical_work_payload_hash({"seed": "any-status"})
    run = existing_run(idempotency_key="any-status-key", payload_hash=payload_hash, status=status)
    store.add_run(run)
    service = service_for(store, worker=FakeWorker(enabled=False, is_running=False))

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input={"seed": "any-status"},
        idempotency_key="any-status-key",
        prepare=default_prepare,
    )

    assert outcome.created is False
    assert outcome.run_id == run.id
    assert outcome.status == status
    assert len(store.runs) == 1  # unchanged, not mutated


# --- IDEMPOTENCY RACE (IntegrityError recovery, SM-509 Part G/X) -------------


@pytest.mark.asyncio
async def test_integrity_error_race_loser_resolves_the_winner_on_hash_match() -> None:
    store = FakeStore()
    service = service_for(store)
    work_input = {"seed": "race"}
    payload_hash = canonical_work_payload_hash(work_input)
    winner = existing_run(
        idempotency_key="race-key", payload_hash=payload_hash, status=PipelineRunStatus.QUEUED
    )

    async def losing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        # Simulate the winner's concurrent commit landing between this
        # loser's own lookup (which found nothing) and its own INSERT.
        store.add_run(winner)
        store.fail_next_add = fake_integrity_error("uq_pipeline_runs_idempotency_key")
        return SubmissionTargets(dataset_id=None, source_id=None)

    outcome = await service.submit(
        pipeline_type=PipelineType.REMEMBER,
        work_input=work_input,
        idempotency_key="race-key",
        prepare=losing_prepare,
    )

    assert outcome.created is False
    assert outcome.run_id == winner.id
    assert len(store.runs) == 1  # the loser's own row never survived


@pytest.mark.asyncio
async def test_integrity_error_race_loser_gets_conflict_on_hash_mismatch() -> None:
    store = FakeStore()
    service = service_for(store)
    winner = existing_run(
        idempotency_key="race-key-2",
        payload_hash=canonical_work_payload_hash({"seed": "winner-work"}),
        status=PipelineRunStatus.QUEUED,
    )

    async def losing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        store.add_run(winner)
        store.fail_next_add = fake_integrity_error("uq_pipeline_runs_idempotency_key")
        return SubmissionTargets(dataset_id=None, source_id=None)

    with pytest.raises(SofiasMemoryError) as error:
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "loser-work"},
            idempotency_key="race-key-2",
            prepare=losing_prepare,
        )

    assert error.value.status_code == 409
    assert len(store.runs) == 1


@pytest.mark.asyncio
async def test_unrelated_integrity_error_with_key_but_no_matching_run_reraises() -> None:
    store = FakeStore()
    service = service_for(store)

    async def failing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        store.fail_next_add = fake_integrity_error("some_other_constraint")
        return SubmissionTargets(dataset_id=None, source_id=None)

    with pytest.raises(IntegrityError):
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key="orphan-key",
            prepare=failing_prepare,
        )

    assert store.runs == []


@pytest.mark.asyncio
async def test_integrity_error_without_any_key_always_reraises() -> None:
    store = FakeStore()
    service = service_for(store)

    async def failing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        store.fail_next_add = fake_integrity_error("uq_pipeline_runs_idempotency_key")
        return SubmissionTargets(dataset_id=None, source_id=None)

    with pytest.raises(IntegrityError):
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "x"},
            idempotency_key=None,
            prepare=failing_prepare,
        )


@pytest.mark.asyncio
async def test_integrity_error_unrelated_constraint_reraises_despite_coincidence() -> None:
    """SM-509 audit Finding 2: an IntegrityError must match the exact
    ``uq_pipeline_runs_idempotency_key`` constraint to ever be reinterpreted
    as an idempotency race -- a failure from ANY other constraint always
    propagates unchanged, even when a fresh lookup would coincidentally find
    a run for that same key (e.g. a previous, unrelated request already used
    it for genuinely different work)."""

    store = FakeStore()
    service = service_for(store)
    coincidental_winner = existing_run(
        idempotency_key="coincidence-key",
        payload_hash=canonical_work_payload_hash({"seed": "unrelated-prior-work"}),
        status=PipelineRunStatus.SUCCEEDED,
    )

    async def failing_prepare(uow: Any) -> SubmissionTargets:
        del uow
        # This run only appears at prepare() time -- the caller's own
        # initial lookup (before submit() ever reached prepare()) found
        # nothing for this key, exactly like a genuinely concurrent commit
        # landing mid-transaction. The failure itself is a completely
        # different constraint (e.g. a broken FK from prepare()) -- NOT the
        # idempotency-key unique index.
        store.add_run(coincidental_winner)
        store.fail_next_add = fake_integrity_error("sources_dataset_id_fkey")
        return SubmissionTargets(dataset_id=None, source_id=None)

    with pytest.raises(IntegrityError):
        await service.submit(
            pipeline_type=PipelineType.REMEMBER,
            work_input={"seed": "new-unrelated-work"},
            idempotency_key="coincidence-key",
            prepare=failing_prepare,
        )

    # The pre-existing winner is untouched -- no conflict was fabricated, no
    # new run was created, nothing was silently "resolved".
    assert len(store.runs) == 1
    assert store.runs[0].id == coincidental_winner.id


# --- PreparationHook concurrency contract (docstring guard) ------------------


def test_preparation_hook_docstring_documents_its_own_concurrency_responsibility() -> None:
    """Not a full-string comparison -- just a guard that the hook's
    documented contract keeps stating (in some form) that IT owns races on
    ITS OWN target's uniqueness constraint, and that the submitter's own
    race recovery is scoped only to ``uq_pipeline_runs_idempotency_key``.
    Protects against this docstring being silently deleted/shortened away
    without protecting against normal wording edits.

    ``PreparationHook`` is a ``Callable[...]`` type alias, not a function or
    class -- the trailing string literal that documents it is a convention
    readable in source but never attached as a real ``__doc__`` attribute at
    runtime, so this guard reads the module's own source text instead of
    ``PreparationHook.__doc__`` (which is always ``None`` for a plain type
    alias)."""

    import inspect

    import sofias_memory.services.pipeline_submission as pipeline_submission_module

    source = inspect.getsource(pipeline_submission_module)

    assert "own uniqueness or concurrency constraint" in source.lower()
    assert IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT in source
    assert "must never depend on the submitter" in source.lower()
