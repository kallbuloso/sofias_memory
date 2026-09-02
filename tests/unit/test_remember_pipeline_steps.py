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

from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sofias_memory.config import Settings
from sofias_memory.domain import PipelineType
from sofias_memory.infrastructure.storage import (
    FinalizeResult,
    SourceStorageConflictError,
    SourceStorageUnavailableError,
)
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError, RetryablePipelineStepError
from sofias_memory.pipelines.registry import StepResult
from sofias_memory.pipelines.steps.remember import (
    COGNIFY_STEP,
    PREPARE_AND_INGEST_STEP,
    REMEMBER_RESOURCES_RESOURCE,
    CognifyCompositionStep,
    FinalizeResultStep,
    FinalizeStorageStep,
    RememberPipelineResources,
    finalize_storage_input,
)
from sofias_memory.services.cognify import CognifyProcessOutcome
from sofias_memory.services.remember import (
    REMEMBER_RESULT_METRIC_KEY,
    final_storage_path,
    write_final_storage_bytes,
    write_ingress_bytes,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg, arg-type]


def make_context(
    *,
    tmp_path: Path,
    dataset_id: UUID | None,
    run_input: dict[str, object],
    run_id: UUID | None = None,
    cognify_service: object | None = None,
    step_outputs: dict[str, dict[str, object]] | None = None,
    settings: Settings | None = None,
    source_storage: object | None = None,
) -> PipelineContext:
    resources = RememberPipelineResources(
        settings=settings or make_settings(tmp_path),
        cognify_service=cognify_service,  # type: ignore[arg-type]
        source_storage=source_storage,  # type: ignore[arg-type]
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
                "byte_size": len(b"hello world"),
                "mime_type": "text/plain",
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
                "byte_size": len(b"hello world"),
                "mime_type": "text/plain",
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
                "byte_size": len(b"hello world"),
                "mime_type": "text/plain",
            }
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)


# ---------------------------------------------------------------------------
# FinalizeStorageStep -- STORAGE-004: router-based finalize (both backends)
# and B1 backend-switch recovery.
# ---------------------------------------------------------------------------


class _FakeS3Storage:
    """In-memory ``SourceObjectStorage``-shaped double standing in for the
    real S3 adapter -- proves ``FinalizeStorageStep`` drives any injected
    backend uniformly through the same ``deterministic_uri``/``verify``/
    ``finalize`` sequence, without depending on ``boto3``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.finalize_calls = 0
        self.verify_calls = 0

    def deterministic_uri(
        self, *, dataset_id: object, source_id: object, storage_extension: str
    ) -> str:
        return f"s3://fake-bucket/{dataset_id}/{source_id}{storage_extension}"

    async def finalize(
        self,
        *,
        dataset_id: object,
        source_id: object,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        self.finalize_calls += 1
        uri = self.deterministic_uri(
            dataset_id=dataset_id, source_id=source_id, storage_extension=storage_extension
        )
        existing = self.objects.get(uri)
        if existing is not None:
            if existing == original_bytes:
                return FinalizeResult(storage_uri=uri, already_present=True)
            raise SourceStorageConflictError(
                "Source storage target already holds different content."
            )
        self.objects[uri] = original_bytes
        return FinalizeResult(storage_uri=uri, already_present=False)

    async def read(
        self,
        *,
        dataset_id: object,
        source_id: object,
        storage_uri: str,
        expected_byte_size: int,
        expected_content_sha256: str,
        max_bytes: int,
    ) -> bytes:
        data = self.objects.get(storage_uri)
        if data is None:
            raise SourceStorageUnavailableError("Source storage is unavailable.")
        return data

    async def delete(
        self, *, dataset_id: object, source_id: object, storage_uri: str | None
    ) -> object:
        raise NotImplementedError("STORAGE-004 never calls delete()")

    async def verify(
        self,
        *,
        dataset_id: object,
        source_id: object,
        storage_uri: str,
        content_sha256: str,
    ) -> bool:
        self.verify_calls += 1
        data = self.objects.get(storage_uri)
        if data is None:
            return False
        return sha256(data).hexdigest() == content_sha256


class _UnavailableStorage:
    """Every call raises ``SourceStorageUnavailableError`` -- proves
    dependency-unavailable failures translate to a retryable pipeline
    error, never a permanent one."""

    def deterministic_uri(
        self, *, dataset_id: object, source_id: object, storage_extension: str
    ) -> str:
        return f"s3://fake-bucket/{dataset_id}/{source_id}{storage_extension}"

    async def finalize(self, **kwargs: object) -> FinalizeResult:
        raise SourceStorageUnavailableError("Source storage is unavailable.")

    async def read(self, **kwargs: object) -> bytes:
        raise SourceStorageUnavailableError("Source storage is unavailable.")

    async def delete(self, **kwargs: object) -> object:
        raise NotImplementedError

    async def verify(self, **kwargs: object) -> bool:
        raise SourceStorageUnavailableError("Source storage is unavailable.")


class FakeSource:
    def __init__(self, source_id: UUID) -> None:
        self.id = source_id
        self.storage_uri: str | None = None


class FakeSourcesRepo:
    def __init__(self, source: FakeSource) -> None:
        self.source = source

    async def get_by_id_for_update(self, source_id: UUID) -> FakeSource | None:
        return self.source if self.source.id == source_id else None


class FakeUowWithSources:
    def __init__(self, source: FakeSource) -> None:
        self.sources = FakeSourcesRepo(source)


def _s3_upstream(
    *, dataset_id: UUID, source_id: UUID, content: bytes, content_sha256: str | None = None
) -> dict[str, object]:
    return {
        "deduplicated": False,
        "dataset_id": str(dataset_id),
        "source_id": str(source_id),
        "storage_extension": ".txt",
        "content_sha256": content_sha256 or sha256(content).hexdigest(),
        "byte_size": len(content),
        "mime_type": "text/plain",
    }


@pytest.mark.asyncio
async def test_finalize_storage_writes_via_injected_storage_and_records_uri(tmp_path: Path) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content)
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "written"
    assert result.output["storage_written"] is True
    assert result.output["storage_uri"].startswith("s3://")
    assert fake.finalize_calls == 1
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_finalize_storage_replay_via_injected_storage_skips_write(tmp_path: Path) -> None:
    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    uri = fake.deterministic_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    fake.objects[uri] = content
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "already_present"
    assert result.output["storage_written"] is False
    assert result.output["storage_uri"] == uri
    # verify() alone answered the replay -- finalize() was never called.
    assert fake.finalize_calls == 0


@pytest.mark.asyncio
async def test_finalize_storage_conflict_via_injected_storage_fails_permanent(
    tmp_path: Path,
) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content)
    uri = fake.deterministic_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    fake.objects[uri] = b"different content"
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)
    # A failed attempt must never destroy the only recoverable local copy.
    assert ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_finalize_storage_unavailable_storage_is_retryable(tmp_path: Path) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content)
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        source_storage=_UnavailableStorage(),
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(RetryablePipelineStepError):
        await step.execute(context)
    # A retryable failure must never destroy the only recoverable local copy.
    assert ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_finalize_storage_persist_records_storage_uri_once(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        step_outputs={PREPARE_AND_INGEST_STEP: {"source_id": str(source_id)}},
    )
    result = StepResult(
        output={
            "storage_status": "written",
            "storage_written": True,
            "storage_uri": "s3://fake-bucket/x/y.txt",
        }
    )
    source = FakeSource(source_id)
    uow = FakeUowWithSources(source)
    step = FinalizeStorageStep()
    await step.persist(context, result, uow)  # type: ignore[arg-type]
    assert source.storage_uri == "s3://fake-bucket/x/y.txt"


@pytest.mark.asyncio
async def test_finalize_storage_persist_is_idempotent_when_already_set(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        step_outputs={PREPARE_AND_INGEST_STEP: {"source_id": str(source_id)}},
    )
    result = StepResult(
        output={
            "storage_status": "written",
            "storage_written": True,
            "storage_uri": "s3://new/uri.txt",
        }
    )
    source = FakeSource(source_id)
    source.storage_uri = "file:///already/committed.txt"
    uow = FakeUowWithSources(source)
    step = FinalizeStorageStep()
    await step.persist(context, result, uow)  # type: ignore[arg-type]
    assert source.storage_uri == "file:///already/committed.txt"


@pytest.mark.asyncio
async def test_finalize_storage_persist_skips_dedup(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        step_outputs={PREPARE_AND_INGEST_STEP: {"source_id": str(source_id), "deduplicated": True}},
    )
    result = StepResult(output={"storage_status": "skipped_dedup", "storage_written": False})
    source = FakeSource(source_id)
    uow = FakeUowWithSources(source)
    step = FinalizeStorageStep()
    await step.persist(context, result, uow)  # type: ignore[arg-type]
    assert source.storage_uri is None


def test_finalize_storage_input_includes_mime_type_and_byte_size() -> None:
    upstream = {
        "dataset_id": "d",
        "source_id": "s",
        "deduplicated": False,
        "storage_extension": ".txt",
        "content_sha256": "abc",
        "mime_type": "text/plain",
        "byte_size": 11,
    }
    derived = finalize_storage_input({}, {PREPARE_AND_INGEST_STEP: upstream})
    assert derived == {
        "dataset_id": "d",
        "source_id": "s",
        "deduplicated": False,
        "storage_extension": ".txt",
        "content_sha256": "abc",
        "mime_type": "text/plain",
        "byte_size": 11,
    }


# ---------------------------------------------------------------------------
# ADR-0011 B1: STORAGE_BACKEND=filesystem->s3 crash-window recovery.
# ---------------------------------------------------------------------------


def _b1_settings(tmp_path: Path) -> Settings:
    return make_settings(
        tmp_path, storage_backend="s3", storage_s3_bucket="b1-bucket", storage_s3_region="us-east-1"
    )


@pytest.mark.asyncio
async def test_b1_case1_ingress_present_recovers_normally_despite_s3_backend(
    tmp_path: Path,
) -> None:
    """Case 1: the durable ingress artifact is still there -- this is not
    actually a crash-recovery scenario at all, just an ordinary S3 write;
    the legacy-lookup path must never even be consulted."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=content)
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "written"
    assert fake.finalize_calls == 1


@pytest.mark.asyncio
async def test_b1_case2_ingress_absent_s3_target_already_matches(tmp_path: Path) -> None:
    """Case 2: ingress is gone, but the S3 target already holds the
    expected content (an earlier attempt's finalize landed before the
    crash) -- recovery needs no bytes at all, legacy or otherwise."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    uri = fake.deterministic_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    fake.objects[uri] = content
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "already_present"
    assert result.output["storage_uri"] == uri
    assert fake.finalize_calls == 0


@pytest.mark.asyncio
async def test_b1_case3_recovers_from_verified_legacy_filesystem_final(tmp_path: Path) -> None:
    """Case 3: ingress absent, S3 target absent -- recovers from the legacy
    filesystem final object an earlier filesystem-backend attempt left
    behind, verified byte-for-byte, and does NOT delete the legacy file."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=content,
    )
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    result = await step.execute(context)
    assert result.output["storage_status"] == "recovered_legacy"
    assert result.output["storage_written"] is True
    assert fake.finalize_calls == 1
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    # The redundant legacy copy is left in place -- cleanup is STORAGE-006's
    # job (ADR-0011 D9/D35), never this step's.
    assert legacy_path.read_bytes() == content


@pytest.mark.asyncio
async def test_b1_case4_nothing_recoverable_fails_closed(tmp_path: Path) -> None:
    """Case 4: ingress absent, S3 target absent, no legacy final either --
    unrecoverable, must fail closed with the same ingress-missing error as
    any other unrecoverable Remember attempt."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)
    assert fake.finalize_calls == 0


@pytest.mark.asyncio
async def test_b1_case5_legacy_present_with_wrong_identity_fails_closed_untouched(
    tmp_path: Path,
) -> None:
    """Case 5: a file exists at the exact deterministic legacy location but
    fails size/hash validation against this run's own persisted identity --
    never uploaded, overwritten, or deleted; treated as unusable and the
    attempt fails closed exactly as case 4."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"some other unrelated content",
    )
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)
    assert fake.finalize_calls == 0
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert legacy_path.read_bytes() == b"some other unrelated content"


@pytest.mark.asyncio
async def test_b1_never_triggers_on_filesystem_backend(tmp_path: Path) -> None:
    """B1 recovery is exclusively an S3-backend concern: on the filesystem
    backend, an absent target with absent ingress has no separate 'legacy'
    location to fall back to -- must fail closed exactly as before
    STORAGE-004, never attempt a legacy lookup."""

    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        step_outputs={
            PREPARE_AND_INGEST_STEP: _s3_upstream(
                dataset_id=dataset_id, source_id=source_id, content=content
            )
        },
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)


@pytest.mark.asyncio
async def test_b1_case3_unmappable_mime_type_fails_closed(tmp_path: Path) -> None:
    """An unmappable ``mime_type`` cannot even determine where the legacy
    object would live -- fails closed the same way as case 4, never a
    glob/search fallback."""

    fake = _FakeS3Storage()
    dataset_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    content = b"hello world"
    upstream = _s3_upstream(dataset_id=dataset_id, source_id=source_id, content=content)
    upstream["mime_type"] = "application/x-totally-unmapped"
    context = make_context(
        tmp_path=tmp_path,
        dataset_id=dataset_id,
        run_input={},
        run_id=run_id,
        settings=_b1_settings(tmp_path),
        source_storage=fake,
        step_outputs={PREPARE_AND_INGEST_STEP: upstream},
    )
    step = FinalizeStorageStep()
    with pytest.raises(PermanentPipelineStepError):
        await step.execute(context)
    assert fake.finalize_calls == 0


def test_fingerprint_payload_excludes_storage_backend(tmp_path: Path) -> None:
    """ADR-0011 D18: storage backend/config are physical, not semantic,
    configuration -- a bare STORAGE_BACKEND flip must never be observed by
    ``finalize_storage_input``'s drift-detection hash."""

    upstream = {
        "dataset_id": "d",
        "source_id": "s",
        "deduplicated": False,
        "storage_extension": ".txt",
        "content_sha256": "abc",
        "mime_type": "text/plain",
        "byte_size": 11,
    }
    derived = finalize_storage_input({}, {PREPARE_AND_INGEST_STEP: upstream})
    assert derived is not None
    assert "storage_backend" not in derived
    assert not any("s3" in str(key).lower() for key in derived)


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
