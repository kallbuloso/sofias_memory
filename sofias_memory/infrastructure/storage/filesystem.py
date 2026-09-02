"""Filesystem-backed SourceObjectStorage adapter (ADR-0011 D2/D4).

This module is the canonical, lower-level owner of every filesystem Source-
object primitive: deterministic path/URI construction, containment-checked
resolution, atomic writes, content verification, and exact-file deletion.
``services.remember``/``services.forget`` re-export these functions as thin,
translating compatibility wrappers (their existing public names, unchanged
observable behavior) for call sites STORAGE-003/004/005 have not migrated
yet -- this module must never import back from ``sofias_memory.services``
(that was the STORAGE-001 layering violation this file corrects:
``infrastructure`` depends on nothing above it; ``services``/pipelines
depend on ``infrastructure``, never the reverse).

This module also never imports ``sofias_memory.api.errors`` -- that module
transitively imports FastAPI at module level (its own exception-handler
functions), which this lower-level boundary must not depend on. Failures
raise the plain, dependency-free exceptions in
:mod:`sofias_memory.infrastructure.storage.port`
(``InvalidSourceStorageUriError``, ``SourceStorageUnavailableError``,
``SourceStoragePathError``); ``services.remember``/``services.forget``'s
wrapper functions translate these into the existing ``SofiasMemoryError``
family so every current caller/test keeps observing the exact same public
contract.

Logic is relocated verbatim from its original home in ``services.remember``/
``services.forget`` -- this is a relocation of already-proven path safety,
not a rewrite of behavior; only the raised exception types changed, and only
at this layer.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import UUID

from sofias_memory.infrastructure.storage.port import (
    FinalizeResult,
    InvalidSourceStorageUriError,
    SourceStorageConflictError,
    SourceStoragePathError,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
)

SOURCE_STORAGE_UNAVAILABLE_MESSAGE = "Source storage is unavailable."
SOURCE_STORAGE_CONFLICT_MESSAGE = "Source storage target already holds different content."

# ---------------------------------------------------------------------------
# Final storage helpers (relocated from services.remember, SM-513 SS 16/17):
# filesystem I/O only, never called from a PipelineStep's persist().
# ---------------------------------------------------------------------------


def final_storage_directory(data_directory: Path, *, dataset_id: UUID, source_id: UUID) -> Path:
    return data_directory / str(dataset_id) / str(source_id)


def final_storage_path(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_extension: str,
) -> Path:
    return (
        final_storage_directory(data_directory, dataset_id=dataset_id, source_id=source_id)
        / f"original{storage_extension}"
    )


def final_storage_uri(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_extension: str,
) -> str:
    """Pure path computation, no filesystem access (safe to call from a
    ``persist()`` phase, ADR-0009 SS O): the ``file://`` URI a source's final
    storage path resolves to, assuming ``data_directory`` is already an
    absolute, canonical root (true for ``Settings.data_directory``). Never
    calls ``Path.resolve()`` -- that performs real syscalls (symlink
    readback), which ``persist()`` may not do; ``dataset_id``/``source_id``
    are trusted application-generated UUIDs here, not values parsed back
    from an untrusted URI, so no symlink-escape re-check is needed the way
    the *read*-side ``source_storage_path`` requires."""

    return final_storage_path(
        data_directory,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=storage_extension,
    ).as_uri()


def write_final_storage_bytes(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_extension: str,
    original_bytes: bytes,
) -> str:
    """Atomically write ``original_bytes`` to the source's final storage
    path and return its ``file://`` URI. ``dataset_id``/``source_id`` are
    always application-generated UUIDs at this call site (never parsed back
    from client input), so the guard here only needs to prevent this
    function's own output from ever escaping ``data_directory``."""

    storage_root = data_directory.resolve()
    target_directory = (storage_root / str(dataset_id) / str(source_id)).resolve()
    if not target_directory.is_relative_to(storage_root):
        raise SourceStoragePathError("Source storage path is invalid.")
    target_directory.mkdir(parents=True, exist_ok=True)
    target_path = target_directory / f"original{storage_extension}"
    temporary_path = target_directory / f"original{storage_extension}.tmp"
    temporary_path.write_bytes(original_bytes)
    temporary_path.replace(target_path)
    return final_storage_uri(
        data_directory,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=storage_extension,
    )


def final_storage_content_matches(path: Path, *, content_sha256: str) -> bool:
    """Whether an already-existing final storage file's content hash matches
    the expected content identity -- the idempotent-replay check that lets
    ``FinalizeStorageStep`` treat a file already present (a prior attempt's
    own write, or a crash exactly after the copy but before the
    ``storage_uri`` commit) as safely done rather than re-copying or
    failing."""

    if not path.is_file():
        return False
    return sha256(path.read_bytes()).hexdigest() == content_sha256


# ---------------------------------------------------------------------------
# Storage deletion safety (relocated from services.forget, unchanged guards).
# ---------------------------------------------------------------------------


def delete_source_storage(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_uri: str | None,
) -> StorageDeleteResult:
    if storage_uri is None:
        return StorageDeleteResult(StorageDeleteStatus.NOT_REQUESTED)
    target_path = source_storage_path(
        data_directory, dataset_id=dataset_id, source_id=source_id, storage_uri=storage_uri
    )
    if target_path is None:
        return StorageDeleteResult(StorageDeleteStatus.ALREADY_ABSENT)
    try:
        target_path.unlink()
    except OSError as exc:
        raise SourceStorageUnavailableError("Source storage could not be deleted.") from exc
    return StorageDeleteResult(StorageDeleteStatus.DELETED_NOW)


def source_storage_path(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    storage_uri: str,
) -> Path | None:
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise invalid_storage_uri_error()

    storage_root = data_directory.resolve(strict=False)
    expected_directory = (storage_root / str(dataset_id) / str(source_id)).resolve(strict=False)
    raw_path = Path(url2pathname(parsed.path))
    nominal_path = raw_path.resolve(strict=False)
    if not expected_directory.is_relative_to(storage_root):
        raise invalid_storage_uri_error()
    if not nominal_path.is_relative_to(expected_directory):
        raise invalid_storage_uri_error()
    if not raw_path.exists():
        return None

    resolved_path = raw_path.resolve(strict=True)
    if not resolved_path.is_relative_to(expected_directory):
        raise invalid_storage_uri_error()
    if not resolved_path.is_file() or resolved_path.is_dir():
        raise invalid_storage_uri_error()
    return resolved_path


def invalid_storage_uri_error() -> InvalidSourceStorageUriError:
    return InvalidSourceStorageUriError("Source storage location is invalid.")


# ---------------------------------------------------------------------------
# SourceObjectStorage adapter
# ---------------------------------------------------------------------------


class FilesystemSourceObjectStorage:
    """``SourceObjectStorage`` backed by ``Settings.data_directory``.

    Today's only backend (ADR-0011 D2 default). ``data_directory`` is passed
    in explicitly rather than reading ``Settings`` again here, keeping this
    class free of any config-loading responsibility of its own.
    """

    def __init__(self, data_directory: Path) -> None:
        self._data_directory = data_directory

    async def finalize(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        content_sha256 = sha256(original_bytes).hexdigest()
        target_path = final_storage_path(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
        )
        if final_storage_content_matches(target_path, content_sha256=content_sha256):
            return FinalizeResult(
                storage_uri=final_storage_uri(
                    self._data_directory,
                    dataset_id=dataset_id,
                    source_id=source_id,
                    storage_extension=storage_extension,
                ),
                already_present=True,
            )
        if target_path.exists():
            raise SourceStorageConflictError(SOURCE_STORAGE_CONFLICT_MESSAGE)
        storage_uri = write_final_storage_bytes(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
            original_bytes=original_bytes,
        )
        return FinalizeResult(storage_uri=storage_uri, already_present=False)

    def deterministic_uri(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
    ) -> str:
        return final_storage_uri(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
        )

    async def read(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str,
        expected_byte_size: int,
        expected_content_sha256: str,
        max_bytes: int,
    ) -> bytes:
        # Reuses this module's own containment-checked resolver -- the same
        # primitive delete() below uses -- rather than a second, independent
        # path-safety implementation (ADR-0011 review finding G).
        path = source_storage_path(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=storage_uri,
        )
        if path is None:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        if expected_byte_size > max_bytes:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        original_bytes = path.read_bytes()
        if len(original_bytes) != expected_byte_size:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        if sha256(original_bytes).hexdigest() != expected_content_sha256:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        return original_bytes

    async def delete(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str | None,
    ) -> StorageDeleteResult:
        return delete_source_storage(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=storage_uri,
        )

    async def verify(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str,
        content_sha256: str,
    ) -> bool:
        path = source_storage_path(
            self._data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=storage_uri,
        )
        if path is None:
            return False
        return final_storage_content_matches(path, content_sha256=content_sha256)
