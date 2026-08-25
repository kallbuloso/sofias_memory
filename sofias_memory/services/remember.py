"""Remember pure/reusable primitives (SM-513, ADR-0009 SS O).

Run lifecycle for Remember lives entirely in ``pipelines.steps.remember`` and
the shared B5 submission/engine runtime -- this module holds only pure
functions and PostgreSQL-free helpers reused by both the route (work
identity, durable ingress staging) and the pipeline step (loader dispatch
inputs, final storage helpers, B4-legacy semantic-intent compatibility).
Nothing here owns a ``PipelineRun``/``PipelineStep`` transition.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
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
    Forget's *read*-side ``source_storage_path`` requires."""

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
        raise SofiasMemoryError(
            code=ErrorCode.INTERNAL_ERROR,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="Source storage path is invalid.",
        )
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
    :class:`~sofias_memory.pipelines.steps.remember.FinalizeStorageStep`
    treat a file already present (a prior attempt's own write, or a crash
    exactly after the copy but before the ``storage_uri`` commit) as safely
    done rather than re-copying or failing."""

    if not path.is_file():
        return False
    return sha256(path.read_bytes()).hexdigest() == content_sha256


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
