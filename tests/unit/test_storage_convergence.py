"""Unit tests for the ADR-0011 STORAGE-006 storage convergence engine.

Scope: prove ``StorageConvergenceService``'s own decision logic --
classification (D34/D40), the D8 migration algorithm's non-DB steps, CAS
outcome consumption, D9/D35 post-repoint local cleanup, and filesystem-mode
no-op behavior -- against fakes for the S3 adapter and the two genuinely
DB-bound gateways (CAS repoint, Case B lineage proof). The two new
repository methods this slice adds
(``SourceRepository.list_all_for_storage_convergence``,
``PipelineRunRepository.find_compatible_destructive_lineage``) are, like
every other SQLAlchemy-statement-level behavior in this codebase, proven
correct in the integration suite -- not duplicated here.
"""

from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.storage import (
    FinalizeResult,
    SourceStorageConflictError,
    SourceStorageUnavailableError,
)
from sofias_memory.infrastructure.storage.filesystem import (
    final_storage_path,
    write_final_storage_bytes,
)
from sofias_memory.services import storage_convergence as sc

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
CONTENT = b"hello world"
CONTENT_SHA256 = sha256(CONTENT).hexdigest()


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "storage_backend": "s3",
        "storage_s3_bucket": "b",
        "storage_s3_region": "us-east-1",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg, arg-type]


def make_snapshot(**overrides: object) -> sc.SourceSnapshot:
    values: dict[str, object] = {
        "id": uuid4(),
        "dataset_id": uuid4(),
        "status": SourceStatus.ACTIVE,
        "storage_uri": None,
        "mime_type": "text/plain",
        "byte_size": len(CONTENT),
        "content_sha256": CONTENT_SHA256,
    }
    values.update(overrides)
    return sc.SourceSnapshot(**values)  # type: ignore[arg-type]


class _FakeStorage:
    """Minimal ``SourceObjectStorage`` double covering only ``finalize``/
    ``verify`` -- everything :class:`StorageConvergenceService` actually
    calls (it never calls ``read``/``delete``/``deterministic_uri``)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.finalize_calls = 0
        self.verify_calls = 0
        self._finalize_raises: Exception | None = None
        self._verify_result: bool | None = None
        self._verify_raises: Exception | None = None

    def target_uri(self, *, dataset_id: UUID, source_id: UUID, storage_extension: str) -> str:
        return f"s3://fake-bucket/{dataset_id}/{source_id}{storage_extension}"

    async def finalize(
        self, *, dataset_id: UUID, source_id: UUID, storage_extension: str, original_bytes: bytes
    ) -> FinalizeResult:
        self.finalize_calls += 1
        if self._finalize_raises is not None:
            raise self._finalize_raises
        uri = self.target_uri(
            dataset_id=dataset_id, source_id=source_id, storage_extension=storage_extension
        )
        already_present = uri in self.objects
        if not already_present:
            self.objects[uri] = original_bytes
        return FinalizeResult(storage_uri=uri, already_present=already_present)

    async def verify(
        self, *, dataset_id: UUID, source_id: UUID, storage_uri: str, content_sha256: str
    ) -> bool:
        self.verify_calls += 1
        if self._verify_raises is not None:
            raise self._verify_raises
        if self._verify_result is not None:
            return self._verify_result
        data = self.objects.get(storage_uri)
        if data is None:
            return False
        return sha256(data).hexdigest() == content_sha256

    async def read(self, **kwargs: object) -> bytes:
        raise NotImplementedError

    async def delete(self, **kwargs: object) -> object:
        raise NotImplementedError

    def deterministic_uri(self, **kwargs: object) -> str:
        raise NotImplementedError


def _write_legacy(
    tmp_path: Path, *, dataset_id: UUID, source_id: UUID, content: bytes = CONTENT
) -> Path:
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=content,
    )
    return final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )


def _service(
    tmp_path: Path,
    *,
    snapshots: list[sc.SourceSnapshot],
    storage: _FakeStorage | None = None,
    settings: Settings | None = None,
    lineage: sc.CaseBLineage | None = None,
) -> tuple[sc.StorageConvergenceService, _FakeStorage]:
    fake_storage = storage or _FakeStorage()

    async def list_sources() -> list[sc.SourceSnapshot]:
        return snapshots

    async def lineage_lookup(dataset_id: UUID, source_id: UUID) -> sc.CaseBLineage | None:
        return lineage

    async def cas_repoint(snapshot: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        # A trivial always-commits fake: these tests exercise migration/
        # cleanup/classification logic, not CAS mechanics itself (covered
        # separately below).
        return sc.CasOutcome.COMMITTED

    service = sc.StorageConvergenceService(
        settings or make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type] - never touched (all gateways overridden)
        source_storage=fake_storage,
        list_sources=list_sources,
        lineage_lookup=lineage_lookup,
        cas_repoint=cas_repoint,
    )
    return service, fake_storage


# ---------------------------------------------------------------------------
# Classification (test items 1-12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [SourceStatus.PENDING, SourceStatus.PROCESSING, SourceStatus.ACTIVE, SourceStatus.FAILED],
)
async def test_case_a_live_file_present_is_migrated(tmp_path: Path, status: SourceStatus) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = _write_legacy(tmp_path, dataset_id=dataset_id, source_id=source_id)
    snapshot = make_snapshot(
        id=source_id, dataset_id=dataset_id, status=status, storage_uri=legacy_path.as_uri()
    )
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.candidates_examined == 1
    assert result.migrated == 1
    assert storage.finalize_calls == 1


@pytest.mark.asyncio
async def test_case_a_missing_file_is_integrity_failure(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    file_uri = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    ).as_uri()
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=file_uri)
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.migrated == 0
    assert len(result.integrity_failures) == 1
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.LOCAL_OBJECT_MISSING
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_live_null_storage_uri_is_remember_owned(tmp_path: Path) -> None:
    snapshot = make_snapshot(status=SourceStatus.PENDING, storage_uri=None)
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.remember_owned_null == 1
    assert result.candidates_examined == 0
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_deleting_with_file_present_is_not_migration_owned(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = _write_legacy(tmp_path, dataset_id=dataset_id, source_id=source_id)
    snapshot = make_snapshot(
        id=source_id,
        dataset_id=dataset_id,
        status=SourceStatus.DELETING,
        storage_uri=legacy_path.as_uri(),
    )
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.skipped_deleting_present == 1
    assert result.candidates_examined == 0
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_deleting_missing_with_lineage_is_case_b(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    file_uri = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    ).as_uri()
    snapshot = make_snapshot(
        id=source_id, dataset_id=dataset_id, status=SourceStatus.DELETING, storage_uri=file_uri
    )
    run_id = uuid4()
    lineage = sc.CaseBLineage(
        source_id=source_id,
        dataset_id=dataset_id,
        pipeline_run_id=run_id,
        pipeline_type=PipelineType.FORGET,
        pipeline_run_status=PipelineRunStatus.SUCCEEDED,
    )
    service, storage = _service(tmp_path, snapshots=[snapshot], lineage=lineage)
    result = await service.converge()
    assert len(result.recovery_owned_case_b) == 1
    assert result.recovery_owned_case_b[0].pipeline_run_id == run_id
    assert result.recovery_owned_case_b[0].is_terminal is True
    assert not result.integrity_failures
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_deleting_missing_without_lineage_is_case_d(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    file_uri = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    ).as_uri()
    snapshot = make_snapshot(
        id=source_id, dataset_id=dataset_id, status=SourceStatus.DELETING, storage_uri=file_uri
    )
    service, storage = _service(tmp_path, snapshots=[snapshot], lineage=None)
    result = await service.converge()
    assert not result.recovery_owned_case_b
    assert len(result.integrity_failures) == 1
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.CASE_D_NO_PROVEN_LINEAGE
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage_uri_factory",
    [
        lambda: None,
        lambda: "file:///data/x/original.txt",
        lambda: "s3://bucket/v1/sources/x/y/original.txt",
    ],
)
async def test_deleted_is_case_c_never_inspected(tmp_path: Path, storage_uri_factory) -> None:
    snapshot = make_snapshot(status=SourceStatus.DELETED, storage_uri=storage_uri_factory())
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.skipped_deleted == 1
    assert not result.integrity_failures
    assert storage.finalize_calls == 0
    assert storage.verify_calls == 0


# ---------------------------------------------------------------------------
# Migration normal path (test items 20-25)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_target_absent_uploads_verifies_cas_cleans_up(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.migrated == 1
    assert result.local_duplicates_cleaned == 1
    assert not legacy_path.exists()
    assert storage.finalize_calls == 1
    # Once for D8 step G's strong pre-CAS verification, once again for the
    # D35 step 2 re-confirmation immediately before local cleanup.
    assert storage.verify_calls == 2


@pytest.mark.asyncio
async def test_migration_target_already_matching_reuses_idempotently(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.migrated == 1  # CAS still commits (storage_uri repoint is new)
    assert result.local_duplicates_cleaned == 1


@pytest.mark.asyncio
async def test_migration_target_conflicts_fails_closed_no_cas(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    storage = _FakeStorage()
    storage._finalize_raises = SourceStorageConflictError("conflict")
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.migrated == 0
    assert len(result.integrity_failures) == 1
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.S3_TARGET_CONFLICT
    assert legacy_path.exists()  # local source retained


@pytest.mark.asyncio
async def test_migration_s3_unavailable_fails_closed(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    storage = _FakeStorage()
    storage._finalize_raises = SourceStorageUnavailableError("unavailable")
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.migrated == 0
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.S3_UNAVAILABLE
    assert legacy_path.exists()


@pytest.mark.asyncio
async def test_migration_local_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    same_length_but_different = b"HELLO WORLD"  # same length as CONTENT, different bytes/hash
    assert len(same_length_but_different) == len(CONTENT)
    legacy_path.write_bytes(same_length_but_different)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.migrated == 0
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.HASH_MISMATCH
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_migration_local_byte_size_mismatch_fails_closed(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(
        id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri(), byte_size=999
    )
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.SIZE_MISMATCH
    assert storage.finalize_calls == 0


@pytest.mark.asyncio
async def test_migration_unmappable_mime_type_fails_closed_no_search(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    # Deliberately do not create ANY file -- an unmappable mime_type must
    # fail closed before ever attempting to locate a legacy path, and must
    # never glob/search for one.
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".bin"
    )
    snapshot = make_snapshot(
        id=source_id,
        dataset_id=dataset_id,
        storage_uri=legacy_path.as_uri(),
        mime_type="application/x-totally-unmapped",
    )
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.migrated == 0
    assert storage.finalize_calls == 0
    # The local path IS resolvable via source_storage_path (it's a
    # legitimate file:// URI) but nothing exists there yet, so this
    # surfaces as LOCAL_OBJECT_MISSING before the mime_type is even
    # consulted -- proving no directory scan ever substitutes for it.
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.LOCAL_OBJECT_MISSING


# ---------------------------------------------------------------------------
# CAS outcome consumption (test items 26-30)
# ---------------------------------------------------------------------------


def _migratable_snapshot(tmp_path: Path) -> tuple[sc.SourceSnapshot, Path]:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    return make_snapshot(
        id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri()
    ), legacy_path


@pytest.mark.asyncio
async def test_cas_committed_increments_migrated_and_cleans_up(tmp_path: Path) -> None:
    snapshot, legacy_path = _migratable_snapshot(tmp_path)
    calls: list[tuple[sc.SourceSnapshot, str]] = []

    async def cas_repoint(snap: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        calls.append((snap, new_uri))
        return sc.CasOutcome.COMMITTED

    fake_storage = _FakeStorage()
    service = sc.StorageConvergenceService(
        make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type]
        source_storage=fake_storage,
        list_sources=_const_sources([snapshot]),
        cas_repoint=cas_repoint,
    )
    result = await service.converge()
    assert result.migrated == 1
    assert result.local_duplicates_cleaned == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cas_already_converged_counts_separately_and_still_cleans_up(tmp_path: Path) -> None:
    snapshot, legacy_path = _migratable_snapshot(tmp_path)

    async def cas_repoint(snap: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        return sc.CasOutcome.ALREADY_CONVERGED

    fake_storage = _FakeStorage()
    service = sc.StorageConvergenceService(
        make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type]
        source_storage=fake_storage,
        list_sources=_const_sources([snapshot]),
        cas_repoint=cas_repoint,
    )
    result = await service.converge()
    assert result.already_converged == 1
    assert result.migrated == 0
    assert result.local_duplicates_cleaned == 1  # cleanup still runs


@pytest.mark.asyncio
async def test_cas_owned_elsewhere_never_repoints_never_counted_as_migrated(tmp_path: Path) -> None:
    """Benign CAS ownership/status loss (e.g. a live D34 Case A Source
    reclassified to another still-migration-eligible status, such as
    ACTIVE -> FAILED, while S3 I/O was in flight) -- ordinary, expected CAS
    contention under D10/D43: never repointed, never counted as migrated,
    never touched locally, and never surfaced as an integrity failure. This
    is the genuinely-benign outcome `_cas_repoint` reports as
    `OWNED_ELSEWHERE`; the distinct, D43-invariant-violation case (an
    observed `DELETING` transition) is `D43_INVARIANT_VIOLATION`, covered
    separately below -- the two must never share handling."""

    snapshot, legacy_path = _migratable_snapshot(tmp_path)

    async def cas_repoint(snap: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        return sc.CasOutcome.OWNED_ELSEWHERE

    fake_storage = _FakeStorage()
    service = sc.StorageConvergenceService(
        make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type]
        source_storage=fake_storage,
        list_sources=_const_sources([snapshot]),
        cas_repoint=cas_repoint,
    )
    result = await service.converge()
    assert result.migrated == 0
    assert result.already_converged == 0
    assert not result.integrity_failures
    assert result.local_duplicates_cleaned == 0  # never touches local copy either
    assert legacy_path.exists()


@pytest.mark.asyncio
async def test_cas_incompatible_state_fails_closed(tmp_path: Path) -> None:
    snapshot, legacy_path = _migratable_snapshot(tmp_path)

    async def cas_repoint(snap: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        return sc.CasOutcome.INCOMPATIBLE

    fake_storage = _FakeStorage()
    service = sc.StorageConvergenceService(
        make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type]
        source_storage=fake_storage,
        list_sources=_const_sources([snapshot]),
        cas_repoint=cas_repoint,
    )
    result = await service.converge()
    assert result.migrated == 0
    assert len(result.integrity_failures) == 1
    assert result.integrity_failures[0].reason is sc.IntegrityFailureReason.CAS_INCOMPATIBLE_STATE
    assert legacy_path.exists()


@pytest.mark.asyncio
async def test_cas_d43_invariant_violation_fails_closed_never_repoints(tmp_path: Path) -> None:
    """ADR-0011 D43 (fifth/sixth amendments): a live D34 Case A Source
    observed `DELETING` at its own migration CAS attempt is a lifecycle
    invariant violation, structurally excluded under the supported
    single-process deployment model -- never ordinary CAS contention. When
    `_cas_repoint` reports `D43_INVARIANT_VIOLATION`, `_migrate_case_a` must:
    never adopt the S3 target as migrated/already-converged, never clean up
    the local object, append a distinct integrity failure, and leave
    `ConvergenceResult.converged` false so bootstrap cannot reach OPERATIONAL
    from this pass."""

    snapshot, legacy_path = _migratable_snapshot(tmp_path)

    async def cas_repoint(snap: sc.SourceSnapshot, new_uri: str) -> sc.CasOutcome:
        return sc.CasOutcome.D43_INVARIANT_VIOLATION

    fake_storage = _FakeStorage()
    service = sc.StorageConvergenceService(
        make_settings(tmp_path),
        session_factory=None,  # type: ignore[arg-type]
        source_storage=fake_storage,
        list_sources=_const_sources([snapshot]),
        cas_repoint=cas_repoint,
    )
    result = await service.converge()

    # (4) no Postgres repoint reported as successful.
    assert result.migrated == 0
    assert result.already_converged == 0
    # (3) no local cleanup occurs.
    assert result.local_duplicates_cleaned == 0
    assert legacy_path.exists()
    # (2) classified as the distinct D43 violation, with the expected reason.
    assert len(result.integrity_failures) == 1
    assert (
        result.integrity_failures[0].reason
        is sc.IntegrityFailureReason.D43_DELETING_DURING_CASE_A_CAS
    )
    assert result.integrity_failures[0].source_id == snapshot.id
    # `converged` (the fixed-point signal `_run_convergence_to_fixed_point`
    # in lifespan.py checks before ever allowing OPERATIONAL) must be false.
    assert result.converged is False


def _const_sources(snapshots: list[sc.SourceSnapshot]):
    async def list_sources() -> list[sc.SourceSnapshot]:
        return snapshots

    return list_sources


# ---------------------------------------------------------------------------
# Crash windows / post-repoint cleanup (test items 31-35, B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_window_s3_valid_pg_still_file_reuses_target_and_cas(tmp_path: Path) -> None:
    """Crash window 1: S3 target already valid, PostgreSQL still file://."""

    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=legacy_path.as_uri())
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT  # already uploaded by a crashed prior attempt
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.migrated == 1  # CAS still needed/performed
    assert storage.finalize_calls == 1  # idempotent reuse, not a duplicate upload


@pytest.mark.asyncio
async def test_crash_window_pg_already_s3_legacy_duplicate_cleaned(tmp_path: Path) -> None:
    """Crash window 2: PostgreSQL already s3://, legacy local exact
    duplicate remains -- restart's cleanup pass removes it."""

    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=target)
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.local_duplicates_cleaned == 1
    assert not legacy_path.exists()
    assert not legacy_path.parent.exists()  # empty directory also cleaned


@pytest.mark.asyncio
async def test_crash_window_pg_already_s3_no_local_duplicate_is_noop(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=target)
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.local_duplicates_cleaned == 0
    assert not result.integrity_failures
    assert not result.cleanup_deferred


@pytest.mark.asyncio
async def test_b1_post_repoint_cleanup_removes_verified_legacy_duplicate(tmp_path: Path) -> None:
    """B1: STORAGE-004 deliberately leaves the verified legacy local final
    after Remember's own crash recovery -- STORAGE-006 cleans it up."""

    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=target)
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.local_duplicates_cleaned == 1
    assert not legacy_path.exists()


@pytest.mark.asyncio
async def test_local_duplicate_hash_conflict_is_never_silently_deleted(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"unrelated conflicting content")
    storage = _FakeStorage()
    target = storage.target_uri(
        dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    storage.objects[target] = CONTENT
    snapshot = make_snapshot(id=source_id, dataset_id=dataset_id, storage_uri=target)
    service, _ = _service(tmp_path, snapshots=[snapshot], storage=storage)
    result = await service.converge()
    assert result.local_duplicates_cleaned == 0
    assert legacy_path.exists()
    assert legacy_path.read_bytes() == b"unrelated conflicting content"
    assert len(result.cleanup_deferred) == 1
    assert result.cleanup_deferred[0].reason is sc.IntegrityFailureReason.HASH_MISMATCH


# ---------------------------------------------------------------------------
# Filesystem mode (test items 41-44)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filesystem_backend_is_a_noop(tmp_path: Path) -> None:
    listed = False

    async def list_sources() -> list[sc.SourceSnapshot]:
        nonlocal listed
        listed = True
        return []

    settings = make_settings(
        tmp_path, storage_backend="filesystem", storage_s3_bucket=None, storage_s3_region=None
    )
    service = sc.StorageConvergenceService(
        settings,
        session_factory=None,  # type: ignore[arg-type]
        list_sources=list_sources,
    )
    result = await service.converge()
    assert result == sc.ConvergenceResult()
    assert listed is False  # never even queries for historical rows


@pytest.mark.asyncio
async def test_filesystem_backend_leaves_historical_s3_source_untouched(tmp_path: Path) -> None:
    """Frozen product decision (D7): filesystem mode never reverse-migrates
    or inspects historical s3:// rows even if they exist."""

    snapshot = make_snapshot(storage_uri="s3://bucket/v1/sources/x/y/original.txt")
    called = False

    async def list_sources() -> list[sc.SourceSnapshot]:
        nonlocal called
        called = True
        return [snapshot]

    settings = make_settings(
        tmp_path, storage_backend="filesystem", storage_s3_bucket=None, storage_s3_region=None
    )
    service = sc.StorageConvergenceService(
        settings,
        session_factory=None,
        list_sources=list_sources,  # type: ignore[arg-type]
    )
    result = await service.converge()
    assert result == sc.ConvergenceResult()
    assert called is False


# ---------------------------------------------------------------------------
# DELETED tombstone regressions (test items 45-47)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_with_present_local_file_is_untouched(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    legacy_path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(CONTENT)
    snapshot = make_snapshot(
        id=source_id,
        dataset_id=dataset_id,
        status=SourceStatus.DELETED,
        storage_uri=legacy_path.as_uri(),
    )
    service, storage = _service(tmp_path, snapshots=[snapshot])
    result = await service.converge()
    assert result.skipped_deleted == 1
    assert result.migrated == 0
    assert result.local_duplicates_cleaned == 0
    assert legacy_path.exists()  # object presence is irrelevant, never touched


# ---------------------------------------------------------------------------
# Path safety (test items 52-55)
# ---------------------------------------------------------------------------


def test_module_never_uses_glob_or_directory_scanning() -> None:
    source = Path(sc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"glob", "rglob", "walk", "scandir", "listdir"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
            raise AssertionError(f"forbidden directory-scan call found: .{node.attr}(...)")
        if isinstance(node, ast.Name) and node.id in forbidden_calls:
            raise AssertionError(f"forbidden directory-scan call found: {node.id}(...)")


def test_module_never_imports_boto3_or_botocore() -> None:
    source = Path(sc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_names.add(node.module.split(".")[0])
    assert "boto3" not in module_names
    assert "botocore" not in module_names
