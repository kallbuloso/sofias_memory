"""SourceStorageRouter (ADR-0011 D4/D5).

Write routes by ``Settings.storage_backend`` ("where do *new* originals go
right now"); read/delete/verify route by the scheme already on
``Source.storage_uri`` ("where does *this* original actually live"),
independent of the current write backend. This split is what STORAGE-006's
mixed filesystem/S3 migration state (D5) depends on.

Two first-party backends only (ADR-0005, amended by ADR-0011 D17) -- no
generic provider registry, no dynamic plugins, no import-by-string. The S3
adapter is constructed lazily, on first actual need, via
``asyncio.to_thread`` (its construction may perform blocking credential-
provider work, ADR-0011 D16/D21) -- never eagerly at router construction
time, so a filesystem-only installation's startup never depends on S3
configuration being present (`STORAGE_BACKEND=filesystem` never touches it;
an `s3://` URI encountered while running filesystem-backend normally
requires S3 configuration to actually be present to read/delete it -- D5's
explicit "S3 remains S3-backed even if the write backend changes" contract).
If S3 configuration is absent when actually needed,
``UnsupportedStorageBackendError`` is raised -- never a startup crash.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse
from uuid import UUID

from sofias_memory.config import Settings
from sofias_memory.infrastructure.storage.filesystem import FilesystemSourceObjectStorage
from sofias_memory.infrastructure.storage.port import (
    FinalizeResult,
    SourceObjectStorage,
    StorageDeleteResult,
    UnsupportedStorageBackendError,
)
from sofias_memory.infrastructure.storage.s3 import (
    S3SourceObjectStorage,
    s3_object_key,
    s3_object_uri,
)

FILE_URI_SCHEME = "file"
S3_URI_SCHEME = "s3"

UNSUPPORTED_S3_BACKEND_MESSAGE = (
    "STORAGE_BACKEND=s3 is not supported by this build: no S3 adapter is configured."
)
S3_NOT_CONFIGURED_MESSAGE = "An s3:// Source object was requested but S3 storage is not configured."
UNSUPPORTED_STORAGE_SCHEME_MESSAGE = "Source storage location uses an unsupported scheme."


class SourceStorageRouter:
    """The only storage boundary pipelines/services are meant to depend on.

    No pipeline step or service imports this router yet -- call-site
    migration is owned by STORAGE-003 (reads), STORAGE-004 (Remember
    finalize), and STORAGE-005 (Forget/Dataset DELETE deletes).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        filesystem: SourceObjectStorage | None = None,
        s3: SourceObjectStorage | None = None,
    ) -> None:
        self._settings = settings
        self._filesystem: SourceObjectStorage = filesystem or FilesystemSourceObjectStorage(
            settings.data_directory
        )
        self._explicit_s3 = s3
        self._lazy_s3: SourceObjectStorage | None = None
        self._s3_construction_lock = asyncio.Lock()

    async def finalize(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        if self._settings.storage_backend == "filesystem":
            return await self._filesystem.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
                original_bytes=original_bytes,
            )
        if self._settings.storage_backend == "s3":
            s3_adapter = await self._s3_adapter()
            return await s3_adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
                original_bytes=original_bytes,
            )
        raise UnsupportedStorageBackendError(UNSUPPORTED_S3_BACKEND_MESSAGE)  # unreachable

    def deterministic_uri(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
    ) -> str:
        """Pure, I/O-free target-URI computation for the *current write*
        backend (ADR-0011 D12/B1) -- never constructs the (async, lazily
        built) S3 adapter, since the S3 case only needs pure key/URI math
        against already-validated ``Settings``."""

        if self._settings.storage_backend == "filesystem":
            return self._filesystem.deterministic_uri(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
            )
        if self._settings.storage_backend == "s3":
            if not self._settings.storage_s3_bucket:
                raise UnsupportedStorageBackendError(S3_NOT_CONFIGURED_MESSAGE)
            key = s3_object_key(
                prefix=self._settings.storage_s3_prefix,
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
            )
            return s3_object_uri(bucket=self._settings.storage_s3_bucket, key=key)
        raise UnsupportedStorageBackendError(UNSUPPORTED_S3_BACKEND_MESSAGE)  # unreachable

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
        adapter = await self._adapter_for_uri(storage_uri)
        return await adapter.read(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=storage_uri,
            expected_byte_size=expected_byte_size,
            expected_content_sha256=expected_content_sha256,
            max_bytes=max_bytes,
        )

    async def delete(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str | None,
    ) -> StorageDeleteResult:
        if storage_uri is None:
            # No locator at all is never an S3-vs-filesystem question --
            # NOT_REQUESTED is backend-independent, so route it directly
            # rather than through scheme dispatch.
            return await self._filesystem.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=None
            )
        adapter = await self._adapter_for_uri(storage_uri)
        return await adapter.delete(
            dataset_id=dataset_id, source_id=source_id, storage_uri=storage_uri
        )

    async def verify(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str,
        content_sha256: str,
    ) -> bool:
        adapter = await self._adapter_for_uri(storage_uri)
        return await adapter.verify(
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=storage_uri,
            content_sha256=content_sha256,
        )

    async def probe(self) -> None:
        """ADR-0011 D21 (STORAGE-007): exercise the configured S3 backend's
        real put/get/delete capability. Only ever called by the bootstrap
        task when ``STORAGE_BACKEND=s3``; constructs the lazy S3 adapter if
        it has not been built yet, same as any other first S3 use."""

        adapter = await self._s3_adapter()
        probe = getattr(adapter, "probe", None)
        if not callable(probe):
            raise UnsupportedStorageBackendError(UNSUPPORTED_S3_BACKEND_MESSAGE)  # pragma: no cover
        await probe()

    async def aclose(self) -> None:
        """Close the lazily-constructed S3 client, if one was ever built
        (STORAGE-007 application-owned shutdown) -- idempotent, safe to call
        even when S3 was never touched this process at all."""

        if self._lazy_s3 is not None:
            close = getattr(self._lazy_s3, "close", None)
            if callable(close):
                close()

    async def _adapter_for_uri(self, storage_uri: str) -> SourceObjectStorage:
        scheme = urlparse(storage_uri).scheme
        if scheme == FILE_URI_SCHEME:
            return self._filesystem
        if scheme == S3_URI_SCHEME:
            return await self._s3_adapter()
        raise UnsupportedStorageBackendError(UNSUPPORTED_STORAGE_SCHEME_MESSAGE)

    async def _s3_adapter(self) -> SourceObjectStorage:
        if self._explicit_s3 is not None:
            return self._explicit_s3
        if self._lazy_s3 is not None:
            return self._lazy_s3
        # Constructed lazily, at most once, guarded so two concurrent callers
        # never race to build two clients (ADR-0011 D10-style discipline
        # applied to adapter construction, not just migration).
        async with self._s3_construction_lock:
            if self._lazy_s3 is None:
                if not self._settings.storage_s3_bucket:
                    raise UnsupportedStorageBackendError(S3_NOT_CONFIGURED_MESSAGE)
                # boto3 client construction may perform blocking credential-
                # provider work (env/shared config/IMDS) -- never on the
                # event-loop thread.
                self._lazy_s3 = await asyncio.to_thread(S3SourceObjectStorage, self._settings)
        return self._lazy_s3
