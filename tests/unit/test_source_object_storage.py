"""Unit tests for the ADR-0011 Source object storage boundary (STORAGE-001).

Scope: prove the new port/filesystem-adapter/router seam is byte-for-byte
compatible with today's filesystem behavior. No pipeline step imports this
boundary yet (that is STORAGE-003/004/005) -- Remember/Forget/Dataset DELETE
step-level behavior (idempotent-replay vs. conflict-fails-safe) is covered
unmodified by ``test_remember_pipeline_steps.py``/``test_forget_pipeline_steps.py``
and is not duplicated here.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from sofias_memory.config import Settings
from sofias_memory.infrastructure.storage.filesystem import (
    FilesystemSourceObjectStorage,
    final_storage_path,
    final_storage_uri,
)
from sofias_memory.infrastructure.storage.port import (
    FinalizeResult,
    InvalidSourceStorageUriError,
    SourceObjectStorage,
    SourceStorageError,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
    UnsupportedStorageBackendError,
)
from sofias_memory.infrastructure.storage.router import (
    S3_NOT_CONFIGURED_MESSAGE,
    UNSUPPORTED_STORAGE_SCHEME_MESSAGE,
    SourceStorageRouter,
)
from sofias_memory.services.forget import StorageDeleteResult as ForgetStorageDeleteResult
from sofias_memory.services.forget import StorageDeleteStatus as ForgetStorageDeleteStatus

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

CONTENT = b"stored source bytes"
CONTENT_SHA256 = sha256(CONTENT).hexdigest()


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


class _FakeSourceObjectStorage:
    """Minimal ``SourceObjectStorage``-shaped test double proving
    ``SourceStorageRouter`` dispatches to the injected S3 adapter -- avoids
    depending on the real ``boto3`` client for pure router-dispatch tests."""

    def __init__(self) -> None:
        self.finalize_calls = 0
        self.read_calls = 0
        self.delete_calls = 0
        self.verify_calls = 0

    async def finalize(
        self,
        *,
        dataset_id: object,
        source_id: object,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        self.finalize_calls += 1
        return FinalizeResult(
            storage_uri=f"s3://fake/{dataset_id}/{source_id}{storage_extension}",
            already_present=False,
        )

    def deterministic_uri(
        self,
        *,
        dataset_id: object,
        source_id: object,
        storage_extension: str,
    ) -> str:
        return f"s3://fake/{dataset_id}/{source_id}{storage_extension}"

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
        self.read_calls += 1
        return CONTENT

    async def delete(
        self, *, dataset_id: object, source_id: object, storage_uri: str | None
    ) -> StorageDeleteResult:
        self.delete_calls += 1
        return StorageDeleteResult(StorageDeleteStatus.DELETED_NOW)

    async def verify(
        self, *, dataset_id: object, source_id: object, storage_uri: str, content_sha256: str
    ) -> bool:
        self.verify_calls += 1
        return True


# ---------------------------------------------------------------------------
# StorageDeleteResult/StorageDeleteStatus relocation (no duplicate definitions)
# ---------------------------------------------------------------------------


def test_forget_module_reexports_the_same_relocated_types() -> None:
    assert ForgetStorageDeleteResult is StorageDeleteResult
    assert ForgetStorageDeleteStatus is StorageDeleteStatus


def test_existing_three_outcomes_are_unchanged() -> None:
    # STORAGE-002 (ADR-0011 D37/D38) adds UNRESOLVED; the original three
    # values/semantics are otherwise unchanged.
    assert set(StorageDeleteStatus) == {
        StorageDeleteStatus.NOT_REQUESTED,
        StorageDeleteStatus.DELETED_NOW,
        StorageDeleteStatus.ALREADY_ABSENT,
        StorageDeleteStatus.UNRESOLVED,
    }
    assert StorageDeleteStatus.NOT_REQUESTED.value == "not_requested"
    assert StorageDeleteStatus.DELETED_NOW.value == "deleted_now"
    assert StorageDeleteStatus.ALREADY_ABSENT.value == "already_absent"


def test_completed_property_unchanged() -> None:
    assert not StorageDeleteResult(StorageDeleteStatus.NOT_REQUESTED).completed
    assert StorageDeleteResult(StorageDeleteStatus.DELETED_NOW).completed
    assert StorageDeleteResult(StorageDeleteStatus.ALREADY_ABSENT).completed
    assert not StorageDeleteResult(StorageDeleteStatus.UNRESOLVED).completed


# ---------------------------------------------------------------------------
# FilesystemSourceObjectStorage: deterministic path/URI compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_produces_the_same_path_and_uri_as_today(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)

    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    assert uri == final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert uri.startswith("file://")
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert path == tmp_path / str(dataset_id) / str(source_id) / "original.txt"
    assert path.read_bytes() == CONTENT


@pytest.mark.asyncio
async def test_finalize_new_bytes_succeeds(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)

    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".json",
        original_bytes=b'{"a": 1}',
    )
    uri = uri_result.storage_uri

    assert uri.endswith("original.json")


@pytest.mark.asyncio
async def test_finalize_replay_with_identical_bytes_converges(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)

    first_uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    first_uri = first_uri_result.storage_uri
    second_uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    second_uri = second_uri_result.storage_uri

    assert first_uri == second_uri
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert path.read_bytes() == CONTENT


# ---------------------------------------------------------------------------
# read(): verified bytes, containment, size/hash enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_verified_expected_bytes(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    result = await adapter.read(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=uri,
        expected_byte_size=len(CONTENT),
        expected_content_sha256=CONTENT_SHA256,
        max_bytes=1024,
    )

    assert result == CONTENT


@pytest.mark.asyncio
async def test_read_rejects_content_exceeding_max_bytes(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    with pytest.raises(SourceStorageUnavailableError):
        await adapter.read(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=uri,
            expected_byte_size=len(CONTENT),
            expected_content_sha256=CONTENT_SHA256,
            max_bytes=1,
        )


@pytest.mark.asyncio
async def test_read_rejects_hash_mismatch(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    with pytest.raises(SourceStorageUnavailableError):
        await adapter.read(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=uri,
            expected_byte_size=len(CONTENT),
            expected_content_sha256="0" * 64,
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_read_rejects_missing_object(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri = final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    with pytest.raises(SourceStorageUnavailableError):
        await adapter.read(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=uri,
            expected_byte_size=len(CONTENT),
            expected_content_sha256=CONTENT_SHA256,
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_read_rejects_traversal_outside_expected_directory(tmp_path: Path) -> None:
    # Reuses source_storage_path's own containment check unchanged (the same
    # primitive delete() already relied on) -- a mismatched dataset_id/
    # source_id in the URI raises the infra layer's own
    # InvalidSourceStorageUriError; services.forget's wrapper translates this
    # into SofiasMemoryError for its own callers (see test_forget_service.py).
    dataset_id, source_id = uuid4(), uuid4()
    other_dataset_id = uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    escaping_uri = (
        (tmp_path / str(other_dataset_id) / "escaped" / "original.txt").resolve().as_uri()
    )

    with pytest.raises(InvalidSourceStorageUriError):
        await adapter.read(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=escaping_uri,
            expected_byte_size=len(CONTENT),
            expected_content_sha256=CONTENT_SHA256,
            max_bytes=1024,
        )


# ---------------------------------------------------------------------------
# delete(): exact-file deletion, already-absent semantics, no collateral damage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_the_exact_object(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    result = await adapter.delete(dataset_id=dataset_id, source_id=source_id, storage_uri=uri)

    assert result.status is StorageDeleteStatus.DELETED_NOW
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert not path.exists()


@pytest.mark.asyncio
async def test_delete_none_uri_is_not_requested(tmp_path: Path) -> None:
    adapter = FilesystemSourceObjectStorage(tmp_path)

    result = await adapter.delete(dataset_id=uuid4(), source_id=uuid4(), storage_uri=None)

    assert result.status is StorageDeleteStatus.NOT_REQUESTED


@pytest.mark.asyncio
async def test_delete_missing_object_is_already_absent(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri = final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    result = await adapter.delete(dataset_id=dataset_id, source_id=source_id, storage_uri=uri)

    assert result.status is StorageDeleteStatus.ALREADY_ABSENT


@pytest.mark.asyncio
async def test_delete_never_touches_unrelated_data_directory_content(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri
    unrelated_ingress = tmp_path / "_ingress" / "unrelated-run"
    unrelated_ingress.mkdir(parents=True)
    (unrelated_ingress / "original").write_bytes(b"unrelated ingress bytes")
    sibling_dataset_id, sibling_source_id = uuid4(), uuid4()
    await adapter.finalize(
        dataset_id=sibling_dataset_id,
        source_id=sibling_source_id,
        storage_extension=".txt",
        original_bytes=b"sibling bytes",
    )

    await adapter.delete(dataset_id=dataset_id, source_id=source_id, storage_uri=uri)

    assert (unrelated_ingress / "original").read_bytes() == b"unrelated ingress bytes"
    sibling_path = final_storage_path(
        tmp_path,
        dataset_id=sibling_dataset_id,
        source_id=sibling_source_id,
        storage_extension=".txt",
    )
    assert sibling_path.read_bytes() == b"sibling bytes"


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_true_for_matching_content(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri_result = await adapter.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    assert await adapter.verify(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=uri,
        content_sha256=CONTENT_SHA256,
    )


@pytest.mark.asyncio
async def test_verify_false_for_missing_object(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    adapter = FilesystemSourceObjectStorage(tmp_path)
    uri = final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    assert not await adapter.verify(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=uri,
        content_sha256=CONTENT_SHA256,
    )


# ---------------------------------------------------------------------------
# SourceStorageRouter: filesystem-only scaffolding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_finalize_routes_to_filesystem_by_default(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    router = SourceStorageRouter(make_settings(tmp_path))

    uri_result = await router.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    assert uri == final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )


@pytest.mark.asyncio
async def test_router_read_routes_by_file_scheme(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    router = SourceStorageRouter(make_settings(tmp_path))
    uri_result = await router.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    result = await router.read(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=uri,
        expected_byte_size=len(CONTENT),
        expected_content_sha256=CONTENT_SHA256,
        max_bytes=1024,
    )

    assert result == CONTENT


@pytest.mark.asyncio
async def test_router_delete_routes_by_file_scheme(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    router = SourceStorageRouter(make_settings(tmp_path))
    uri_result = await router.finalize(
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    result = await router.delete(dataset_id=dataset_id, source_id=source_id, storage_uri=uri)

    assert result.status is StorageDeleteStatus.DELETED_NOW


@pytest.mark.asyncio
async def test_router_finalize_routes_to_s3_adapter_when_backend_is_s3(tmp_path: Path) -> None:
    fake_s3 = _FakeSourceObjectStorage()
    router = SourceStorageRouter(
        make_settings(tmp_path, storage_backend="s3", storage_s3_bucket="b", storage_s3_region="r"),
        s3=fake_s3,
    )

    uri_result = await router.finalize(
        dataset_id=uuid4(),
        source_id=uuid4(),
        storage_extension=".txt",
        original_bytes=CONTENT,
    )
    uri = uri_result.storage_uri

    assert uri.startswith("s3://fake/")
    assert fake_s3.finalize_calls == 1


@pytest.mark.asyncio
async def test_router_read_rejects_s3_uri_when_s3_not_configured(tmp_path: Path) -> None:
    # storage_backend stays filesystem (default) and no STORAGE_S3_* values
    # are set -- an s3:// URI can still legitimately be encountered (D5: a
    # Source finalized under a prior STORAGE_BACKEND=s3 configuration), and
    # the router must fail closed with a distinct, clear message rather than
    # crash the process or silently treat it as filesystem.
    router = SourceStorageRouter(make_settings(tmp_path))

    with pytest.raises(UnsupportedStorageBackendError, match=S3_NOT_CONFIGURED_MESSAGE):
        await router.read(
            dataset_id=uuid4(),
            source_id=uuid4(),
            storage_uri="s3://some-bucket/v1/sources/x/y/original.txt",
            expected_byte_size=len(CONTENT),
            expected_content_sha256=CONTENT_SHA256,
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_router_read_routes_to_s3_adapter_for_s3_scheme(tmp_path: Path) -> None:
    fake_s3 = _FakeSourceObjectStorage()
    router = SourceStorageRouter(
        make_settings(tmp_path, storage_s3_bucket="b", storage_s3_region="r"),
        s3=fake_s3,
    )

    result = await router.read(
        dataset_id=uuid4(),
        source_id=uuid4(),
        storage_uri="s3://fake/v1/sources/x/y/original.txt",
        expected_byte_size=len(CONTENT),
        expected_content_sha256=CONTENT_SHA256,
        max_bytes=1024,
    )

    assert result == CONTENT
    assert fake_s3.read_calls == 1


@pytest.mark.asyncio
async def test_router_delete_rejects_unsupported_scheme_cleanly(tmp_path: Path) -> None:
    router = SourceStorageRouter(make_settings(tmp_path))

    with pytest.raises(UnsupportedStorageBackendError, match=UNSUPPORTED_STORAGE_SCHEME_MESSAGE):
        await router.delete(
            dataset_id=uuid4(),
            source_id=uuid4(),
            storage_uri="ftp://example/original.txt",
        )


@pytest.mark.asyncio
async def test_router_delete_none_uri_never_needs_scheme_routing(tmp_path: Path) -> None:
    # backend=s3 (with valid mandatory-write config) proves this path never
    # even attempts to construct/consult the S3 adapter for a None URI.
    router = SourceStorageRouter(
        make_settings(tmp_path, storage_backend="s3", storage_s3_bucket="b", storage_s3_region="r")
    )

    result = await router.delete(dataset_id=uuid4(), source_id=uuid4(), storage_uri=None)

    assert result.status is StorageDeleteStatus.NOT_REQUESTED


@pytest.mark.asyncio
async def test_router_probe_delegates_to_the_lazily_constructed_s3_adapter(tmp_path: Path) -> None:
    probe_calls = 0

    class _FakeS3Adapter:
        async def probe(self) -> None:
            nonlocal probe_calls
            probe_calls += 1

    router = SourceStorageRouter(
        make_settings(tmp_path, storage_backend="s3", storage_s3_bucket="b", storage_s3_region="r"),
        s3=_FakeS3Adapter(),
    )

    await router.probe()

    assert probe_calls == 1


@pytest.mark.asyncio
async def test_router_probe_fails_closed_when_s3_not_configured(tmp_path: Path) -> None:
    router = SourceStorageRouter(make_settings(tmp_path))  # filesystem mode, no S3 config

    with pytest.raises(UnsupportedStorageBackendError, match=S3_NOT_CONFIGURED_MESSAGE):
        await router.probe()


@pytest.mark.asyncio
async def test_router_aclose_is_a_noop_when_s3_never_constructed(tmp_path: Path) -> None:
    router = SourceStorageRouter(make_settings(tmp_path))

    await router.aclose()  # must not raise


@pytest.mark.asyncio
async def test_router_aclose_never_closes_an_explicitly_injected_adapter(tmp_path: Path) -> None:
    """``aclose()`` only ever closes the router's *own* lazily-constructed S3
    adapter -- an explicitly-injected test double (``s3=...``, the same
    injection point STORAGE-002/003/004/005/006 all use) is caller-owned;
    the router never assumes ownership of it and therefore never closes it
    on the caller's behalf."""

    close_calls = 0

    class _FakeS3Adapter:
        async def probe(self) -> None:
            return None

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    router = SourceStorageRouter(
        make_settings(tmp_path, storage_backend="s3", storage_s3_bucket="b", storage_s3_region="r"),
        s3=_FakeS3Adapter(),
    )
    await router.probe()  # resolves through the explicit adapter, not laziness

    await router.aclose()

    assert close_calls == 0


def test_port_is_a_runtime_checkable_shape_the_adapter_satisfies(tmp_path: Path) -> None:
    adapter: SourceObjectStorage = FilesystemSourceObjectStorage(tmp_path)
    assert hasattr(adapter, "finalize")
    assert hasattr(adapter, "read")
    assert hasattr(adapter, "delete")
    assert hasattr(adapter, "verify")


def test_storage_errors_share_one_dependency_free_base() -> None:
    assert issubclass(InvalidSourceStorageUriError, SourceStorageError)
    assert issubclass(SourceStorageUnavailableError, SourceStorageError)
    assert issubclass(UnsupportedStorageBackendError, SourceStorageError)


# ---------------------------------------------------------------------------
# Legacy service wrappers: same function objects / faithful translation
# (services.remember/services.forget must depend on infrastructure.storage,
# never the reverse -- proven by import graph below, not just by these
# behavioral checks).
# ---------------------------------------------------------------------------


def test_services_remember_reexports_the_same_pure_functions() -> None:
    from sofias_memory.services import remember as remember_service

    assert remember_service.final_storage_path is final_storage_path
    assert remember_service.final_storage_uri is final_storage_uri
    assert remember_service.final_storage_content_matches is not None
    assert remember_service.final_storage_directory is not None


def test_services_remember_write_final_storage_bytes_translates_path_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The infra-level escape guard is defensive (dataset_id/source_id are
    # always trusted UUIDs in real call sites, so it cannot normally fire) --
    # proving the wrapper's translation is exercised directly via the
    # module's own private import seam, matching how it is actually wired.
    from sofias_memory.api.errors import SofiasMemoryError
    from sofias_memory.infrastructure.storage.port import SourceStoragePathError
    from sofias_memory.services import remember as remember_service

    def _raise(*_args: object, **_kwargs: object) -> str:
        raise SourceStoragePathError("boom")

    monkeypatch.setattr(remember_service, "_write_final_storage_bytes", _raise)

    with pytest.raises(SofiasMemoryError):
        remember_service.write_final_storage_bytes(
            Path("/data/sources"),
            dataset_id=uuid4(),
            source_id=uuid4(),
            storage_extension=".txt",
            original_bytes=CONTENT,
        )


def test_services_forget_source_storage_path_translates_invalid_uri() -> None:
    from sofias_memory.api.errors import SofiasMemoryError
    from sofias_memory.services.forget import source_storage_path

    with pytest.raises(SofiasMemoryError):
        source_storage_path(
            Path("/data/sources"),
            dataset_id=uuid4(),
            source_id=uuid4(),
            storage_uri="s3://not-a-file-uri/original.txt",
        )


def test_services_forget_delete_source_storage_translates_invalid_uri() -> None:
    from sofias_memory.api.errors import SofiasMemoryError
    from sofias_memory.services.forget import delete_source_storage

    with pytest.raises(SofiasMemoryError):
        delete_source_storage(
            Path("/data/sources"),
            dataset_id=uuid4(),
            source_id=uuid4(),
            storage_uri="s3://not-a-file-uri/original.txt",
        )


# ---------------------------------------------------------------------------
# Import-graph invariant: infrastructure.storage never depends on services,
# FastAPI, SQLAlchemy, or pipeline modules (ADR-0011 layering correction).
# ---------------------------------------------------------------------------


def _module_import_names(module_path: Path) -> list[str]:
    import ast

    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_infrastructure_storage_never_imports_services_or_fastapi_or_sqlalchemy() -> None:
    package_root = _repository_root() / "sofias_memory" / "infrastructure" / "storage"
    forbidden_prefixes = (
        "sofias_memory.services",
        "sofias_memory.pipelines",
        "sofias_memory.api",
        "fastapi",
        "sqlalchemy",
    )
    violations: list[str] = []
    for module_path in sorted(package_root.glob("*.py")):
        for module_name in _module_import_names(module_path):
            if module_name.startswith(forbidden_prefixes):
                violations.append(f"{module_path.name}: {module_name}")

    assert violations == [], (
        "infrastructure.storage must never import services/pipelines/api/"
        f"FastAPI/SQLAlchemy, found: {violations}"
    )


def test_boto3_only_appears_below_infrastructure_storage_s3_module() -> None:
    # ADR-0011 STORAGE-002: boto3/botocore usage is confined entirely to
    # infrastructure/storage/s3.py -- not even elsewhere inside the storage
    # package (port.py/filesystem.py/router.py stay boto3-free).
    package_root = _repository_root() / "sofias_memory" / "infrastructure" / "storage"
    violations: list[str] = []
    for module_path in sorted(package_root.glob("*.py")):
        if module_path.name == "s3.py":
            continue
        for module_name in _module_import_names(module_path):
            if module_name.startswith(("boto3", "botocore")):
                violations.append(f"{module_path.name}: {module_name}")

    assert violations == [], f"boto3/botocore must only appear in s3.py, found: {violations}"


def test_boto3_never_appears_above_infrastructure_storage() -> None:
    # Repository-wide: no boto3/botocore import may appear in services/,
    # pipelines/, domain/, or api/ (ADR-0011 STORAGE-002 layering
    # requirement -- the S3 SDK is owned entirely by the storage adapter).
    repo_root = _repository_root() / "sofias_memory"
    watched_packages = ("services", "pipelines", "domain", "api")
    violations: list[str] = []
    for package_name in watched_packages:
        package_root = repo_root / package_name
        if not package_root.exists():
            continue
        for module_path in sorted(package_root.rglob("*.py")):
            for module_name in _module_import_names(module_path):
                if module_name.startswith(("boto3", "botocore")):
                    relative = module_path.relative_to(repo_root)
                    violations.append(f"{relative}: {module_name}")

    assert violations == [], f"boto3/botocore leaked above infrastructure.storage: {violations}"
