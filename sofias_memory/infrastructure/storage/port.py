"""First-party Source object storage port (ADR-0011 D4).

A narrow, closed boundary -- not a generic blob-storage abstraction (ADR-0005,
amended by ADR-0011 D17). Exactly two adapters are ever expected to implement
:class:`SourceObjectStorage`: a filesystem adapter (this slice, STORAGE-001)
and an S3 adapter (STORAGE-002). Nothing here imports FastAPI, SQLAlchemy, or
any provider SDK -- the port is pure typing plus the storage-deletion result
contract, safe to import from `domain`-adjacent code without pulling in I/O
dependencies.

``StorageDeleteStatus``/``StorageDeleteResult`` are relocated here from
``sofias_memory.services.forget`` (their original home) because ADR-0011 D14
frames them as part of the storage boundary itself, not something owned by
Forget specifically -- Dataset DELETE's `DeleteStorageStep` needs the exact
same result shape. Values and semantics of the original three outcomes are
unchanged; ``services.forget`` re-exports them for backward compatibility.

``UNRESOLVED`` is added in STORAGE-002 (ADR-0011 D37/D38) because it is the
S3 adapter that first needs to *produce* it -- a recognized operational
inability to prove physical deletion (credentials, AccessDenied, timeout,
Object Lock, or a versioned-bucket purge that cannot be verified complete).
Adding the enum value here does not by itself change any pipeline/finalizer
semantics: the filesystem adapter never produces ``UNRESOLVED``, and no
existing Forget/`DATASET_DELETE` call site is wired to the S3 adapter yet
(that wiring, and the business-delete-must-converge consumption of this
outcome, remain STORAGE-005's job).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class FinalizeResult:
    """``finalize()``'s complete outcome (ADR-0011 D12, STORAGE-004).

    ``storage_uri`` is always the deterministic target's URI, whether this
    call actually wrote bytes or recognized a matching target already there.
    ``already_present`` distinguishes the two only for the caller's own
    status/metrics reporting (``FinalizeStorageStep``'s ``storage_status``);
    it never changes how ``persist()`` behaves -- either way the URI is
    committed at most once.
    """

    storage_uri: str
    already_present: bool


class StorageDeleteStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    DELETED_NOW = "deleted_now"
    ALREADY_ABSENT = "already_absent"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class StorageDeleteResult:
    status: StorageDeleteStatus

    @property
    def completed(self) -> bool:
        return self.status in {
            StorageDeleteStatus.DELETED_NOW,
            StorageDeleteStatus.ALREADY_ABSENT,
        }


class SourceStorageError(Exception):
    """Base for every error the storage infrastructure layer itself raises.

    Deliberately a plain ``Exception`` subclass with no HTTP/FastAPI
    dependency -- ``sofias_memory.api.errors`` (the ``SofiasMemoryError``
    family) transitively imports ``fastapi`` at module level, which
    ``infrastructure.storage`` must never depend on (ADR-0011 D4: this
    package is a lower-level implementation boundary, not a services layer).
    Callers above this layer (``services.remember``/``services.forget``
    today; pipeline steps directly once STORAGE-003/004/005 migrate their
    call sites) translate these into the application's HTTP-facing error
    family at their own layer, preserving each existing call site's exact
    public contract.
    """


class InvalidSourceStorageUriError(SourceStorageError):
    """A ``storage_uri`` failed scheme, containment, or identity validation."""


class SourceStorageUnavailableError(SourceStorageError):
    """The requested Source object could not be read, verified, or deleted
    (missing, size/hash mismatch, or an OS-level failure)."""


class SourceStoragePathError(SourceStorageError):
    """A computed target path would escape the configured storage root."""


class SourceStorageConflictError(SourceStorageError):
    """A deterministic target already holds different content than expected
    (ADR-0011 D8 step F's conflict case) -- never silently overwritten."""


class UnsupportedStorageBackendError(SourceStorageError):
    """The selected backend or ``storage_uri`` scheme has no adapter in this
    build (ADR-0011 D2/D5 fail-closed scaffolding)."""


class SourceObjectStorage(Protocol):
    """One backend's implementation of Source-original storage semantics.

    Every method is async: filesystem I/O is cheap enough to run inline
    today, but the S3 adapter (STORAGE-002) needs real network I/O behind
    the identical signatures -- callers must never know which backend they
    are talking to (ADR-0011 D4/D13).
    """

    async def finalize(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        """Idempotently write the finalized Source original.

        A matching target already present is a successful no-op replay
        (``already_present=True``); an absent target is written
        (``already_present=False``); a target holding different content
        raises ``SourceStorageConflictError`` (ADR-0011 D12) -- the adapter
        itself owns this decision so every caller observes identical
        semantics regardless of backend.
        """
        ...

    def deterministic_uri(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
    ) -> str:
        """Pure, I/O-free computation of the target URI for this identity.

        Lets a caller check ``verify()`` against a not-yet-written target
        (ADR-0011 D12/B1: "does the deterministic target already hold the
        expected content" must be answerable before any bytes are read).
        """
        ...

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
        """Return verified original bytes, or raise a typed, safe error."""
        ...

    async def delete(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str | None,
    ) -> StorageDeleteResult:
        """Delete the exact Source original; already-absent is success."""
        ...

    async def verify(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str,
        content_sha256: str,
    ) -> bool:
        """Whether the object at ``storage_uri`` matches ``content_sha256``."""
        ...
