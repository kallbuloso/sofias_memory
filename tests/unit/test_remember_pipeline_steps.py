"""Unit tests for the Remember B5 pipeline steps (SM-513, ADR-0009 SS O).

Covers ``CognifyCompositionStep``'s mode dispatch (ingest no-op vs. full,
against a fake ``CognifySourceProcessor`` -- proving zero nested COGNIFY
run at the unit level), ``FinalizeStorageStep``'s dedup-skip/idempotent-
replay/content-conflict decision tree against a real temporary filesystem,
and ``FinalizeResultStep``'s aggregation against a fake pipeline_runs
repository. Real Source/Document dedup/version mutation correctness and
full end-to-end crash/resume behavior are proven against real PostgreSQL in
the integration suite (mirroring Forget's SM-512 documented split).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sofias_memory.config import Settings
from sofias_memory.domain import PipelineType
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.pipelines.registry import StepResult
from sofias_memory.pipelines.steps.remember import (
    COGNIFY_STEP,
    PREPARE_AND_INGEST_STEP,
    REMEMBER_RESOURCES_RESOURCE,
    CognifyCompositionStep,
    FinalizeResultStep,
    FinalizeStorageStep,
    RememberPipelineResources,
)
from sofias_memory.services.cognify import CognifyProcessOutcome
from sofias_memory.services.remember import (
    REMEMBER_RESULT_METRIC_KEY,
    write_final_storage_bytes,
    write_ingress_bytes,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


def make_context(
    *,
    tmp_path: Path,
    dataset_id: UUID | None,
    run_input: dict[str, object],
    run_id: UUID | None = None,
    cognify_service: object | None = None,
    step_outputs: dict[str, dict[str, object]] | None = None,
) -> PipelineContext:
    resources = RememberPipelineResources(
        settings=make_settings(tmp_path),
        cognify_service=cognify_service,  # type: ignore[arg-type]
    )
    return PipelineContext(
        run_id=run_id or uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=dataset_id,
        source_id=None,
        run_input=run_input,
        step_outputs=step_outputs or {},
        session_factory=None,  # type: ignore[arg-type] - unused by these steps' persist()
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )


class FakePipelineRunsRepo:
    def __init__(self, run: object) -> None:
        self.run = run

    async def get_by_id_for_update(self, run_id: UUID) -> object | None:
        return self.run if self.run.id == run_id else None  # type: ignore[attr-defined]


class FakeUow:
    def __init__(self, run: object) -> None:
        self.pipeline_runs = FakePipelineRunsRepo(run)


class FakeRun:
    def __init__(self, run_id: UUID) -> None:
        self.id = run_id
        self.metrics: dict[str, object] = {}


class FakePreparedBatch:
    def __init__(self, outcome: CognifyProcessOutcome) -> None:
        self._outcome = outcome

    def planned_outcome(self) -> CognifyProcessOutcome:
        return self._outcome


class FakeCognifyProcessor:
    def __init__(self, outcome: CognifyProcessOutcome) -> None:
        self.outcome = outcome
        self.prepare_calls: list[dict[str, object]] = []
        self.persist_calls: int = 0

    async def prepare_batch(
        self, *, dataset_id: UUID, source_ids: list[UUID] | None, rebuild: bool
    ):
        self.prepare_calls.append(
            {"dataset_id": dataset_id, "source_ids": source_ids, "rebuild": rebuild}
        )
        return FakePreparedBatch(self.outcome)

    async def persist_batch(self, uow: object, batch: object) -> CognifyProcessOutcome:
        del uow, batch
        self.persist_calls += 1
        return self.outcome


# ---------------------------------------------------------------------------
# CognifyCompositionStep -- zero nested COGNIFY run (SM-513 SS 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cognify_step_skips_for_ingest_mode(tmp_path: Path) -> None:
    processor = FakeCognifyProcessor(
        CognifyProcessOutcome(
            dataset_id=uuid4(),
            target_generation=0,
            rebuild=False,
            sources_processed=0,
            chunks=0,
            entities=0,
            relations=0,
        )
    )
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=uuid4(),
        run_input={"mode": "ingest"},
        cognify_service=processor,
    )
    step = CognifyCompositionStep()
    result = await step.execute(context)
    assert result.output["cognify_skipped"] is True
    assert result.output["chunks"] == 0
    assert not processor.prepare_calls


@pytest.mark.asyncio
async def test_cognify_step_full_mode_calls_prepare_and_persist_batch(tmp_path: Path) -> None:
    dataset_id = uuid4()
    source_id = uuid4()
    outcome = CognifyProcessOutcome(
        dataset_id=dataset_id,
        target_generation=0,
        rebuild=False,
        sources_processed=1,
        chunks=2,
        entities=3,
        relations=1,
    )
    processor = FakeCognifyProcessor(outcome)
    run_id = uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={"mode": "full"},
        run_id=run_id,
        cognify_service=processor,
        step_outputs={PREPARE_AND_INGEST_STEP: {"source_id": str(source_id)}},
    )
    step = CognifyCompositionStep()
    result = await step.execute(context)
    assert result.output["cognify_skipped"] is False
    assert processor.prepare_calls == [
        {"dataset_id": dataset_id, "source_ids": [source_id], "rebuild": False}
    ]

    persist_result = StepResult(output=dict(result.output))
    await step.persist(context, persist_result, uow=object())  # type: ignore[arg-type]
    assert processor.persist_calls == 1
    assert persist_result.output["chunks"] == 2
    assert persist_result.output["entities"] == 3
    assert persist_result.output["relations"] == 1


@pytest.mark.asyncio
async def test_cognify_step_full_mode_never_touches_run_metrics(tmp_path: Path) -> None:
    """Proves the composition step writes no COGNIFY-specific artifact on
    this REMEMBER run -- only FinalizeResultStep aggregates a result."""

    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    outcome = CognifyProcessOutcome(
        dataset_id=dataset_id,
        target_generation=0,
        rebuild=False,
        sources_processed=1,
        chunks=1,
        entities=1,
        relations=0,
    )
    processor = FakeCognifyProcessor(outcome)
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={"mode": "full"},
        run_id=run_id,
        cognify_service=processor,
        step_outputs={PREPARE_AND_INGEST_STEP: {"source_id": str(source_id)}},
    )
    step = CognifyCompositionStep()
    result = await step.execute(context)
    fake_run = FakeRun(run_id)
    await step.persist(context, result, uow=FakeUow(fake_run))  # type: ignore[arg-type]
    assert fake_run.metrics == {}


# ---------------------------------------------------------------------------
# FinalizeStorageStep -- dedup skip / idempotent replay / content conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_storage_skips_entirely_when_deduplicated(tmp_path: Path) -> None:
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=uuid4(),
        run_input={},
        step_outputs={PREPARE_AND_INGEST_STEP: {"deduplicated": True}},
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output == {"storage_status": "skipped_dedup", "storage_written": False}


@pytest.mark.asyncio
async def test_finalize_storage_writes_from_ingress_then_deletes_it(tmp_path: Path) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"hello world")
    from hashlib import sha256

    content_sha256 = sha256(b"hello world").hexdigest()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: {
                "deduplicated": False,
                "dataset_id": str(dataset_id),
                "source_id": str(source_id),
                "storage_extension": ".txt",
                "content_sha256": content_sha256,
            }
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "written"
    assert result.output["storage_written"] is True
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_finalize_storage_replay_recognizes_already_present_content(tmp_path: Path) -> None:
    from hashlib import sha256

    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content_sha256 = sha256(b"hello world").hexdigest()
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"hello world",
    )
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: {
                "deduplicated": False,
                "dataset_id": str(dataset_id),
                "source_id": str(source_id),
                "storage_extension": ".txt",
                "content_sha256": content_sha256,
            }
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "already_present"
    assert result.output["storage_written"] is False


@pytest.mark.asyncio
async def test_finalize_storage_wrong_content_at_final_path_fails_safe(tmp_path: Path) -> None:
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"different content",
    )
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"hello world")
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: {
                "deduplicated": False,
                "dataset_id": str(dataset_id),
                "source_id": str(source_id),
                "storage_extension": ".txt",
                "content_sha256": "0" * 64,
            }
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)


# ---------------------------------------------------------------------------
# FinalizeResultStep -- pure aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_result_aggregates_ingest_and_cognify_outputs(tmp_path: Path) -> None:
    dataset_id, source_id, document_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: {
                "dataset_id": str(dataset_id),
                "source_id": str(source_id),
                "document_id": str(document_id),
                "content_sha256": "a" * 64,
                "deduplicated": False,
            },
            COGNIFY_STEP: {"chunks": 2, "entities": 3, "relations": 1},
        },
    )
    step = FinalizeResultStep()
    result = StepResult(output={})
    fake_run = FakeRun(run_id)
    await step.persist(context, result, uow=FakeUow(fake_run))  # type: ignore[arg-type]
    persisted = fake_run.metrics[REMEMBER_RESULT_METRIC_KEY]
    assert persisted["dataset_id"] == str(dataset_id)
    assert persisted["source_id"] == str(source_id)
    assert persisted["document_id"] == str(document_id)
    assert persisted["content_hash"] == "a" * 64
    assert persisted["chunks"] == 2
    assert persisted["entities"] == 3
    assert persisted["relations"] == 1
    assert persisted["deduplicated"] is False
    assert result.output == persisted


@pytest.mark.asyncio
async def test_finalize_result_defaults_cognify_counters_to_zero_for_ingest(tmp_path: Path) -> None:
    dataset_id, source_id, document_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: {
                "dataset_id": str(dataset_id),
                "source_id": str(source_id),
                "document_id": str(document_id),
                "content_sha256": "b" * 64,
                "deduplicated": True,
            },
            COGNIFY_STEP: {"cognify_skipped": True, "chunks": 0, "entities": 0, "relations": 0},
        },
    )
    step = FinalizeResultStep()
    result = StepResult(output={})
    fake_run = FakeRun(run_id)
    await step.persist(context, result, uow=FakeUow(fake_run))  # type: ignore[arg-type]
    persisted = fake_run.metrics[REMEMBER_RESULT_METRIC_KEY]
    assert persisted["chunks"] == 0
    assert persisted["deduplicated"] is True
