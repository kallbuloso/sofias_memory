"""Unit tests for the SM-510 Cognify pipeline steps and production registry.

Everything here is exercised against in-memory doubles: the durable
PostgreSQL behavior (real generation flip, restart, atomicity) is proven in
``tests/integration/test_cognify_async_postgres_integration.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import (
    CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE,
    PermanentPipelineStepError,
    RetryablePipelineStepError,
)
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    StepResult,
    build_default_pipeline_registry,
)
from sofias_memory.pipelines.retry_policy import RetryPolicy
from sofias_memory.pipelines.steps.cognify import (
    ACTIVATE_GENERATION_DEFINITION_ID,
    ACTIVATE_GENERATION_STEP,
    COGNIFY_BATCH_MISSING_ERROR_CODE,
    COGNIFY_DEPENDENCY_ERROR_CODE,
    COGNIFY_REQUEST_ERROR_CODE,
    COGNIFY_RESOURCE_MISSING_ERROR_CODE,
    COGNIFY_SERVICE_RESOURCE,
    PROCESS_SOURCES_DEFINITION_ID,
    PROCESS_SOURCES_STEP,
    STAGED_BATCH_MAX_AGE_SECONDS,
    ActivateGenerationStep,
    ProcessSourcesStep,
    activate_generation_input,
    process_sources_input,
)
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.cognify import (
    COGNIFY_RESULT_METRIC_KEY,
    CognifyPreparedBatch,
    CognifyPreparedSourcePlan,
    CognifyProcessOutcome,
)
from sofias_memory.services.pipeline_recovery import PipelineRecoveryService

DATASET_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
RECOVERY_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
CONFIG_FINGERPRINT = "a" * 64


# --- production registry (SM-510 Cognify; SM-511 added IMPROVE; SM-512 FORGET) -------


def test_default_registry_contains_cognify_improve_and_forget() -> None:
    registry = build_default_pipeline_registry()

    assert len(registry) == 3
    assert registry.get(PipelineType.COGNIFY).pipeline_type == PipelineType.COGNIFY
    assert registry.get(PipelineType.IMPROVE).pipeline_type == PipelineType.IMPROVE
    assert registry.get(PipelineType.FORGET).pipeline_type == PipelineType.FORGET


@pytest.mark.parametrize(
    "pipeline_type",
    [PipelineType.REMEMBER],
)
def test_default_registry_leaves_future_pipelines_unregistered(
    pipeline_type: PipelineType,
) -> None:
    from sofias_memory.pipelines.registry import UnknownPipelineTypeError

    with pytest.raises(UnknownPipelineTypeError):
        build_default_pipeline_registry().get(pipeline_type)


def test_cognify_definition_declares_the_two_expected_steps_in_order() -> None:
    steps = build_default_pipeline_registry().get(PipelineType.COGNIFY).steps

    assert [step.name for step in steps] == [PROCESS_SOURCES_STEP, ACTIVATE_GENERATION_STEP]
    assert [step.definition_id for step in steps] == [
        PROCESS_SOURCES_DEFINITION_ID,
        ACTIVATE_GENERATION_DEFINITION_ID,
    ]


def test_cognify_steps_declare_their_cancellation_recovery_contracts() -> None:
    process_sources, activate_generation = (
        build_default_pipeline_registry().get(PipelineType.COGNIFY).steps
    )

    # Both steps are ATOMIC: neither commits anything outside the engine's own
    # transaction, so an orphaned RUNNING row proves nothing was committed and
    # recovery needs no reconciliation callback at all.
    assert process_sources.cancellation_recovery_mode == CancellationRecoveryMode.ATOMIC
    assert process_sources.cancellation_reconcile is None
    assert activate_generation.cancellation_recovery_mode == CancellationRecoveryMode.ATOMIC
    assert activate_generation.cancellation_reconcile is None


def test_submission_materializes_both_steps_with_the_first_hash_resolved() -> None:
    plan = build_default_pipeline_registry().build_step_plan(
        PipelineType.COGNIFY,
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
    )

    assert [(item.name, item.ordinal) for item in plan] == [
        (PROCESS_SOURCES_STEP, 0),
        (ACTIVATE_GENERATION_STEP, 1),
    ]
    assert plan[0].input_hash is not None
    assert plan[1].input_hash is None


# --- input derivation --------------------------------------------------------


def test_process_sources_input_is_the_work_identity_and_ignores_wait() -> None:
    run_input = {"dataset": "main", "source_ids": None, "rebuild": True}

    assert process_sources_input(run_input, {}) == run_input
    assert "wait" not in process_sources_input({**run_input, "wait": False}, {})


def test_activate_generation_input_is_unresolvable_until_the_prior_step_succeeds() -> None:
    assert activate_generation_input({"rebuild": True}, {}) is None
    assert activate_generation_input(
        {},
        {PROCESS_SOURCES_STEP: {"target_generation": 4, "rebuild": True, "chunks": 9}},
    ) == {"target_generation": 4, "rebuild": True}


# --- process_sources ---------------------------------------------------------


def planned_batch(outcome: CognifyProcessOutcome) -> CognifyPreparedBatch:
    """A batch whose ``planned_outcome()`` is exactly ``outcome``.

    Built from real ``CognifyPreparedSourcePlan`` rows so the step is exercised
    against the same shape the service produces, without any provider or
    database being involved.
    """

    sources = tuple(
        CognifyPreparedSourcePlan(
            prepared=cast(Any, object()),
            chunk_plans=(),
            chunks_created=outcome.chunks if index == 0 else 0,
            entities_created=outcome.entities if index == 0 else 0,
            relations_created=outcome.relations if index == 0 else 0,
        )
        for index in range(outcome.sources_processed)
    )
    return CognifyPreparedBatch(
        dataset_id=outcome.dataset_id,
        target_generation=outcome.target_generation,
        rebuild=outcome.rebuild,
        sources=sources,
        sources_to_activate=(),
        failed_source_ids=(),
        entity_plans={},
        relation_plans={},
    )


class FakeProcessor:
    """In-memory double for the two-phase ``CognifySourceProcessor``."""

    def __init__(self, outcome: CognifyProcessOutcome | None = None) -> None:
        self.outcome = outcome or CognifyProcessOutcome(
            dataset_id=DATASET_ID,
            target_generation=0,
            rebuild=False,
            sources_processed=2,
            chunks=7,
            entities=3,
            relations=1,
        )
        self.calls: list[dict[str, Any]] = []
        self.persisted: list[CognifyPreparedBatch] = []
        self.persisted_uows: list[Any] = []
        self.failure: Exception | None = None
        self.persisted_outcome: CognifyProcessOutcome | None = None

    async def prepare_batch(
        self,
        *,
        dataset_id: UUID,
        source_ids: list[UUID] | None,
        rebuild: bool,
    ) -> CognifyPreparedBatch:
        self.calls.append({"dataset_id": dataset_id, "source_ids": source_ids, "rebuild": rebuild})
        if self.failure is not None:
            raise self.failure
        return planned_batch(self.outcome)

    async def persist_batch(
        self,
        uow: Any,
        batch: CognifyPreparedBatch,
    ) -> CognifyProcessOutcome:
        self.persisted.append(batch)
        self.persisted_uows.append(uow)
        return self.persisted_outcome or batch.planned_outcome()


def make_context(
    *,
    run_input: Mapping[str, Any],
    resources: Mapping[str, Any] | None = None,
    step_outputs: Mapping[str, Mapping[str, Any]] | None = None,
    dataset_id: UUID | None = DATASET_ID,
    run_id: UUID = RUN_ID,
) -> PipelineContext:
    return PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.COGNIFY,
        dataset_id=dataset_id,
        source_id=None,
        run_input=dict(run_input),
        step_outputs=dict(step_outputs or {}),
        session_factory=cast(Any, None),
        resources=dict(resources or {}),
    )


@pytest.mark.asyncio
async def test_process_sources_forwards_the_persisted_run_input() -> None:
    processor = FakeProcessor()
    source_id = uuid4()
    context = make_context(
        run_input={"dataset": "main", "source_ids": [str(source_id)], "rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    await ProcessSourcesStep().execute(context)

    assert processor.calls == [
        {"dataset_id": DATASET_ID, "source_ids": [source_id], "rebuild": False}
    ]


@pytest.mark.asyncio
async def test_process_sources_output_is_json_safe_and_leaks_no_content() -> None:
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": True},
        resources={COGNIFY_SERVICE_RESOURCE: FakeProcessor()},
    )

    result = await ProcessSourcesStep().execute(context)

    assert set(result.output) == {
        "dataset_id",
        "target_generation",
        "rebuild",
        "sources_processed",
        "chunks",
        "entities",
        "relations",
    }
    # Round-trips through JSON unchanged, and carries no text/embedding/
    # prompt/provider payload of any kind (ADR-0009 SS 10).
    assert json.loads(json.dumps(result.output)) == result.output


@pytest.mark.asyncio
async def test_process_sources_classifies_dependency_failures_as_retryable() -> None:
    processor = FakeProcessor()
    processor.failure = DependencyUnavailableError("Embedding provider is unavailable.")
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    with pytest.raises(RetryablePipelineStepError) as raised:
        await ProcessSourcesStep().execute(context)

    assert raised.value.code == COGNIFY_DEPENDENCY_ERROR_CODE
    assert "Embedding provider" not in raised.value.message


@pytest.mark.asyncio
async def test_process_sources_classifies_request_failures_as_permanent() -> None:
    processor = FakeProcessor()
    processor.failure = SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=404,
        message="Dataset does not exist.",
    )
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    with pytest.raises(PermanentPipelineStepError) as raised:
        await ProcessSourcesStep().execute(context)

    assert raised.value.code == COGNIFY_REQUEST_ERROR_CODE


@pytest.mark.asyncio
async def test_process_sources_never_builds_its_own_dependencies() -> None:
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
        resources={},
    )

    with pytest.raises(PermanentPipelineStepError) as raised:
        await ProcessSourcesStep().execute(context)

    assert raised.value.code == COGNIFY_RESOURCE_MISSING_ERROR_CODE


@pytest.mark.asyncio
async def test_execute_stages_the_batch_and_persist_applies_it_to_the_engine_uow() -> None:
    """The SM-510 boundary contract, end to end on one step instance.

    ``execute`` writes nothing and stages the computed batch; ``persist``
    consumes it exactly once, against the transaction the *engine* supplies.
    """

    processor = FakeProcessor()
    step = ProcessSourcesStep()
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )
    engine_uow = object()

    result = await step.execute(context)
    assert processor.persisted == []

    await step.persist(context, result, cast(PostgresUnitOfWork, engine_uow))

    assert len(processor.persisted) == 1
    assert processor.persisted_uows == [engine_uow]


@pytest.mark.asyncio
async def test_persist_corrects_the_counters_when_a_source_was_dropped() -> None:
    """``persist`` runs before the engine writes ``PipelineStep.output``, so a
    source lost to per-source failure isolation must be reflected there."""

    processor = FakeProcessor()
    processor.persisted_outcome = CognifyProcessOutcome(
        dataset_id=DATASET_ID,
        target_generation=0,
        rebuild=False,
        sources_processed=1,
        chunks=4,
        entities=2,
        relations=0,
    )
    step = ProcessSourcesStep()
    context = make_context(
        run_input={"dataset": "main", "source_ids": None, "rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    result = await step.execute(context)
    assert result.output["sources_processed"] == 2

    await step.persist(context, result, cast(PostgresUnitOfWork, object()))

    assert result.output["sources_processed"] == 1
    assert result.output["chunks"] == 4
    assert result.output["relations"] == 0
    assert json.loads(json.dumps(result.output)) == result.output


@pytest.mark.asyncio
async def test_persist_without_a_staged_batch_fails_loudly() -> None:
    """An engine-invariant violation, never a silent empty cognify."""

    processor = FakeProcessor()
    context = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    with pytest.raises(PermanentPipelineStepError) as raised:
        await ProcessSourcesStep().persist(
            context, StepResult(), cast(PostgresUnitOfWork, object())
        )

    assert raised.value.code == COGNIFY_BATCH_MISSING_ERROR_CODE
    assert processor.persisted == []


@pytest.mark.asyncio
async def test_a_staged_batch_is_consumed_exactly_once() -> None:
    """Popped, not read: a replayed ``persist`` can never re-apply a batch."""

    processor = FakeProcessor()
    step = ProcessSourcesStep()
    context = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    result = await step.execute(context)
    await step.persist(context, result, cast(PostgresUnitOfWork, object()))

    with pytest.raises(PermanentPipelineStepError):
        await step.persist(context, result, cast(PostgresUnitOfWork, object()))

    assert len(processor.persisted) == 1


@pytest.mark.asyncio
async def test_compensate_discards_a_staged_batch() -> None:
    processor = FakeProcessor()
    step = ProcessSourcesStep()
    context = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    result = await step.execute(context)
    await step.compensate(context, result)

    with pytest.raises(PermanentPipelineStepError):
        await step.persist(context, result, cast(PostgresUnitOfWork, object()))
    assert processor.persisted == []


# --- activate_generation -----------------------------------------------------


@dataclass
class FakeDataset:
    id: UUID
    active_generation: int


@dataclass
class FakeEntity:
    id: UUID
    dataset_id: UUID
    generation: int
    name: str = "PostgreSQL"
    entity_type: str = "Technology"
    description: str = "A database."
    importance_weight: float = 1.0


@dataclass
class FakeRun:
    id: UUID
    metrics: dict[str, Any] = field(default_factory=dict)


class FakeDatasetRepository:
    def __init__(self, dataset: FakeDataset | None) -> None:
        self._dataset = dataset
        self.locked: list[UUID] = []

    async def get_by_id_for_update(self, dataset_id: UUID) -> FakeDataset | None:
        self.locked.append(dataset_id)
        return self._dataset


class FakeEntityRepository:
    def __init__(self, entities: list[FakeEntity]) -> None:
        self._entities = entities

    async def advance_active_generation(
        self,
        *,
        dataset_id: UUID,
        target_generation: int,
    ) -> list[FakeEntity]:
        advanced = [
            entity
            for entity in self._entities
            if entity.dataset_id == dataset_id and entity.generation < target_generation
        ]
        for entity in advanced:
            entity.generation = target_generation
        return advanced


class FakeGraphOutboxRepository:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def add_projection_command(self, command: Any) -> object:
        self.commands.append(command)
        return object()


class FakeRunRepository:
    def __init__(self, run: FakeRun | None) -> None:
        self._run = run

    async def get_by_id_for_update(self, run_id: UUID) -> FakeRun | None:
        return self._run


class FakeActivationUnitOfWork:
    def __init__(
        self,
        *,
        dataset: FakeDataset | None,
        entities: list[FakeEntity] | None = None,
        run: FakeRun | None = None,
    ) -> None:
        self.datasets = FakeDatasetRepository(dataset)
        self.entities = FakeEntityRepository(entities or [])
        self.graph_outbox = FakeGraphOutboxRepository()
        self.pipeline_runs = FakeRunRepository(run)


def activation_result(*, target_generation: int, rebuild: bool) -> StepResult:
    return StepResult(
        output={
            "dataset_id": str(DATASET_ID),
            "target_generation": target_generation,
            "rebuild": rebuild,
            "sources_processed": 2,
            "chunks": 7,
            "entities": 3,
            "relations": 1,
        }
    )


@pytest.mark.asyncio
async def test_activate_generation_execute_is_a_pure_pass_through() -> None:
    upstream = {"target_generation": 3, "rebuild": True, "chunks": 5}
    context = make_context(run_input={}, step_outputs={PROCESS_SOURCES_STEP: upstream})

    result = await ActivateGenerationStep().execute(context)

    assert result.output == upstream


@pytest.mark.asyncio
async def test_activate_generation_execute_requires_the_prior_step_output() -> None:
    with pytest.raises(PermanentPipelineStepError):
        await ActivateGenerationStep().execute(make_context(run_input={}))


@pytest.mark.asyncio
async def test_activate_generation_advances_dataset_and_entities_on_rebuild() -> None:
    dataset = FakeDataset(id=DATASET_ID, active_generation=0)
    entity = FakeEntity(id=uuid4(), dataset_id=DATASET_ID, generation=0)
    run = FakeRun(id=RUN_ID)
    uow = FakeActivationUnitOfWork(dataset=dataset, entities=[entity], run=run)

    await ActivateGenerationStep().persist(
        make_context(run_input={"rebuild": True}),
        activation_result(target_generation=1, rebuild=True),
        cast(PostgresUnitOfWork, uow),
    )

    assert dataset.active_generation == 1
    assert entity.generation == 1
    assert uow.datasets.locked == [DATASET_ID]
    assert [command.aggregate_type for command in uow.graph_outbox.commands] == ["entity"]
    assert run.metrics[COGNIFY_RESULT_METRIC_KEY]["generation"] == 1


@pytest.mark.asyncio
async def test_activate_generation_is_idempotent_when_replayed() -> None:
    dataset = FakeDataset(id=DATASET_ID, active_generation=1)
    entity = FakeEntity(id=uuid4(), dataset_id=DATASET_ID, generation=1)
    uow = FakeActivationUnitOfWork(dataset=dataset, entities=[entity], run=FakeRun(id=RUN_ID))

    await ActivateGenerationStep().persist(
        make_context(run_input={"rebuild": True}),
        activation_result(target_generation=1, rebuild=True),
        cast(PostgresUnitOfWork, uow),
    )

    assert dataset.active_generation == 1
    assert uow.graph_outbox.commands == []


@pytest.mark.asyncio
async def test_activate_generation_never_touches_the_generation_without_rebuild() -> None:
    dataset = FakeDataset(id=DATASET_ID, active_generation=0)
    entity = FakeEntity(id=uuid4(), dataset_id=DATASET_ID, generation=0)
    run = FakeRun(id=RUN_ID)
    uow = FakeActivationUnitOfWork(dataset=dataset, entities=[entity], run=run)

    await ActivateGenerationStep().persist(
        make_context(run_input={"rebuild": False}),
        activation_result(target_generation=0, rebuild=False),
        cast(PostgresUnitOfWork, uow),
    )

    assert dataset.active_generation == 0
    assert entity.generation == 0
    assert uow.graph_outbox.commands == []
    assert run.metrics[COGNIFY_RESULT_METRIC_KEY] == {
        "dataset_id": str(DATASET_ID),
        "generation": 0,
        "sources_processed": 2,
        "chunks": 7,
        "entities": 3,
        "relations": 1,
    }


@pytest.mark.asyncio
async def test_persisted_run_metrics_carry_no_content_or_provider_payload() -> None:
    run = FakeRun(id=RUN_ID)
    uow = FakeActivationUnitOfWork(dataset=FakeDataset(id=DATASET_ID, active_generation=0), run=run)

    await ActivateGenerationStep().persist(
        make_context(run_input={"rebuild": False}),
        activation_result(target_generation=0, rebuild=False),
        cast(PostgresUnitOfWork, uow),
    )

    encoded = json.dumps(run.metrics)
    assert json.loads(encoded) == run.metrics
    assert set(run.metrics[COGNIFY_RESULT_METRIC_KEY]) == {
        "dataset_id",
        "generation",
        "sources_processed",
        "chunks",
        "entities",
        "relations",
    }


@pytest.mark.asyncio
async def test_activate_generation_fails_when_its_dataset_disappeared() -> None:
    uow = FakeActivationUnitOfWork(dataset=None)

    with pytest.raises(PermanentPipelineStepError):
        await ActivateGenerationStep().persist(
            make_context(run_input={"rebuild": True}),
            activation_result(target_generation=1, rebuild=True),
            cast(PostgresUnitOfWork, uow),
        )


# --- stale-CANCELLING recovery: ATOMIC, no reconciliation at all --------------


def recovery_run(**overrides: Any) -> PipelineRun:
    defaults: dict[str, Any] = dict(
        id=RUN_ID,
        pipeline_type=PipelineType.COGNIFY,
        dataset_id=DATASET_ID,
        source_id=None,
        status=PipelineRunStatus.CANCELLING,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={"dataset": "main", "source_ids": None, "rebuild": False},
        progress=0.0,
        current_step=PROCESS_SOURCES_STEP,
        attempt=1,
        worker_id="wk-lost",
        heartbeat_at=RECOVERY_NOW - timedelta(seconds=600),
        config_fingerprint=CONFIG_FINGERPRINT,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=RECOVERY_NOW - timedelta(seconds=700),
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=None,
    )
    defaults.update(overrides)
    return PipelineRun(**defaults)


def recovery_step(status: PipelineStepStatus, name: str, ordinal: int) -> PipelineStep:
    return PipelineStep(
        id=uuid4(),
        run_id=RUN_ID,
        name=name,
        ordinal=ordinal,
        status=status,
        attempt=1,
        input_hash=None,
        output={},
        metrics={},
        error=None,
        started_at=RECOVERY_NOW - timedelta(seconds=650),
        finished_at=None,
    )


def recovery_service() -> PipelineRecoveryService:
    return PipelineRecoveryService(
        session_factory=cast(AsyncSessionFactory, object()),
        registry=build_default_pipeline_registry(),
        stale_after_seconds=300,
        config_fingerprint=CONFIG_FINGERPRINT,
        retry_policy=RetryPolicy(jitter_source=lambda: 0.0),
    )


class ExplodingUnitOfWork:
    """Any attribute access is a failed test: an ATOMIC step's recovery must
    reach no repository at all -- no source/chunk/summary completeness read is
    needed to decide, which is the entire point of the ATOMIC contract."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"ATOMIC recovery must not read durable state (touched {name!r}).")


@pytest.mark.asyncio
async def test_orphaned_process_sources_recovers_straight_to_cancelled() -> None:
    """ADR-0009 SS I case A. ``process_sources`` commits its whole batch inside
    the engine's own transaction, in the same commit as its ``succeeded``
    transition, so an orphaned RUNNING row is structural proof that nothing
    was committed: recovery cancels with no check."""

    service = recovery_service()
    run = recovery_run()
    orphan = recovery_step(PipelineStepStatus.RUNNING, PROCESS_SOURCES_STEP, 0)
    queued = recovery_step(PipelineStepStatus.QUEUED, ACTIVATE_GENERATION_STEP, 1)

    await service._recover_cancelling(
        cast(Any, ExplodingUnitOfWork()), run, [orphan, queued], now=RECOVERY_NOW
    )

    assert run.status == PipelineRunStatus.CANCELLED
    assert run.error_code is None
    assert orphan.status == PipelineStepStatus.CANCELLED
    assert queued.status == PipelineStepStatus.CANCELLED


@pytest.mark.asyncio
async def test_orphaned_process_sources_recovers_to_cancelled_for_a_rebuild_too() -> None:
    """The old RECONCILABLE callback refused to decide for ``rebuild=true`` (it
    could not tell target generation N+1 from N in durable state) and failed the
    run CANCEL_RECOVERY_AMBIGUOUS. Under ATOMIC that question never arises: no
    generation was activated because nothing committed."""

    service = recovery_service()
    run = recovery_run(input={"dataset": "main", "source_ids": None, "rebuild": True})
    orphan = recovery_step(PipelineStepStatus.RUNNING, PROCESS_SOURCES_STEP, 0)

    await service._recover_cancelling(
        cast(Any, ExplodingUnitOfWork()), run, [orphan], now=RECOVERY_NOW
    )

    assert run.status == PipelineRunStatus.CANCELLED
    assert run.error_code != CANCEL_RECOVERY_AMBIGUOUS_ERROR_CODE
    assert orphan.status == PipelineStepStatus.CANCELLED


@pytest.mark.asyncio
async def test_orphaned_activate_generation_recovers_straight_to_cancelled() -> None:
    service = recovery_service()
    run = recovery_run(current_step=ACTIVATE_GENERATION_STEP)
    succeeded = recovery_step(PipelineStepStatus.SUCCEEDED, PROCESS_SOURCES_STEP, 0)
    orphan = recovery_step(PipelineStepStatus.RUNNING, ACTIVATE_GENERATION_STEP, 1)

    await service._recover_cancelling(
        cast(Any, ExplodingUnitOfWork()), run, [succeeded, orphan], now=RECOVERY_NOW
    )

    assert run.status == PipelineRunStatus.CANCELLED
    assert succeeded.status == PipelineStepStatus.SUCCEEDED
    assert orphan.status == PipelineStepStatus.CANCELLED


# --- staged batch: transient, recomputable, non-leaking ----------------------


@pytest.mark.asyncio
async def test_a_fenced_out_attempt_does_not_leak_its_staged_batch_forever() -> None:
    """The one path that stages a batch and never calls ``persist``:
    ``PipelineEngine._commit_success`` finds ``_load_fenced_running_step``
    returning ``None`` (this worker was superseded mid-attempt) and returns
    ``abandoned=True``. ``compensate`` is not invoked there, so the entry would
    otherwise sit in this long-lived step instance holding chunk text and
    embeddings. The age sweep on the next ``execute`` drops it."""

    processor = FakeProcessor()
    step = ProcessSourcesStep()
    abandoned = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    await step.execute(abandoned)
    assert len(step._staged) == 1

    # Age the abandoned entry past the bound, then run an unrelated run.
    staged_at, batch = step._staged[abandoned.run_id]
    step._staged[abandoned.run_id] = (staged_at - STAGED_BATCH_MAX_AGE_SECONDS - 1.0, batch)

    live = make_context(
        run_id=uuid4(),
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )
    await step.execute(live)

    assert abandoned.run_id not in step._staged
    assert list(step._staged) == [live.run_id]


@pytest.mark.asyncio
async def test_the_sweep_never_drops_a_batch_still_awaiting_its_persist() -> None:
    """The bound is memory hygiene, never correctness: a concurrently staged
    batch whose ``persist`` has not run yet must survive another run's
    ``execute``."""

    processor = FakeProcessor()
    step = ProcessSourcesStep()
    first = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )
    second = make_context(
        run_id=uuid4(),
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    first_result = await step.execute(first)
    await step.execute(second)

    assert set(step._staged) == {first.run_id, second.run_id}

    await step.persist(first, first_result, cast(PostgresUnitOfWork, object()))
    assert set(step._staged) == {second.run_id}


@pytest.mark.asyncio
async def test_a_lost_staged_batch_is_simply_recomputed_by_a_fresh_execute() -> None:
    """Nothing durable ever depended on the staged batch: losing it (crash,
    worker kill, cancellation, sweep) costs only redoing ``execute``'s external
    work -- a fresh attempt rebuilds it from scratch and persists normally."""

    processor = FakeProcessor()
    step = ProcessSourcesStep()
    context = make_context(
        run_input={"rebuild": False},
        resources={COGNIFY_SERVICE_RESOURCE: processor},
    )

    await step.execute(context)
    step._staged.clear()  # crash / kill / sweep: the cache is gone

    retried = await step.execute(context)
    await step.persist(context, retried, cast(PostgresUnitOfWork, object()))

    assert len(processor.calls) == 2
    assert len(processor.persisted) == 1
    assert step._staged == {}
