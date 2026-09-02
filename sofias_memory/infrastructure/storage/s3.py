"""S3-backed SourceObjectStorage adapter (ADR-0011 D2/D4-D6/D11/D15/D16/D36-D38).

``boto3`` (official, synchronous SDK) is used behind ``asyncio.to_thread``,
bounded by an ``asyncio.Semaphore`` -- the decision recorded in the active
execution plan's STORAGE-002 SDK checkpoint, mirroring this codebase's own
existing pattern for bounded concurrent offload of a sync-only dependency
(``infrastructure/llm.py``, ``infrastructure/embeddings.py``).

**Event-loop safety (binding, not optional).** Every blocking ``boto3``/
``botocore`` interaction -- including ``StreamingBody.read``/``.close`` and
every ``list_object_versions`` page fetch -- is fully contained inside one
function passed to ``asyncio.to_thread``. No ``StreamingBody`` or other
blocking object ever crosses back onto the event-loop thread. All such
functions are named with a ``_sync`` suffix and are the *only* place this
module calls into ``boto3``/``botocore`` directly.

**Error translation boundary.** ``botocore.exceptions.ClientError``/
``BotoCoreError`` are caught only here and translated into the dependency-
free exceptions in :mod:`sofias_memory.infrastructure.storage.port`. No
``botocore``/``boto3`` type ever leaves this module. Unexpected exceptions
(anything that is not a recognized ``ClientError``/``BotoCoreError``) are
never caught here -- they propagate as genuine defects, never silently
become ``StorageDeleteStatus.UNRESOLVED``.

**Managed namespace (D36).** Every operation this adapter performs targets
exactly one validated Source key under
``<prefix>/v1/sources/<dataset_id>/<source_id>/original<extension>`` --
never a prefix-wide or bucket-wide operation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from sofias_memory.config import Settings
from sofias_memory.infrastructure.storage.port import (
    FinalizeResult,
    InvalidSourceStorageUriError,
    SourceStorageConflictError,
    SourceStoragePathError,
    SourceStorageUnavailableError,
    StorageDeleteResult,
    StorageDeleteStatus,
)

# No `boto3-stubs`/`mypy-boto3-s3` dependency is added for this (ADR-0011
# D17: the S3 SDK's own transitive dependencies are the only new pins this
# slice introduces) -- the boto3 client is typed as `Any` at this boundary;
# every use of it is confined to this module's own `_sync` methods.
S3Client = Any

S3_URI_SCHEME = "s3"
DETERMINISTIC_KEY_ROOT = "v1/sources"
FINAL_OBJECT_FILENAME_PREFIX = "original"

# ADR-0011 D21: a reserved system prefix, entirely outside the managed
# `v1/sources/...` Source namespace (D6/D36) -- the startup probe never
# writes/reads/deletes anything a real Source could ever occupy.
PROBE_KEY_ROOT = "v1/system/probe"
PROBE_OBJECT_BODY = b"sofias-memory-storage-probe"


def _probe_key_prefix(prefix: str) -> str:
    return "/".join([*normalized_prefix_segments(prefix), PROBE_KEY_ROOT])


# ADR-0011 D11: Sofias-Memory-owned object metadata -- never trust ETag as
# SHA-256. S3 lower-cases and stores these under an `x-amz-meta-` prefix.
SHA256_METADATA_KEY = "sofias-memory-sha256"
BYTE_SIZE_METADATA_KEY = "sofias-memory-byte-size"

# S3 DeleteObjects accepts at most 1000 keys per call.
DELETE_BATCH_SIZE = 1000
# GET streaming enforcement chunk size -- bounds peak memory overshoot past
# max_bytes to at most one chunk, regardless of what ContentLength claims.
READ_CHUNK_BYTES = 1024 * 1024

SOURCE_STORAGE_UNAVAILABLE_MESSAGE = "Source storage is unavailable."
SOURCE_STORAGE_CONFLICT_MESSAGE = "Source storage target already holds different content."


# ---------------------------------------------------------------------------
# URI / key construction and parsing (ADR-0011 D6): centralized, no traversal,
# no client-controlled filename, identity always bound to dataset_id/source_id.
# ---------------------------------------------------------------------------


def normalized_prefix_segments(prefix: str) -> list[str]:
    return [prefix] if prefix else []


def s3_object_key(
    *,
    prefix: str,
    dataset_id: UUID,
    source_id: UUID,
    storage_extension: str,
) -> str:
    """Deterministic key -- pure, no I/O. Mirrors
    ``filesystem.final_storage_path``'s role for the S3 backend."""

    segments = [
        *normalized_prefix_segments(prefix),
        DETERMINISTIC_KEY_ROOT,
        str(dataset_id),
        str(source_id),
    ]
    return "/".join(segments) + f"/{FINAL_OBJECT_FILENAME_PREFIX}{storage_extension}"


def s3_object_uri(*, bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def parse_s3_storage_uri(
    storage_uri: str,
    *,
    prefix: str,
    dataset_id: UUID,
    source_id: UUID,
) -> tuple[str, str]:
    """Parse and validate ``storage_uri`` against the exact expected identity
    for ``dataset_id``/``source_id``. Fails closed (never guesses/broadens
    the managed target) on any traversal, encoding, or identity mismatch."""

    parsed = urlparse(storage_uri)
    if parsed.scheme != S3_URI_SCHEME or not parsed.netloc or parsed.query or parsed.fragment:
        raise InvalidSourceStorageUriError("Source storage location is invalid.")

    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not key:
        raise InvalidSourceStorageUriError("Source storage location is invalid.")

    segments = key.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise InvalidSourceStorageUriError("Source storage location is invalid.")

    expected_directory = "/".join(
        [
            *normalized_prefix_segments(prefix),
            DETERMINISTIC_KEY_ROOT,
            str(dataset_id),
            str(source_id),
        ]
    )
    if not key.startswith(f"{expected_directory}/"):
        raise InvalidSourceStorageUriError("Source storage location is invalid.")

    filename = key[len(expected_directory) + 1 :]
    if "/" in filename or not filename.startswith(FINAL_OBJECT_FILENAME_PREFIX):
        raise InvalidSourceStorageUriError("Source storage location is invalid.")

    return bucket, key


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def build_s3_client(settings: Settings) -> S3Client:
    """Construct one ``boto3`` S3 client from validated ``Settings``.

    Never called on the event-loop thread directly by
    :class:`S3SourceObjectStorage` -- callers offload construction via
    ``asyncio.to_thread`` because the default AWS credential provider chain
    (used whenever explicit credentials are absent) may perform blocking
    local/IMDS lookups. Explicit credentials are passed to the client
    constructor directly when configured (ADR-0011 D16) so the provider
    chain is consulted only when the operator did not supply static
    credentials.
    """

    client_kwargs: dict[str, object] = {"region_name": settings.storage_s3_region}
    if settings.storage_s3_endpoint_url:
        client_kwargs["endpoint_url"] = settings.storage_s3_endpoint_url
    if settings.storage_s3_access_key_id and settings.storage_s3_secret_access_key:
        client_kwargs["aws_access_key_id"] = settings.storage_s3_access_key_id.get_secret_value()
        client_kwargs["aws_secret_access_key"] = (
            settings.storage_s3_secret_access_key.get_secret_value()
        )
        if settings.storage_s3_session_token:
            client_kwargs["aws_session_token"] = (
                settings.storage_s3_session_token.get_secret_value()
            )

    pool_connections = max(settings.storage_s3_max_concurrency, 10)
    config = BotoConfig(
        max_pool_connections=pool_connections,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client("s3", config=config, **client_kwargs)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ObjectIdentity:
    byte_size: int
    content_sha256: str | None


class S3SourceObjectStorage:
    """``SourceObjectStorage`` backed by S3 (ADR-0011 D2 second backend)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: S3Client | None = None,
    ) -> None:
        if not settings.storage_s3_bucket:
            raise SourceStoragePathError("STORAGE_S3_BUCKET is required to build the S3 adapter.")
        self._bucket = settings.storage_s3_bucket
        self._prefix = settings.storage_s3_prefix
        self._client: S3Client = client if client is not None else build_s3_client(settings)
        self._semaphore = asyncio.Semaphore(settings.storage_s3_max_concurrency)

    def close(self) -> None:
        """Cheap, optional cleanup -- idle connections time out on their own;
        wired into the application shutdown path by STORAGE-007, not here."""

        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    async def probe(self) -> None:
        """ADR-0011 D21 startup probe: exercise PUT, GET, and DELETE against
        one namespaced temporary object under a reserved system prefix
        (``<prefix>/v1/system/probe/...``), entirely outside the
        ``v1/sources/...`` managed Source key space (D6/D36) -- proves real
        write/read/delete capability, not merely that credentials parse or
        the bucket resolves. Raises a :class:`SourceStorageError` subclass on
        any failure; the caller (STORAGE-007's bootstrap task) decides how to
        surface that as a safe, non-blocking-liveness readiness condition.
        """

        key = f"{_probe_key_prefix(self._prefix)}/{uuid4()}"
        async with self._semaphore:
            await asyncio.to_thread(self._probe_sync, key)

    def _probe_sync(self, key: str) -> None:
        body = PROBE_OBJECT_BODY
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body)  # type: ignore[union-attr]
            response = self._client.get_object(Bucket=self._bucket, Key=key)  # type: ignore[union-attr]
            fetched = response["Body"].read()
            response["Body"].close()
            if fetched != body:
                raise SourceStorageUnavailableError(
                    "S3 probe object readback did not match the expected content."
                )
        except (ClientError, BotoCoreError) as exc:
            raise SourceStorageUnavailableError(
                "S3 probe failed: put/get capability could not be confirmed."
            ) from exc
        finally:
            # Best-effort, idempotent cleanup (D21): a leftover probe object
            # from an interrupted prior boot must never fail a later probe --
            # delete failures here are deliberately swallowed, never raised.
            with contextlib.suppress(ClientError, BotoCoreError):
                self._client.delete_object(Bucket=self._bucket, Key=key)  # type: ignore[union-attr]

    # -- SourceObjectStorage -------------------------------------------------

    async def finalize(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        key = s3_object_key(
            prefix=self._prefix,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
        )
        content_sha256 = sha256(original_bytes).hexdigest()
        byte_size = len(original_bytes)
        async with self._semaphore:
            already_present = await asyncio.to_thread(
                self._finalize_sync, key, original_bytes, content_sha256, byte_size
            )
        return FinalizeResult(
            storage_uri=s3_object_uri(bucket=self._bucket, key=key),
            already_present=already_present,
        )

    def deterministic_uri(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
    ) -> str:
        key = s3_object_key(
            prefix=self._prefix,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
        )
        return s3_object_uri(bucket=self._bucket, key=key)

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
        bucket, key = parse_s3_storage_uri(
            storage_uri, prefix=self._prefix, dataset_id=dataset_id, source_id=source_id
        )
        if expected_byte_size > max_bytes:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        async with self._semaphore:
            data = await asyncio.to_thread(self._get_object_bytes_sync, bucket, key, max_bytes)
        if len(data) != expected_byte_size:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        if sha256(data).hexdigest() != expected_content_sha256:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        return data

    async def delete(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str | None,
    ) -> StorageDeleteResult:
        if storage_uri is None:
            return StorageDeleteResult(StorageDeleteStatus.NOT_REQUESTED)
        bucket, key = parse_s3_storage_uri(
            storage_uri, prefix=self._prefix, dataset_id=dataset_id, source_id=source_id
        )
        async with self._semaphore:
            status = await asyncio.to_thread(self._delete_object_sync, bucket, key)
        return StorageDeleteResult(status)

    async def verify(
        self,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_uri: str,
        content_sha256: str,
    ) -> bool:
        bucket, key = parse_s3_storage_uri(
            storage_uri, prefix=self._prefix, dataset_id=dataset_id, source_id=source_id
        )
        async with self._semaphore:
            return await asyncio.to_thread(self._verify_sync, bucket, key, content_sha256)

    # -- Offloaded synchronous operations (never call these directly) -------

    def _head_object_sync(self, bucket: str, key: str) -> _ObjectIdentity | None:
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise
        # Not every S3-compatible provider lower-cases custom metadata header
        # names the way AWS S3 does (observed live: some providers echo
        # back e.g. ``Sofias-Memory-Sha256`` instead of
        # ``sofias-memory-sha256``) -- normalize case defensively so D11's
        # identity check is not provider-casing-dependent.
        metadata = {str(k).lower(): v for k, v in response.get("Metadata", {}).items()}
        return _ObjectIdentity(
            byte_size=int(response.get("ContentLength") or 0),
            content_sha256=metadata.get(SHA256_METADATA_KEY),
        )

    def _finalize_sync(
        self, key: str, original_bytes: bytes, content_sha256: str, byte_size: int
    ) -> bool:
        """Returns ``True`` when an already-matching target made this call a
        no-op replay, ``False`` when this call actually wrote the object."""

        existing = self._head_object_sync(self._bucket, key)
        if existing is not None:
            if _identity_matches(existing, content_sha256=content_sha256, byte_size=byte_size):
                return True  # idempotent replay (ADR-0011 D12) -- nothing to do
            raise SourceStorageConflictError(SOURCE_STORAGE_CONFLICT_MESSAGE)

        try:
            self._client.put_object(  # type: ignore[union-attr]
                Bucket=self._bucket,
                Key=key,
                Body=original_bytes,
                Metadata={
                    SHA256_METADATA_KEY: content_sha256,
                    BYTE_SIZE_METADATA_KEY: str(byte_size),
                },
            )
        except (ClientError, BotoCoreError) as exc:
            # Ambiguous transport failure: re-inspect before deciding, rather
            # than blindly issuing a second PUT (ADR-0011 D12/finalize
            # contract) -- the write may have actually landed.
            reinspected = self._head_object_sync(self._bucket, key)
            if reinspected is not None and _identity_matches(
                reinspected, content_sha256=content_sha256, byte_size=byte_size
            ):
                return True
            if reinspected is not None:
                raise SourceStorageConflictError(SOURCE_STORAGE_CONFLICT_MESSAGE) from exc
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE) from exc

        confirmed = self._head_object_sync(self._bucket, key)
        if confirmed is None or not _identity_matches(
            confirmed, content_sha256=content_sha256, byte_size=byte_size
        ):
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
        return False

    def _get_object_bytes_sync(self, bucket: str, key: str, max_bytes: int) -> bytes:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
        except ClientError as exc:
            if _is_not_found(exc):
                raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE) from exc
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE) from exc
        except BotoCoreError as exc:
            raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE) from exc

        content_length = response.get("ContentLength")
        body = response["Body"]
        try:
            if content_length is not None and content_length > max_bytes:
                raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = body.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                # Never trust ContentLength alone -- enforce the cap while
                # streaming too, so a broken/malicious response cannot cause
                # unbounded memory growth.
                if total > max_bytes:
                    raise SourceStorageUnavailableError(SOURCE_STORAGE_UNAVAILABLE_MESSAGE)
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            body.close()

    def _verify_sync(self, bucket: str, key: str, content_sha256: str) -> bool:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
        except (ClientError, BotoCoreError):
            return False
        body = response["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        return sha256(data).hexdigest() == content_sha256

    def _delete_object_sync(self, bucket: str, key: str) -> StorageDeleteStatus:
        try:
            versioning_status = self._get_bucket_versioning_sync(bucket)
        except (ClientError, BotoCoreError):
            # Cannot even determine versioning state -- a recognized
            # operational inability to prove complete deletion (D15).
            return StorageDeleteStatus.UNRESOLVED

        if versioning_status in ("Enabled", "Suspended"):
            return self._delete_all_versions_sync(bucket, key)
        return self._delete_unversioned_sync(bucket, key)

    def _get_bucket_versioning_sync(self, bucket: str) -> str:
        response = self._client.get_bucket_versioning(Bucket=bucket)  # type: ignore[union-attr]
        return str(response.get("Status") or "")

    def _delete_unversioned_sync(self, bucket: str, key: str) -> StorageDeleteStatus:
        # STORAGE-005 exception-classification audit: _head_object_sync only
        # swallows a *not-found* ClientError (returning None) -- any other
        # recognized condition (AccessDenied, a transient BotoCoreError,
        # ...) re-raises. Both HEAD calls here must therefore be guarded
        # explicitly, or such a condition would escape this method as a raw
        # botocore exception instead of the typed UNRESOLVED outcome D37/D38
        # require for it.
        try:
            existing = self._head_object_sync(bucket, key)
        except (ClientError, BotoCoreError):
            return StorageDeleteStatus.UNRESOLVED
        if existing is None:
            return StorageDeleteStatus.ALREADY_ABSENT
        try:
            self._client.delete_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
        except (ClientError, BotoCoreError):
            return StorageDeleteStatus.UNRESOLVED
        try:
            confirmed_absent = self._head_object_sync(bucket, key) is None
        except (ClientError, BotoCoreError):
            # The delete call itself succeeded, but post-delete verification
            # could not be completed -- D15/D38's positive-evidence
            # requirement means this must not be reported as DELETED_NOW
            # without proof of absence.
            return StorageDeleteStatus.UNRESOLVED
        return (
            StorageDeleteStatus.DELETED_NOW if confirmed_absent else StorageDeleteStatus.UNRESOLVED
        )

    def _list_exact_key_versions_sync(self, bucket: str, key: str) -> tuple[list[str], list[str]]:
        """Every version id and delete-marker version id for the *exact*
        ``key`` only -- ``list_object_versions`` is prefix-based, so results
        are filtered to an exact key match; a similarly-prefixed neighboring
        key must never be touched (D36)."""

        version_ids: list[str] = []
        delete_marker_ids: list[str] = []
        paginator = self._client.get_paginator("list_object_versions")  # type: ignore[union-attr]
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            for version in page.get("Versions", []):
                if version.get("Key") == key:
                    version_ids.append(version["VersionId"])
            for marker in page.get("DeleteMarkers", []):
                if marker.get("Key") == key:
                    delete_marker_ids.append(marker["VersionId"])
        return version_ids, delete_marker_ids

    def _delete_all_versions_sync(self, bucket: str, key: str) -> StorageDeleteStatus:
        try:
            version_ids, marker_ids = self._list_exact_key_versions_sync(bucket, key)
        except (ClientError, BotoCoreError):
            return StorageDeleteStatus.UNRESOLVED

        if not version_ids and not marker_ids:
            return StorageDeleteStatus.ALREADY_ABSENT

        all_ids = [*version_ids, *marker_ids]
        try:
            for batch_start in range(0, len(all_ids), DELETE_BATCH_SIZE):
                batch = all_ids[batch_start : batch_start + DELETE_BATCH_SIZE]
                response = self._client.delete_objects(  # type: ignore[union-attr]
                    Bucket=bucket,
                    Delete={
                        "Objects": [{"Key": key, "VersionId": version_id} for version_id in batch],
                        "Quiet": True,
                    },
                )
                if response.get("Errors"):
                    return StorageDeleteStatus.UNRESOLVED
        except (ClientError, BotoCoreError):
            return StorageDeleteStatus.UNRESOLVED

        try:
            remaining_versions, remaining_markers = self._list_exact_key_versions_sync(bucket, key)
        except (ClientError, BotoCoreError):
            return StorageDeleteStatus.UNRESOLVED

        if remaining_versions or remaining_markers:
            return StorageDeleteStatus.UNRESOLVED
        return StorageDeleteStatus.DELETED_NOW


def _is_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    if code in {"404", "NoSuchKey", "NotFound"}:
        return True
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return bool(status_code == 404)


def _identity_matches(identity: _ObjectIdentity, *, content_sha256: str, byte_size: int) -> bool:
    return identity.content_sha256 == content_sha256 and identity.byte_size == byte_size
