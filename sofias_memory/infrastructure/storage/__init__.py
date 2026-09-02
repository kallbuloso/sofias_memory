"""Source object storage infrastructure (ADR-0011).

Filesystem and (later) S3 are the complete, closed, first-party set of
supported Source-storage backends -- not a plugin point (ADR-0005, amended
by ADR-0011 D17).

This is a true lower-level implementation boundary: nothing under this
package imports ``sofias_memory.services``, FastAPI, SQLAlchemy, or any
pipeline module. ``services.remember``/``services.forget`` import *from*
here (never the reverse) and re-export their existing public helper names as
thin, translating compatibility wrappers for call sites STORAGE-003/004/005
have not migrated yet.
"""

from sofias_memory.infrastructure.storage.filesystem import FilesystemSourceObjectStorage
from sofias_memory.infrastructure.storage.port import (
    FinalizeResult,
    InvalidSourceStorageUriError,
    SourceObjectStorage,
    SourceStorageConflictError,
    SourceStorageError,
    SourceStoragePathError,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
    UnsupportedStorageBackendError,
)
from sofias_memory.infrastructure.storage.router import SourceStorageRouter

__all__ = [
    "FilesystemSourceObjectStorage",
    "FinalizeResult",
    "InvalidSourceStorageUriError",
    "SourceObjectStorage",
    "SourceStorageConflictError",
    "SourceStorageError",
    "SourceStoragePathError",
    "SourceStorageRouter",
    "SourceStorageUnavailableError",
    "StorageDeleteResult",
    "StorageDeleteStatus",
    "UnsupportedStorageBackendError",
]
