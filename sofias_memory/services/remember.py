"""Remember pure/reusable primitives (SM-513, ADR-0009 SS O).

Run lifecycle for Remember lives entirely in ``pipelines.steps.remember`` and
the shared B5 submission/engine runtime -- this module holds mostly pure
functions and filesystem/PostgreSQL-free helpers reused by both the route
(work identity, durable ingress staging) and the pipeline step (loader
dispatch inputs, final storage helpers, B4-legacy semantic-intent
compatibility). The one exception is :func:`prepare_remember_retry_ingress`
(SM-514), which needs a short, independent read of the authoritative Source
to recover a manual retry's ingress bytes -- it never owns a
``PipelineRun``/``PipelineStep`` transition itself.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.infrastructure.storage.filesystem import (
    SourceStoragePathError as _SourceStoragePathError,
)
from sofias_memory.infrastructure.storage.filesystem import (
    final_storage_content_matches,
    final_storage_directory,
    final_storage_path,
    final_storage_uri,
)
from sofias_memory.infrastructure.storage.filesystem import (
    write_final_storage_bytes as _write_final_storage_bytes,
)
from sofias_memory.infrastructure.storage.port import SourceObjectStorage
from sofias_memory.schemas.common import ErrorCode, JSONValue

DEFAULT_DATASET_SLUG = "main"
REMEMBER_RESULT_METRIC_KEY = "remember_result"

MODE_INGEST = "ingest"
MODE_FULL = "full"
SUPPORTED_MODES = frozenset({MODE_INGEST, MODE_FULL})

SOURCE_KIND_TEXT = "text"
SOURCE_KIND_FILE = "file"
SOURCE_KIND_URL = "url"

TEXT_MIME_TYPE = "text/plain"
TEXT_STORAGE_EXTENSION = ".txt"
UNTOKENIZED_SENTINEL = -1
UNDETERMINED_LANGUAGE = "und"

INGRESS_DIRECTORY_NAME = "_ingress"
INGRESS_ORIGINAL_FILENAME = "original"
INGRESS_FILENAME_METADATA_FILENAME = "filename.txt"

UNSUPPORTED_MODE_ERROR_CODE = "REMEMBER_UNSUPPORTED_MODE"


# ---------------------------------------------------------------------------
# Mode validation (SM-513 SS 3): pure, syntactic, done at the route boundary.
# ---------------------------------------------------------------------------


def validate_remember_mode(mode: str) -> None:
    if mode not in SUPPORTED_MODES:
        supported: list[JSONValue] = list(sorted(SUPPORTED_MODES))
        raise SofiasMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            status_code=HTTPStatus.BAD_REQUEST,
            message="Unsupported remember mode.",
            details={"mode": mode, "supported": supported},
        )


# ---------------------------------------------------------------------------
# Work identity (SM-513 SS 5): wait/confirm/request-id never participate.
# ---------------------------------------------------------------------------


def remember_text_run_input(
    *,
    dataset: str,
    content_sha256: str,
    name: str | None,
    metadata: dict[str, JSONValue],
    session_id: str | None,
    mode: str,
    force: bool,
) -> dict[str, JSONValue]:
    return {
        "source_kind": SOURCE_KIND_TEXT,
        "dataset": dataset,
        "content_sha256": content_sha256,
        "name": name,
        "metadata": metadata,
        "session_id": session_id,
        "mode": mode,
        "force": force,
    }


def remember_file_run_input(
    *,
    dataset: str,
    content_sha256: str,
    filename: str,
    metadata: dict[str, JSONValue],
    session_id: str | None,
    mode: str,
    force: bool,
) -> dict[str, JSONValue]:
    return {
        "source_kind": SOURCE_KIND_FILE,
        "dataset": dataset,
        "content_sha256": content_sha256,
        "filename": filename,
        "metadata": metadata,
        "session_id": session_id,
        "mode": mode,
        "force": force,
    }


def remember_url_run_input(
    *,
    dataset: str,
    url: str,
    metadata: dict[str, JSONValue],
    session_id: str | None,
    mode: str,
    force: bool,
) -> dict[str, JSONValue]:
    return {
        "source_kind": SOURCE_KIND_URL,
        "dataset": dataset,
        "url": url,
        "metadata": metadata,
        "session_id": session_id,
        "mode": mode,
        "force": force,
    }


# ---------------------------------------------------------------------------
# B4 -> B5 semantic intent compatibility (SM-513 SS 6).
#
# B4's own run_input shape included `wait` for FILE/URL (never for TEXT) and
# had no `source_kind` key at all. B5 never includes `wait` and always
# includes `source_kind`. Comparing raw payload_hash would therefore reject a
# same-work retry against a historical B4 run even though nothing semantic
# changed -- so, mirroring Forget's `same_forget_intent` (SM-512), identity
# is compared as a normalized, tolerant intent, never as a raw hash.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RememberSemanticIntent:
    source_kind: str
    dataset: str
    content_identity: str
    name: str | None
    metadata: Mapping[str, JSONValue]
    session_id: str | None
    mode: str
    force: bool


def remember_semantic_intent_from_run_input(
    run_input: Mapping[str, Any] | None,
) -> RememberSemanticIntent | None:
    """Best-effort, tolerant parse. Returns ``None`` for anything that does
    not unambiguously describe a recognizable Remember work item -- callers
    must never treat two ``None`` results as equal (see
    :func:`same_remember_intent`)."""

    if run_input is None:
        return None
    try:
        dataset = str(run_input["dataset"])
        metadata = run_input.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            return None
        session_id = run_input.get("session_id")
        mode = str(run_input["mode"])
        force = bool(run_input.get("force", False))

        source_kind = run_input.get("source_kind")
        if source_kind is None:
            # Legacy B4 shape carried no `source_kind` key at all.
            if "url" in run_input:
                source_kind = SOURCE_KIND_URL
            elif "filename" in run_input:
                source_kind = SOURCE_KIND_FILE
            elif "content_sha256" in run_input:
                source_kind = SOURCE_KIND_TEXT
            else:
                return None

        if source_kind == SOURCE_KIND_URL:
            content_identity = str(run_input["url"])
            name = None
        elif source_kind == SOURCE_KIND_FILE:
            content_identity = str(run_input["content_sha256"])
            name = str(run_input["filename"])
        elif source_kind == SOURCE_KIND_TEXT:
            content_identity = str(run_input["content_sha256"])
            raw_name = run_input.get("name")
            name = str(raw_name) if raw_name is not None else None
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None

    return RememberSemanticIntent(
        source_kind=str(source_kind),
        dataset=dataset,
        content_identity=content_identity,
        name=name,
        metadata=dict(metadata),
        session_id=str(session_id) if session_id is not None else None,
        mode=mode,
        force=force,
    )


def same_remember_intent(
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
) -> bool:
    left_intent = remember_semantic_intent_from_run_input(left)
    right_intent = remember_semantic_intent_from_run_input(right)
    if left_intent is None or right_intent is None:
        return False
    return left_intent == right_intent


# ---------------------------------------------------------------------------
# Durable ingress staging (SM-513 SS 9/10).
#
# Path is keyed only by an application-generated run id (UUID) -- never by
# any client-controlled string -- so no traversal/symlink-escape guard is
# needed the way Source's *final* storage path requires (that one mixes in
# dataset_id/source_id read back from a URI). Every artifact under this root
# belongs to exactly one Remember run and is safe to delete unconditionally
# once that run no longer needs it.
# ---------------------------------------------------------------------------


def ingress_directory(data_directory: Path, *, run_id: UUID) -> Path:
    return data_directory / INGRESS_DIRECTORY_NAME / str(run_id)


def ingress_artifact_path(data_directory: Path, *, run_id: UUID) -> Path:
    return ingress_directory(data_directory, run_id=run_id) / INGRESS_ORIGINAL_FILENAME


def _ingress_filename_path(data_directory: Path, *, run_id: UUID) -> Path:
    return ingress_directory(data_directory, run_id=run_id) / INGRESS_FILENAME_METADATA_FILENAME


def write_ingress_bytes(
    data_directory: Path,
    *,
    run_id: UUID,
    raw_bytes: bytes,
    filename: str | None = None,
) -> None:
    """Atomically stage ``raw_bytes`` under this run's opaque ingress
    directory, durable before the caller returns (SM-513 SS 9). Safe to call
    more than once for the same ``run_id`` (a later call simply replaces the
    artifact) -- callers that must not clobber an already-fetched artifact
    check :func:`ingress_artifact_path` existence first."""

    directory = ingress_directory(data_directory, run_id=run_id)
    directory.mkdir(parents=True, exist_ok=True)
    target_path = directory / INGRESS_ORIGINAL_FILENAME
    temporary_path = directory / f"{INGRESS_ORIGINAL_FILENAME}.tmp"
    temporary_path.write_bytes(raw_bytes)
    temporary_path.replace(target_path)

    if filename is not None:
        filename_path = _ingress_filename_path(data_directory, run_id=run_id)
        filename_temporary_path = filename_path.with_suffix(".tmp")
        filename_temporary_path.write_text(filename, encoding="utf-8")
        filename_temporary_path.replace(filename_path)


def read_ingress_bytes(data_directory: Path, *, run_id: UUID) -> bytes:
    return ingress_artifact_path(data_directory, run_id=run_id).read_bytes()


def read_ingress_filename(data_directory: Path, *, run_id: UUID) -> str | None:
    filename_path = _ingress_filename_path(data_directory, run_id=run_id)
    if not filename_path.exists():
        return None
    return filename_path.read_text(encoding="utf-8")


def ingress_artifact_exists(data_directory: Path, *, run_id: UUID) -> bool:
    return ingress_artifact_path(data_directory, run_id=run_id).exists()


def delete_ingress_artifact(data_directory: Path, *, run_id: UUID) -> None:
    """Best-effort recursive cleanup of this run's ingress directory.

    Never raises: a leftover ingress directory is inert disk usage (never
    referenced by anything but this one run id) rather than a correctness
    hazard, so a failed delete here must never fail the caller (route
    cleanup, worker post-storage-finalization cleanup)."""

    directory = ingress_directory(data_directory, run_id=run_id)
    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Final storage helpers (SM-513 SS 16/17): filesystem I/O only, never called
# from a PipelineStep's persist().
#
# ADR-0011 STORAGE-001 layering correction: the actual primitives now live in
# infrastructure.storage.filesystem (the lower-level implementation boundary
# -- services/pipelines depend on infrastructure, never the reverse). The
# names below are kept as thin compatibility wrappers for call sites
# STORAGE-004 has not migrated to SourceStorageRouter yet.
# `final_storage_directory`/`final_storage_path`/`final_storage_uri`/
# `final_storage_content_matches` are pure (never raise) and are re-exported
# directly (imported at module top); `write_final_storage_bytes` translates
# the infra layer's dependency-free `SourceStoragePathError` back into this
# module's existing `SofiasMemoryError` contract so every current caller/
# test is unaffected.
# ---------------------------------------------------------------------------


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

    try:
        return _write_final_storage_bytes(
            data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
            original_bytes=original_bytes,
        )
    except _SourceStoragePathError as exc:
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Source storage path is invalid.",
        ) from exc


async def prepare_remember_retry_ingress(
    *,
    session_factory: AsyncSessionFactory,
    data_directory: Path,
    original_run_id: UUID,
    original_source_id: UUID | None,
    candidate_run_id: UUID,
    source_kind: str,
    settings: Settings,
    storage: SourceObjectStorage | None = None,
) -> bool:
    """SM-514 SS 34/35: recover durable bytes for a manual-retry child run's
    own ``candidate_run_id``-keyed ingress artifact, using only what the
    ORIGINAL run left behind. Never called inside a PostgreSQL transaction
    that must stay short (SS 36) -- any read here is its own short,
    independent unit of work.

    Returns ``True`` when the retry can proceed: durable bytes were staged
    for the candidate (TEXT/FILE, or URL content already acquired by the
    original), or -- URL only, and only when nothing was ever acquired --
    nothing needs staging because the worker will fetch fresh. Returns
    ``False`` only when TEXT/FILE has nothing durable to redo step 1 with --
    the caller must then fail the retry closed rather than create a child
    doomed to `REMEMBER_INGRESS_MISSING`.

    ``storage`` is an injection point mirroring
    ``RememberPipelineResources.source_storage`` (``pipelines/steps/remember.py``):
    production call sites leave it ``None`` and a ``SourceStorageRouter`` is
    built from ``settings``; tests inject a fake/Stubbed
    :class:`~sofias_memory.infrastructure.storage.port.SourceObjectStorage` to
    prove Case 2 routes by scheme without needing live S3 (STORAGE-009).
    """

    # Case 1 -- the original run's own ingress directory is still present
    # (never reached/finalized, or the original failed before
    # finalize_storage's cleanup ran). Applies to ALL source kinds
    # (SM-514 SS 43): once a URL's bytes were durably acquired, a retry must
    # reuse them rather than silently refetch and risk observing different
    # content from a server whose response changed between attempts.
    if ingress_artifact_exists(data_directory, run_id=original_run_id):
        raw_bytes = read_ingress_bytes(data_directory, run_id=original_run_id)
        # URL's own filename is derived from the fetch response, not from
        # `run_input` -- it only lives in this ingress metadata, so it must
        # be carried over too, or the retry's own loader dispatch would see
        # no extension at all (TEXT/FILE never write this metadata; None is
        # the correct no-op for them).
        filename = read_ingress_filename(data_directory, run_id=original_run_id)
        write_ingress_bytes(
            data_directory, run_id=candidate_run_id, raw_bytes=raw_bytes, filename=filename
        )
        return True

    if source_kind == SOURCE_KIND_URL and original_source_id is None:
        # Never acquired (no ingress ever staged, and no Source was ever
        # committed by prepare_and_ingest) -- no acquired-content identity
        # exists anywhere to reuse, so a fresh fetch is the only option and
        # is exactly what SM-514 SS 35 case 3 permits.
        return True

    # Case 2 -- original ingress is gone (finalize_storage already
    # succeeded and cleaned it up): recover bytes from the authoritative
    # Source's own final storage. Reads route by `storage_uri` scheme via
    # the storage router (ADR-0011 D4/D13), never by `STORAGE_BACKEND` --
    # this recovers identically whether the original final object is a
    # legacy filesystem write or a real S3 object (STORAGE-009 live-MinIO
    # finding: this case previously called the filesystem-only
    # `source_storage_path` helper directly and hard-failed closed with
    # `INVALID_REQUEST` for any `s3://` Source instead of recovering).
    if original_source_id is not None:
        async with PostgresUnitOfWork(session_factory) as uow:
            source = await uow.sources.get_by_id(original_source_id)
            storage_uri = source.storage_uri if source is not None else None
            dataset_id = source.dataset_id if source is not None else None
            content_sha256 = source.content_sha256 if source is not None else None
            byte_size = source.byte_size if source is not None else None
        if (
            storage_uri is not None
            and dataset_id is not None
            and content_sha256 is not None
            and byte_size is not None
        ):
            from sofias_memory.infrastructure.storage.port import SourceStorageError
            from sofias_memory.infrastructure.storage.router import SourceStorageRouter

            router = storage or SourceStorageRouter(settings)
            recovered_bytes: bytes | None
            try:
                recovered_bytes = await router.read(
                    dataset_id=dataset_id,
                    source_id=original_source_id,
                    storage_uri=storage_uri,
                    expected_byte_size=byte_size,
                    expected_content_sha256=content_sha256,
                    max_bytes=settings.max_source_size_mb * 1024 * 1024,
                )
            except SourceStorageError:
                recovered_bytes = None
            if recovered_bytes is not None:
                write_ingress_bytes(
                    data_directory, run_id=candidate_run_id, raw_bytes=recovered_bytes
                )
                return True

    # Case 4 -- TEXT/FILE with neither a live ingress artifact nor
    # recoverable final storage: no durable bytes exist anywhere to redo
    # step 1 with. Fail closed rather than create a doomed child run.
    return False


def dataset_not_found_error(dataset_slug: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Dataset does not exist.",
        details={"dataset": dataset_slug},
    )


def source_name(*, name: str | None, content_sha256: str) -> str:
    return name or f"text-{content_sha256[:12]}"


def document_metadata(
    *,
    metadata: dict[str, JSONValue],
    session_id: str | None,
) -> dict[str, JSONValue]:
    result = dict(metadata)
    if session_id is not None:
        result["session_id"] = session_id
    return result


__all__ = [
    "DEFAULT_DATASET_SLUG",
    "MODE_FULL",
    "MODE_INGEST",
    "REMEMBER_RESULT_METRIC_KEY",
    "SOURCE_KIND_FILE",
    "SOURCE_KIND_TEXT",
    "SOURCE_KIND_URL",
    "SUPPORTED_MODES",
    "TEXT_MIME_TYPE",
    "TEXT_STORAGE_EXTENSION",
    "UNDETERMINED_LANGUAGE",
    "UNSUPPORTED_MODE_ERROR_CODE",
    "UNTOKENIZED_SENTINEL",
    "RememberSemanticIntent",
    "dataset_not_found_error",
    "delete_ingress_artifact",
    "document_metadata",
    "final_storage_content_matches",
    "final_storage_directory",
    "final_storage_path",
    "final_storage_uri",
    "ingress_artifact_exists",
    "ingress_artifact_path",
    "ingress_directory",
    "prepare_remember_retry_ingress",
    "read_ingress_bytes",
    "read_ingress_filename",
    "remember_file_run_input",
    "remember_semantic_intent_from_run_input",
    "remember_text_run_input",
    "remember_url_run_input",
    "same_remember_intent",
    "source_name",
    "validate_remember_mode",
    "write_final_storage_bytes",
    "write_ingress_bytes",
]
