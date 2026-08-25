"""Unit tests for the Forget B5 pipeline steps (SM-512, ADR-0009 SS O).

Covers ``AuthoritativeMutationStep``'s target-recovery decision tree
(fresh / no-op / REENTRANT / RESUMED / BLOCKED) for SOURCE and DATASET
scope against a fake unit of work, plus ``FinalizeTargetStep``/
``FinalizeResultStep`` aggregation and the Neo4j/filesystem-facing steps
against fake resources. Real orphan-detection/mutation correctness and the
EVERYTHING scope's dynamic dataset loop are proven against real PostgreSQL
in the integration suite (SM-512 self-audit: documented, not silently
skipped).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun, Source
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.pipelines.registry import StepResult
from sofias_memory.pipelines.steps.forget import (
    AUTHORITATIVE_MUTATION_STEP,
    STORAGE_DELETION_STEP,
    AuthoritativeMutationStep,
    FinalizeResultStep,
    FinalizeTargetStep,
    ForgetPipelineResources,
    ProjectionConvergenceStep,
    StorageDeletionStep,
)
from sofias_memory.services.forget import FORGET_TARGET_CONFLICT_ERROR_CODE
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxDrainResult


def make_context(
    *,
    dataset_id: UUID | None,
    source_id: UUID | None,
    run_input: dict[str, object],
    run_id: UUID | None = None,
    resources: object | None = None,
    step_outputs: dict[str, dict[str, object]] | None = None,
) -> PipelineContext:
    return PipelineContext(
        run_id=run_id or uuid4(),
        pipeline_type=PipelineType.FORGET,
        dataset_id=dataset_id,
        source_id=source_id,
        run_input=run_input,
        step_outputs=step_outputs or {},
        session_factory=None,  # type: ignore[arg-type] - unused by these steps' persist()
        resources={"forget_resources": resources} if resources is not None else {},
    )


def make_dataset(*, status: DatasetStatus = DatasetStatus.ACTIVE, generation: int = 0) -> Dataset:
    return Dataset(
        id=uuid4(),
        name="main",
        slug="main",
        description=None,
        status=status,
        active_generation=generation,
    )


def make_source(*, dataset_id: UUID, status: SourceStatus = SourceStatus.ACTIVE) -> Source:
    return Source(
        id=uuid4(),
        dataset_id=dataset_id,
        kind="text",
        name="s",
        mime_type="text/plain",
        content_sha256="a" * 64,
        byte_size=4,
        status=status,
        storage_uri="file:///data/x",
    )


class FakeDatasetsRepo:
    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset

    async def get_by_slug_for_update(self, slug: str) -> Dataset | None:
        return self.dataset if self.dataset.slug == slug else None

    async def get_by_id_for_update(self, dataset_id: UUID) -> Dataset | None:
        return self.dataset if self.dataset.id == dataset_id else None

    async def list_ids_for_everything_forget(self) -> list[UUID]:
        return [self.dataset.id]


class FakeSourcesRepo:
    def __init__(self, source: Source) -> None:
        self.source = source

    async def get_by_id_for_update(self, source_id: UUID) -> Source | None:
        return self.source if self.source.id == source_id else None

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return self.source if self.source.id == source_id else None

    async def list_for_dataset_for_update(self, dataset_id: UUID) -> list[Source]:
        return [self.source] if self.source.dataset_id == dataset_id else []

    async def list_for_dataset_not_deleted(self, dataset_id: UUID) -> list[Source]:
        if self.source.dataset_id == dataset_id and self.source.status != SourceStatus.DELETED:
            return [self.source]
        return []


class EmptyContentRepo:
    async def list_for_source_generation(self, **kwargs: object) -> list[object]:
        return []

    async def list_active_current_for_dataset(self, **kwargs: object) -> list[object]:
        return []

    async def list_for_chunks(self, **kwargs: object) -> list[object]:
        return []

    async def list_active_current_by_ids(self, **kwargs: object) -> list[object]:
        return []

    async def list_active_current_incident_entity_ids(self, **kwargs: object) -> set[UUID]:
        return set()

    async def list_entity_ids_with_authoritative_mentions(self, **kwargs: object) -> set[UUID]:
        return set()

    async def list_relation_ids_with_authoritative_evidence(self, **kwargs: object) -> set[UUID]:
        return set()

    async def list_active_for_forget(self, **kwargs: object) -> list[object]:
        return []

    async def add(self, item: object) -> object:
        return item


class FakeGraphOutboxRepo:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def add_projection_command(self, command: object) -> object:
        self.commands.append(command)
        return command


class FakePipelineRunsRepo:
    def __init__(
        self,
        *,
        running: PipelineRun | None = None,
        latest_source: PipelineRun | None = None,
        latest_dataset: PipelineRun | None = None,
    ) -> None:
        self.running = running
        self.latest_source = latest_source
        self.latest_dataset = latest_dataset

    async def find_running_forget_for_dataset_except(self, **kwargs: object) -> PipelineRun | None:
        return self.running

    async def find_latest_forget_for_source_except(self, **kwargs: object) -> PipelineRun | None:
        return self.latest_source

    async def find_latest_forget_for_dataset_except(self, **kwargs: object) -> PipelineRun | None:
        return self.latest_dataset

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        return None


class FakeForgetUow:
    def __init__(
        self,
        *,
        dataset: Dataset,
        source: Source,
        pipeline_runs: FakePipelineRunsRepo | None = None,
    ) -> None:
        self.datasets = FakeDatasetsRepo(dataset)
        self.sources = FakeSourcesRepo(source)
        self.documents = EmptyContentRepo()
        self.chunks = EmptyContentRepo()
        self.entity_mentions = EmptyContentRepo()
        self.relation_evidence = EmptyContentRepo()
        self.entities = EmptyContentRepo()
        self.relations = EmptyContentRepo()
        self.summaries = EmptyContentRepo()
        self.graph_outbox = FakeGraphOutboxRepo()
        self.pipeline_runs = pipeline_runs or FakePipelineRunsRepo()

    async def flush(self) -> None:
        return None


def make_run(
    *,
    payload_hash: str = "x",
    run_input: dict[str, object] | None = None,
    error_code: str | None = None,
) -> PipelineRun:
    return PipelineRun(
        id=uuid4(),
        pipeline_type=PipelineType.FORGET,
        dataset_id=None,
        source_id=None,
        status=PipelineRunStatus.RUNNING,
        idempotency_key=None,
        payload_hash=payload_hash,
        input=run_input or {},
        progress=0.0,
        current_step="authoritative_mutation",
        attempt=1,
        worker_id="w",
        heartbeat_at=None,
        config_fingerprint="cf",
        error_code=error_code,
        error_message=None,
        metrics={},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=None,
    )


# ---------------------------------------------------------------------------
# AuthoritativeMutationStep -- SOURCE scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_fresh_mutation_sets_deleting_and_proceeds() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.ACTIVE)
    uow = FakeForgetUow(dataset=dataset, source=source)
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": False,
        },
    )
    step = AuthoritativeMutationStep()
    result = StepResult(output={})

    await step.persist(context, result, uow)  # type: ignore[arg-type]

    assert source.status == SourceStatus.DELETING
    assert result.output["proceed"] is True
    assert result.output["scope"] == "source"


@pytest.mark.asyncio
async def test_source_already_deleted_is_a_safe_no_op() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETED)
    uow = FakeForgetUow(dataset=dataset, source=source)
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": False,
        },
    )
    result = StepResult(output={})

    await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert result.output["proceed"] is True
    assert result.output["documents_deactivated"] == 0
    assert source.status == SourceStatus.DELETED  # unchanged


@pytest.mark.asyncio
async def test_source_pending_memory_only_retry_is_a_safe_no_op() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.PENDING)
    uow = FakeForgetUow(dataset=dataset, source=source)
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": True,
        },
    )
    result = StepResult(output={})

    await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert result.output["proceed"] is True
    assert source.status == SourceStatus.PENDING


@pytest.mark.asyncio
async def test_source_reentrant_when_compatible_run_is_running() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.ACTIVE)
    run_input = {
        "scope": "source",
        "dataset": "main",
        "source_id": str(source.id),
        "memory_only": False,
    }
    running = make_run(run_input=run_input)
    uow = FakeForgetUow(
        dataset=dataset, source=source, pipeline_runs=FakePipelineRunsRepo(running=running)
    )
    context = make_context(dataset_id=dataset.id, source_id=source.id, run_input=run_input)
    result = StepResult(output={})

    await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert result.output["proceed"] is False
    assert source.status == SourceStatus.ACTIVE  # never mutated -- owned by the other run


@pytest.mark.asyncio
async def test_source_blocked_when_running_run_has_incompatible_intent() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.ACTIVE)
    running = make_run(
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": True,
        }
    )
    uow = FakeForgetUow(
        dataset=dataset, source=source, pipeline_runs=FakePipelineRunsRepo(running=running)
    )
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": False,
        },
    )
    result = StepResult(output={})

    with pytest.raises(PermanentPipelineStepError) as excinfo:
        await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]
    assert excinfo.value.code == FORGET_TARGET_CONFLICT_ERROR_CODE


@pytest.mark.asyncio
async def test_source_resumes_when_deleting_with_compatible_prior_intent_and_no_running_owner() -> (
    None
):
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETING)
    run_input = {
        "scope": "source",
        "dataset": "main",
        "source_id": str(source.id),
        "memory_only": False,
    }
    prior = make_run(run_input=run_input)
    uow = FakeForgetUow(
        dataset=dataset, source=source, pipeline_runs=FakePipelineRunsRepo(latest_source=prior)
    )
    context = make_context(dataset_id=dataset.id, source_id=source.id, run_input=run_input)
    result = StepResult(output={})

    await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert result.output["proceed"] is True
    assert result.output["documents_deactivated"] == 0  # RESUMED: no new mutation
    assert source.status == SourceStatus.DELETING  # left as-is, not re-mutated


@pytest.mark.asyncio
async def test_source_blocked_when_deleting_with_incompatible_prior_intent() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETING)
    prior = make_run(
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": True,
        }
    )
    uow = FakeForgetUow(
        dataset=dataset, source=source, pipeline_runs=FakePipelineRunsRepo(latest_source=prior)
    )
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": False,
        },
    )
    result = StepResult(output={})

    with pytest.raises(PermanentPipelineStepError) as excinfo:
        await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]
    assert excinfo.value.code == FORGET_TARGET_CONFLICT_ERROR_CODE


# ---------------------------------------------------------------------------
# AuthoritativeMutationStep -- DATASET scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dataset_fresh_mutation_sets_deleting_and_proceeds() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.ACTIVE)
    uow = FakeForgetUow(dataset=dataset, source=source)
    context = make_context(
        dataset_id=dataset.id,
        source_id=None,
        run_input={"scope": "dataset", "dataset": "main", "memory_only": False},
    )
    result = StepResult(output={})

    await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert dataset.status == DatasetStatus.DELETING
    assert source.status == SourceStatus.DELETING
    targets = result.output["targets"]
    assert targets[0]["proceed"] is True


@pytest.mark.asyncio
async def test_dataset_blocked_when_running_source_forget_targets_it() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.ACTIVE)
    running = make_run(
        run_input={
            "scope": "source",
            "dataset": "main",
            "source_id": str(source.id),
            "memory_only": False,
        }
    )
    uow = FakeForgetUow(
        dataset=dataset, source=source, pipeline_runs=FakePipelineRunsRepo(running=running)
    )
    context = make_context(
        dataset_id=dataset.id,
        source_id=None,
        run_input={"scope": "dataset", "dataset": "main", "memory_only": False},
    )
    result = StepResult(output={})

    with pytest.raises(PermanentPipelineStepError) as excinfo:
        await AuthoritativeMutationStep().persist(context, result, uow)  # type: ignore[arg-type]
    assert excinfo.value.code == FORGET_TARGET_CONFLICT_ERROR_CODE
    assert dataset.status == DatasetStatus.ACTIVE  # never mutated


# ---------------------------------------------------------------------------
# ProjectionConvergenceStep / StorageDeletionStep
# ---------------------------------------------------------------------------


class FakeGraphOutboxDrain:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def process_dataset(self, dataset_id: UUID) -> GraphOutboxDrainResult:
        self.calls.append(dataset_id)
        return GraphOutboxDrainResult(dataset_id=dataset_id, processed=3)


@pytest.mark.asyncio
async def test_projection_convergence_skips_when_not_proceeding() -> None:
    drain = FakeGraphOutboxDrain()
    resources = ForgetPipelineResources(settings=None, graph_outbox_drain=drain)  # type: ignore[arg-type]
    context = make_context(
        dataset_id=uuid4(),
        source_id=None,
        run_input={"scope": "source"},
        resources=resources,
        step_outputs={AUTHORITATIVE_MUTATION_STEP: {"scope": "source", "proceed": False}},
    )

    result = await ProjectionConvergenceStep().execute(context)

    assert result.output == {"graph_events_processed": 0}
    assert drain.calls == []


@pytest.mark.asyncio
async def test_projection_convergence_drains_the_mutated_dataset() -> None:
    drain = FakeGraphOutboxDrain()
    resources = ForgetPipelineResources(settings=None, graph_outbox_drain=drain)  # type: ignore[arg-type]
    dataset_id = uuid4()
    context = make_context(
        dataset_id=dataset_id,
        source_id=None,
        run_input={"scope": "source"},
        resources=resources,
        step_outputs={
            AUTHORITATIVE_MUTATION_STEP: {
                "scope": "source",
                "proceed": True,
                "dataset_id": str(dataset_id),
            }
        },
    )

    result = await ProjectionConvergenceStep().execute(context)

    assert result.output == {"graph_events_processed": 3}
    assert drain.calls == [dataset_id]


@pytest.mark.asyncio
async def test_storage_deletion_skips_entirely_for_memory_only() -> None:
    context = make_context(
        dataset_id=uuid4(),
        source_id=None,
        run_input={"scope": "source", "memory_only": True},
        step_outputs={AUTHORITATIVE_MUTATION_STEP: {"scope": "source", "proceed": True}},
    )

    result = await StorageDeletionStep().execute(context)

    assert result.output == {"sources": [], "deleted_now": 0, "already_absent": 0}


# ---------------------------------------------------------------------------
# FinalizeTargetStep / FinalizeResultStep
# ---------------------------------------------------------------------------


class FakeFinalizeSourcesRepo:
    def __init__(self, source: Source) -> None:
        self.source = source

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return self.source if self.source.id == source_id else None


class FakeFinalizeRunRepo:
    def __init__(self, run: PipelineRun) -> None:
        self.run = run

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        return self.run


class FakeFinalizeUow:
    def __init__(self, *, source: Source, run: PipelineRun) -> None:
        self.sources = FakeFinalizeSourcesRepo(source)
        self.pipeline_runs = FakeFinalizeRunRepo(run)


@pytest.mark.asyncio
async def test_finalize_target_sets_deleted_and_clears_storage_uri_for_full_source() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETING)
    run = make_run()
    uow = FakeFinalizeUow(source=source, run=run)
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={"scope": "source", "memory_only": False},
        step_outputs={
            AUTHORITATIVE_MUTATION_STEP: {
                "scope": "source",
                "proceed": True,
                "dataset_id": str(dataset.id),
                "source_id": str(source.id),
            },
            STORAGE_DELETION_STEP: {
                "sources": [{"source_id": str(source.id), "status": "deleted_now"}]
            },
        },
    )
    result = StepResult(output={})

    await FinalizeTargetStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert source.status == SourceStatus.DELETED
    assert source.storage_uri is None
    assert result.output["storage_deleted"] is True


@pytest.mark.asyncio
async def test_finalize_target_preserves_storage_uri_for_memory_only() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETING)
    run = make_run()
    uow = FakeFinalizeUow(source=source, run=run)
    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={"scope": "source", "memory_only": True},
        step_outputs={
            AUTHORITATIVE_MUTATION_STEP: {
                "scope": "source",
                "proceed": True,
                "dataset_id": str(dataset.id),
                "source_id": str(source.id),
            },
            STORAGE_DELETION_STEP: {"sources": []},
        },
    )
    result = StepResult(output={})

    await FinalizeTargetStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert source.status == SourceStatus.PENDING
    assert source.storage_uri is not None


@pytest.mark.asyncio
async def test_finalize_result_aggregates_source_scope() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id, status=SourceStatus.DELETED)
    source.storage_uri = None
    run = make_run()

    class Repo:
        async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
            return run

    class Uow:
        pipeline_runs = Repo()

    context = make_context(
        dataset_id=dataset.id,
        source_id=source.id,
        run_input={"scope": "source", "memory_only": False},
        run_id=run.id,
        step_outputs={
            AUTHORITATIVE_MUTATION_STEP: {
                "scope": "source",
                "proceed": True,
                "dataset_id": str(dataset.id),
                "source_id": str(source.id),
                "documents_deactivated": 2,
                "chunks_deactivated": 5,
                "summaries_deactivated": 1,
                "entities_deactivated": 0,
                "relations_deactivated": 0,
                "entity_mentions_unprojected": 3,
                "relation_evidence_unprojected": 1,
                "graph_events_enqueued": 4,
            },
            "projection_convergence": {"graph_events_processed": 4},
            STORAGE_DELETION_STEP: {"deleted_now": 1, "already_absent": 0},
            "finalize_target": {"source_status": "deleted", "storage_deleted": True},
        },
    )
    result = StepResult(output={})

    await FinalizeResultStep().persist(context, result, Uow())  # type: ignore[arg-type]

    persisted = run.metrics["forget_result"]
    assert persisted["documents_deactivated"] == 2
    assert persisted["graph_events_processed"] == 4
    assert persisted["storage_deleted"] is True
    assert persisted["source_status"] == "deleted"
    assert run.dataset_id == dataset.id
    assert run.source_id == source.id
