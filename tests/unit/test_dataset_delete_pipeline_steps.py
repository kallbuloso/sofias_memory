"""Unit tests for the Dataset administrative delete pipeline steps
(SM-515, ADR-0010 D9, ADR-0011 D37-D42/STORAGE-005).

Covers ``DeleteStorageStep``/``FinalizeTombstoneStep``'s four-outcome
``StorageDeleteResult`` consumption: recognized storage failures become
``UNRESOLVED`` (never a step failure); a missing per-Source storage result
is an internal invariant failure; the tombstone finalizer clears
``storage_uri`` for ``DELETED_NOW``/``ALREADY_ABSENT``/``NOT_REQUESTED`` and
preserves it for ``UNRESOLVED``. Real DB-backed deletion/round-trip
behavior is proven in the integration suite (mirrors Forget's own
documented unit/integration split, SM-512 self-audit).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sofias_memory.domain import DatasetStatus, SourceStatus
from sofias_memory.infrastructure.postgres.models import Dataset, PipelineRun, Source
from sofias_memory.infrastructure.storage import (
    InvalidSourceStorageUriError,
    SourceStorageConflictError,
    SourceStoragePathError,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
    UnsupportedStorageBackendError,
)
from sofias_memory.pipelines.errors import PermanentPipelineStepError
from sofias_memory.pipelines.registry import StepResult
from sofias_memory.pipelines.steps.dataset_delete import (
    CONVERGE_PROJECTION_STEP,
    DEACTIVATE_AUTHORITATIVE_STEP,
    DELETE_STORAGE_STEP,
    FinalizeTombstoneStep,
    _delete_source_storage_result,
    _storage_status_by_source,
    _storage_status_counts,
)


class _FakeDeletingStorage:
    def __init__(
        self, *, result: StorageDeleteResult | None = None, raises: Exception | None = None
    ):
        self._result = result
        self._raises = raises

    async def delete(self, *, dataset_id: object, source_id: object, storage_uri: str | None):
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


@pytest.mark.parametrize(
    "exc",
    [
        # 1. recognized dependency/unavailable delete condition -> UNRESOLVED.
        SourceStorageUnavailableError("unavailable"),
        # 2. invalid/unresolvable storage locator (D38) -> UNRESOLVED.
        InvalidSourceStorageUriError("invalid uri"),
        # lost/unusable S3 configuration (D41) -> UNRESOLVED.
        UnsupportedStorageBackendError("S3 not configured"),
    ],
)
@pytest.mark.asyncio
async def test_delete_source_storage_result_recognized_errors_become_unresolved(
    exc: Exception,
) -> None:
    storage = _FakeDeletingStorage(raises=exc)
    result = await _delete_source_storage_result(
        storage, dataset_id=uuid4(), source_id=uuid4(), storage_uri="s3://bucket/key"
    )
    assert result.status is StorageDeleteStatus.UNRESOLVED


@pytest.mark.asyncio
async def test_delete_source_storage_result_conflict_is_genuine_failure_not_unresolved() -> None:
    """3. A deterministic content-identity conflict must propagate as a
    genuine failure -- never silently absorbed into UNRESOLVED."""

    storage = _FakeDeletingStorage(raises=SourceStorageConflictError("conflict"))
    with pytest.raises(SourceStorageConflictError):
        await _delete_source_storage_result(
            storage, dataset_id=uuid4(), source_id=uuid4(), storage_uri="s3://bucket/key"
        )


@pytest.mark.asyncio
async def test_delete_source_storage_result_path_error_is_genuine_failure_not_unresolved() -> None:
    """4. An unclassified/invariant-defect SourceStorageError subclass must
    also propagate rather than being absorbed into UNRESOLVED."""

    storage = _FakeDeletingStorage(raises=SourceStoragePathError("path escapes root"))
    with pytest.raises(SourceStoragePathError):
        await _delete_source_storage_result(
            storage, dataset_id=uuid4(), source_id=uuid4(), storage_uri="file:///x"
        )


@pytest.mark.asyncio
async def test_delete_source_storage_result_unexpected_exception_propagates() -> None:
    """5. TypeError/unrelated defect remains a genuine failure."""

    storage = _FakeDeletingStorage(raises=TypeError("programming defect"))
    with pytest.raises(TypeError):
        await _delete_source_storage_result(
            storage, dataset_id=uuid4(), source_id=uuid4(), storage_uri="file:///x"
        )


def test_storage_status_counts_tallies_each_outcome() -> None:
    entries = [
        {"source_id": "a", "status": "deleted_now"},
        {"source_id": "b", "status": "unresolved"},
    ]
    assert _storage_status_counts(entries) == {
        "deleted_now": 1,
        "already_absent": 0,
        "unresolved": 1,
        "not_requested": 0,
    }


def test_storage_status_by_source_rejects_duplicates() -> None:
    source_id = uuid4()
    storage_output = {
        "sources": [
            {"source_id": str(source_id), "status": "deleted_now"},
            {"source_id": str(source_id), "status": "deleted_now"},
        ]
    }
    with pytest.raises(PermanentPipelineStepError):
        _storage_status_by_source(storage_output)


def make_dataset(*, status: DatasetStatus = DatasetStatus.DELETING) -> Dataset:
    return Dataset(
        id=uuid4(),
        name="main",
        slug="main",
        description=None,
        status=status,
        active_generation=0,
    )


def make_source(*, dataset_id: UUID, status: SourceStatus = SourceStatus.DELETING) -> Source:
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

    async def get_by_id_for_update(self, dataset_id: UUID) -> Dataset | None:
        return self.dataset if self.dataset.id == dataset_id else None


class FakeSourcesRepo:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources

    async def list_for_dataset_for_update(self, dataset_id: UUID) -> list[Source]:
        return [s for s in self.sources if s.dataset_id == dataset_id]


class FakeRunsRepo:
    def __init__(self, run: PipelineRun) -> None:
        self.run = run

    async def get_by_id_for_update(self, run_id: UUID) -> PipelineRun | None:
        return self.run if self.run.id == run_id else None


class FakeTombstoneUow:
    def __init__(self, *, dataset: Dataset, sources: list[Source], run: PipelineRun) -> None:
        self.datasets = FakeDatasetsRepo(dataset)
        self.sources = FakeSourcesRepo(sources)
        self.pipeline_runs = FakeRunsRepo(run)


def make_run(run_id: UUID) -> PipelineRun:
    from datetime import UTC, datetime

    from sofias_memory.domain import PipelineRunStatus, PipelineType

    return PipelineRun(
        id=run_id,
        pipeline_type=PipelineType.DATASET_DELETE,
        dataset_id=None,
        source_id=None,
        status=PipelineRunStatus.RUNNING,
        idempotency_key=None,
        payload_hash="x",
        input={},
        progress=0.0,
        current_step="finalize_tombstone",
        attempt=1,
        worker_id="w",
        heartbeat_at=None,
        config_fingerprint="cf",
        error_code=None,
        error_message=None,
        metrics={},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=None,
    )


def make_context(*, dataset_id: UUID, run_id: UUID, step_outputs: dict[str, dict[str, object]]):
    from sofias_memory.domain import PipelineType
    from sofias_memory.pipelines.context import PipelineContext

    return PipelineContext(
        run_id=run_id,
        pipeline_type=PipelineType.DATASET_DELETE,
        dataset_id=dataset_id,
        source_id=None,
        run_input={},
        step_outputs=step_outputs,
        session_factory=None,  # type: ignore[arg-type]
        resources={},
    )


@pytest.mark.asyncio
async def test_finalize_tombstone_mixed_outcomes_clear_and_preserve_correctly() -> None:
    dataset = make_dataset()
    deleted_now = make_source(dataset_id=dataset.id)
    already_absent = make_source(dataset_id=dataset.id)
    unresolved = make_source(dataset_id=dataset.id)
    not_requested = make_source(dataset_id=dataset.id)
    not_requested.storage_uri = None
    sources = [deleted_now, already_absent, unresolved, not_requested]
    run = make_run(uuid4())
    uow = FakeTombstoneUow(dataset=dataset, sources=sources, run=run)

    context = make_context(
        dataset_id=dataset.id,
        run_id=run.id,
        step_outputs={
            DEACTIVATE_AUTHORITATIVE_STEP: {"documents_deactivated": 0},
            CONVERGE_PROJECTION_STEP: {"graph_events_processed": 0},
            DELETE_STORAGE_STEP: {
                "sources": [
                    {"source_id": str(deleted_now.id), "status": "deleted_now"},
                    {"source_id": str(already_absent.id), "status": "already_absent"},
                    {"source_id": str(unresolved.id), "status": "unresolved"},
                    {"source_id": str(not_requested.id), "status": "not_requested"},
                ],
                "deleted_now": 1,
                "already_absent": 1,
                "unresolved": 1,
                "not_requested": 1,
            },
        },
    )
    result = StepResult(output={})

    await FinalizeTombstoneStep().persist(context, result, uow)  # type: ignore[arg-type]

    assert deleted_now.storage_uri is None
    assert already_absent.storage_uri is None
    assert unresolved.storage_uri is not None
    assert not_requested.storage_uri is None
    assert all(s.status == SourceStatus.DELETED for s in sources)
    assert dataset.status == DatasetStatus.DELETED
    persisted = run.metrics["dataset_delete_result"]
    assert persisted["storage_deleted"] == 1
    assert persisted["storage_already_absent"] == 1
    assert persisted["storage_unresolved"] == 1
    assert persisted["storage_not_requested"] == 1
    assert persisted["storage_cleanup_complete"] is False


@pytest.mark.asyncio
async def test_finalize_tombstone_missing_result_is_invariant_failure() -> None:
    dataset = make_dataset()
    covered = make_source(dataset_id=dataset.id)
    uncovered = make_source(dataset_id=dataset.id)
    sources = [covered, uncovered]
    run = make_run(uuid4())
    uow = FakeTombstoneUow(dataset=dataset, sources=sources, run=run)

    context = make_context(
        dataset_id=dataset.id,
        run_id=run.id,
        step_outputs={
            DEACTIVATE_AUTHORITATIVE_STEP: {},
            CONVERGE_PROJECTION_STEP: {"graph_events_processed": 0},
            DELETE_STORAGE_STEP: {
                "sources": [{"source_id": str(covered.id), "status": "deleted_now"}],
                "deleted_now": 1,
            },
        },
    )
    result = StepResult(output={})

    with pytest.raises(PermanentPipelineStepError):
        await FinalizeTombstoneStep().persist(context, result, uow)  # type: ignore[arg-type]
    # The Source with no explicit storage result is never silently
    # finalized; the whole persist() call raises before the Dataset itself
    # (and, in real usage, the entire PostgreSQL transaction) can commit.
    assert uncovered.status == SourceStatus.DELETING
    assert dataset.status == DatasetStatus.DELETING


@pytest.mark.asyncio
async def test_finalize_tombstone_cleanup_complete_true_when_fully_resolved() -> None:
    dataset = make_dataset()
    source = make_source(dataset_id=dataset.id)
    run = make_run(uuid4())
    uow = FakeTombstoneUow(dataset=dataset, sources=[source], run=run)

    context = make_context(
        dataset_id=dataset.id,
        run_id=run.id,
        step_outputs={
            DEACTIVATE_AUTHORITATIVE_STEP: {},
            CONVERGE_PROJECTION_STEP: {"graph_events_processed": 0},
            DELETE_STORAGE_STEP: {
                "sources": [{"source_id": str(source.id), "status": "deleted_now"}],
                "deleted_now": 1,
                "already_absent": 0,
                "unresolved": 0,
                "not_requested": 0,
            },
        },
    )
    result = StepResult(output={})

    await FinalizeTombstoneStep().persist(context, result, uow)  # type: ignore[arg-type]

    persisted = run.metrics["dataset_delete_result"]
    assert persisted["storage_cleanup_complete"] is True


def test_dataset_delete_module_never_catches_source_storage_error_base_class() -> None:
    """7. STORAGE-005 exception-classification audit: never catch the
    ``SourceStorageError`` base class -- only the specific recognized-
    operational subclasses."""

    import ast
    from pathlib import Path

    from sofias_memory.pipelines.steps import dataset_delete

    source = Path(dataset_delete.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            names = (
                {node.type.id}
                if isinstance(node.type, ast.Name)
                else {elt.id for elt in getattr(node.type, "elts", []) if isinstance(elt, ast.Name)}
            )
            assert "SourceStorageError" not in names


def test_dataset_delete_module_never_catches_bare_exception() -> None:
    """ADR-0011 D37: recognized storage conditions must be classified via
    ``SourceStorageError``, never a blanket ``except Exception`` that would
    fabricate ``UNRESOLVED`` for a genuine programming defect."""

    import ast
    from pathlib import Path

    from sofias_memory.pipelines.steps import dataset_delete

    source = Path(dataset_delete.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            name = getattr(node.type, "id", None)
            assert name not in {"Exception", "BaseException"}
