"""Remember pipeline steps (SM-513, ADR-0009 SS O).

Four fixed steps, always present regardless of ``source_kind``/``mode``
(never a dynamically-built ``PipelineDefinition``, SM-513 SS 15):

1. ``prepare_and_ingest`` -- ``execute`` reads/fetches the durable ingress
   artifact and runs B4's unchanged loader dispatch (never writing
   anything); ``persist`` is the authoritative dedup/version/Source/Document
   PostgreSQL mutation, entirely inside the engine's own transaction.
2. ``finalize_storage`` -- ``execute`` copies the ingress artifact to the
   source's final storage path (external filesystem I/O, skipped entirely
   on a deduplicated reuse); ``persist`` records the resulting
   ``storage_uri`` once, idempotently.
3. ``cognify`` -- reuses ``CognifyService.prepare_batch``/``persist_batch``
   (SM-510) directly against the ingest step's own Source, targeted with
   ``source_ids=[source_id], rebuild=False``. Deterministic no-op for
   ``mode=ingest``. Never submits a nested ``PipelineType.COGNIFY`` run --
   SM-513's central invariant (SS 2).
4. ``finalize_result`` -- aggregates every prior step's safe output into
   ``run.metrics[REMEMBER_RESULT_METRIC_KEY]``.

Graph projection follows SM-510's Cognify semantics exactly (SS 27): the
``cognify`` step's ``persist`` writes ``graph_outbox`` rows and nothing here
drains them -- SM-506's autonomous consumer converges Neo4j independently,
same as a direct Cognify run. No extra convergence barrier is added, unlike
Improve's ``FinalConvergenceStep`` (SM-511), because SM-513's own backlog
text does not call for one.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineType, SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres.models import Document, PipelineRun, Source
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.infrastructure.storage import (
    FinalizeResult,
    SourceObjectStorage,
    SourceStorageConflictError,
    SourceStorageError,
    SourceStorageRouter,
)
from sofias_memory.loaders.text import (
    PreparedText,
    TextFileLoadError,
    canonical_storage_extension_for_mime_type,
    prepare_text_content,
)
from sofias_memory.loaders.text import prepare_text_file_content as _prepare_text_file_content
from sofias_memory.loaders.url import fetch_https_url
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import (
    PermanentPipelineStepError,
    RetryablePipelineStepError,
)
from sofias_memory.pipelines.registry import (
    CancellationRecoveryMode,
    PipelineDefinition,
    PipelineStepDefinition,
    StepResult,
    no_op_compensate,
)
from sofias_memory.pipelines.steps.cognify import CognifySourceProcessor
from sofias_memory.services.cognify import CognifyUnitOfWork
from sofias_memory.services.remember import (
    REMEMBER_RESULT_METRIC_KEY,
    SOURCE_KIND_FILE,
    SOURCE_KIND_TEXT,
    SOURCE_KIND_URL,
    TEXT_MIME_TYPE,
    TEXT_STORAGE_EXTENSION,
    UNDETERMINED_LANGUAGE,
    UNTOKENIZED_SENTINEL,
    delete_ingress_artifact,
    document_metadata,
    final_storage_path,
    ingress_artifact_exists,
    read_ingress_bytes,
    read_ingress_filename,
    source_name,
    write_ingress_bytes,
)

REMEMBER_RESOURCES_RESOURCE = "remember_resources"
"""``PipelineContext.resources`` key holding :class:`RememberPipelineResources`."""

PREPARE_AND_INGEST_STEP = "prepare_and_ingest"
FINALIZE_STORAGE_STEP = "finalize_storage"
COGNIFY_STEP = "cognify"
FINALIZE_RESULT_STEP = "finalize_result"

PREPARE_AND_INGEST_DEFINITION_ID = "remember.prepare_and_ingest.v1"
FINALIZE_STORAGE_DEFINITION_ID = "remember.finalize_storage.v1"
COGNIFY_DEFINITION_ID = "remember.cognify.v1"
FINALIZE_RESULT_DEFINITION_ID = "remember.finalize_result.v1"

REMEMBER_DEPENDENCY_ERROR_CODE = "REMEMBER_DEPENDENCY_UNAVAILABLE"
REMEMBER_CONTENT_ERROR_CODE = "REMEMBER_CONTENT_REJECTED"
REMEMBER_URL_ERROR_CODE = "REMEMBER_URL_REJECTED"
REMEMBER_SCOPE_ERROR_CODE = "REMEMBER_RUN_SCOPE_INVALID"
REMEMBER_INGRESS_MISSING_ERROR_CODE = "REMEMBER_INGRESS_MISSING"
REMEMBER_BATCH_MISSING_ERROR_CODE = "REMEMBER_PREPARED_STATE_MISSING"
REMEMBER_TARGET_MISSING_ERROR_CODE = "REMEMBER_TARGET_MISSING"
REMEMBER_STORAGE_CONFLICT_ERROR_CODE = "REMEMBER_STORAGE_CONTENT_CONFLICT"
REMEMBER_RESOURCE_MISSING_ERROR_CODE = "REMEMBER_RESOURCE_MISSING"

REMEMBER_DEPENDENCY_ERROR_MESSAGE = "A Remember dependency was unavailable."
REMEMBER_CONTENT_ERROR_MESSAGE = "Remember content could not be processed."
REMEMBER_URL_ERROR_MESSAGE = "Remember URL could not be fetched."
REMEMBER_SCOPE_ERROR_MESSAGE = "Remember run is not scoped to a dataset."
REMEMBER_INGRESS_MISSING_MESSAGE = "Remember durable ingress artifact is missing."
REMEMBER_BATCH_MISSING_MESSAGE = "Remember prepared state is not available for this run."
REMEMBER_TARGET_MISSING_MESSAGE = "Remember target no longer exists."
REMEMBER_STORAGE_CONFLICT_MESSAGE = "Remember final storage already holds different content."
REMEMBER_RESOURCE_MISSING_MESSAGE = "Remember processing resources are not configured."

STAGED_STATE_MAX_AGE_SECONDS = 900.0
"""Same hygiene bound as Cognify's ``ProcessSourcesStep`` (SM-510): an
abandonment sweep on each step's own private, process-local, ``run_id``-keyed
cache, never a source of correctness."""


@dataclass(frozen=True, slots=True)
class RememberPipelineResources:
    settings: Settings
    cognify_service: CognifySourceProcessor
    url_transport: httpx.AsyncBaseTransport | None = None
    url_resolver: Any = None
    """Test-only injection points mirroring ``fetch_https_url``'s own
    ``transport``/``resolver`` parameters (never set in production wiring --
    ``app.build_pipeline_resources`` always leaves both ``None`` so the real
    transport and real DNS resolution are used). Let integration tests
    exercise the real worker's URL-fetch step against a local
    ``httpx.MockTransport`` and a fake DNS resolver instead of the public
    internet, without adding a second URL-fetching code path."""
    source_storage: SourceObjectStorage | None = None
    """Injection point mirroring Cognify's own ``source_storage`` parameter
    (STORAGE-003): ``None`` in production wiring, where a
    ``SourceStorageRouter`` is constructed lazily per call in
    :func:`_source_storage` -- never eagerly here, so a settings-only
    resource never depends on S3 configuration being present."""


def _resources(context: PipelineContext) -> RememberPipelineResources:
    resource = context.resources.get(REMEMBER_RESOURCES_RESOURCE)
    if resource is None:
        raise PermanentPipelineStepError(
            REMEMBER_RESOURCE_MISSING_ERROR_CODE, REMEMBER_RESOURCE_MISSING_MESSAGE
        )
    return cast(RememberPipelineResources, resource)


def _source_storage(resources: RememberPipelineResources) -> SourceObjectStorage:
    return resources.source_storage or SourceStorageRouter(resources.settings)


def _translate_storage_error(
    exc: SourceStorageError,
) -> PermanentPipelineStepError | RetryablePipelineStepError:
    if isinstance(exc, SourceStorageConflictError):
        return PermanentPipelineStepError(
            REMEMBER_STORAGE_CONFLICT_ERROR_CODE, REMEMBER_STORAGE_CONFLICT_MESSAGE
        )
    return RetryablePipelineStepError(
        REMEMBER_DEPENDENCY_ERROR_CODE, REMEMBER_DEPENDENCY_ERROR_MESSAGE
    )


@dataclass(frozen=True, slots=True)
class _PreparedIngest:
    name: str | None
    mime_type: str
    storage_extension: str
    text: PreparedText


async def _retry_source_reuse(
    uow: PostgresUnitOfWork,
    *,
    run: PipelineRun,
    dataset_id: UUID,
    content_sha256: str,
) -> Source | None:
    """SM-514 SS 39/40: if ``run`` is a manual retry (``retry_of_run_id`` set)
    and the original run already committed a Source for this exact
    dataset+content identity, that Source must be reused verbatim -- never
    re-derived from ``force``/dedup logic, which would otherwise create a
    duplicate version purely because a different ``PipelineRun.id`` is now
    doing the work. Whitelisted revalidation only (never trusts the original
    run's persisted output blindly): re-reads the Source fresh from
    PostgreSQL and re-checks dataset/content identity match."""

    if run.retry_of_run_id is None:
        return None
    original_run = await uow.pipeline_runs.get_by_id(run.retry_of_run_id)
    if original_run is None or original_run.source_id is None:
        return None
    candidate = await uow.sources.get_by_id(original_run.source_id)
    if (
        candidate is not None
        and candidate.dataset_id == dataset_id
        and candidate.content_sha256 == content_sha256
    ):
        return candidate
    return None


# ---------------------------------------------------------------------------
# 1. prepare_and_ingest -- execute reads/fetches + loads; persist is the
#    authoritative dedup/version/Source/Document mutation.
# ---------------------------------------------------------------------------


class PrepareAndIngestStep:
    """B4's ingest logic, split across the two ADR-0009 SS O phases.

    The prepared ingest (decoded/normalized text plus identity hashes) is
    handed between ``execute`` and ``persist`` through a private,
    process-local cache on this step instance, keyed by
    ``PipelineContext.run_id`` -- the same pattern Cognify's
    ``ProcessSourcesStep`` established (SM-510): sound because the engine
    calls ``persist`` in the very next statement after this same attempt's
    ``execute`` returns, and losing the cache (crash, fencing loss, the
    age-based sweep) costs at most redoing ``execute``'s work, never
    correctness -- the durable ingress artifact on disk is what actually
    makes this recomputable, not the cache.
    """

    def __init__(self) -> None:
        self._staged: dict[UUID, tuple[float, _PreparedIngest]] = {}

    async def execute(self, context: PipelineContext) -> StepResult:
        resources = _resources(context)
        data_directory = resources.settings.data_directory
        run_input = context.run_input
        source_kind = str(run_input.get("source_kind"))

        if source_kind == SOURCE_KIND_URL:
            prepared = await self._acquire_url(context, resources)
        elif source_kind == SOURCE_KIND_FILE:
            if not ingress_artifact_exists(data_directory, run_id=context.run_id):
                raise PermanentPipelineStepError(
                    REMEMBER_INGRESS_MISSING_ERROR_CODE, REMEMBER_INGRESS_MISSING_MESSAGE
                )
            raw_bytes = read_ingress_bytes(data_directory, run_id=context.run_id)
            filename = str(run_input.get("filename"))
            prepared = self._prepare_file(filename, raw_bytes)
        elif source_kind == SOURCE_KIND_TEXT:
            if not ingress_artifact_exists(data_directory, run_id=context.run_id):
                raise PermanentPipelineStepError(
                    REMEMBER_INGRESS_MISSING_ERROR_CODE, REMEMBER_INGRESS_MISSING_MESSAGE
                )
            raw_bytes = read_ingress_bytes(data_directory, run_id=context.run_id)
            content = raw_bytes.decode("utf-8")
            prepared_text = prepare_text_content(content)
            raw_name = run_input.get("name")
            prepared = _PreparedIngest(
                name=str(raw_name) if raw_name is not None else None,
                mime_type=TEXT_MIME_TYPE,
                storage_extension=TEXT_STORAGE_EXTENSION,
                text=prepared_text,
            )
        else:
            raise PermanentPipelineStepError(
                REMEMBER_SCOPE_ERROR_CODE, REMEMBER_SCOPE_ERROR_MESSAGE
            )

        self._evict_abandoned()
        self._staged[context.run_id] = (time.monotonic(), prepared)
        return StepResult(
            output={
                "source_kind": source_kind,
                "content_sha256": prepared.text.content_sha256,
                "normalized_sha256": prepared.text.normalized_sha256,
                "byte_size": prepared.text.byte_size,
                "mime_type": prepared.mime_type,
                "storage_extension": prepared.storage_extension,
                "name": prepared.name,
            }
        )

    async def _acquire_url(
        self, context: PipelineContext, resources: RememberPipelineResources
    ) -> _PreparedIngest:
        data_directory = resources.settings.data_directory
        if not ingress_artifact_exists(data_directory, run_id=context.run_id):
            url = str(context.run_input.get("url"))
            max_bytes = resources.settings.max_source_size_mb * 1024 * 1024
            try:
                fetched = await fetch_https_url(
                    url,
                    max_bytes=max_bytes,
                    transport=resources.url_transport,
                    resolver=resources.url_resolver,
                )
            except DependencyUnavailableError as exc:
                raise RetryablePipelineStepError(
                    REMEMBER_DEPENDENCY_ERROR_CODE, REMEMBER_DEPENDENCY_ERROR_MESSAGE
                ) from exc
            except SofiasMemoryError as exc:
                raise PermanentPipelineStepError(
                    REMEMBER_URL_ERROR_CODE, REMEMBER_URL_ERROR_MESSAGE
                ) from exc
            # Durable before this attempt's execute proceeds further (SM-513
            # SS 29): a downstream crash/retry never re-fetches (and cannot
            # silently observe different remote content) once this line
            # returns -- only a crash strictly *before* this write can ever
            # cause a refetch, which is the documented acceptable case.
            write_ingress_bytes(
                data_directory,
                run_id=context.run_id,
                raw_bytes=fetched.body,
                filename=fetched.filename,
            )

        raw_bytes = read_ingress_bytes(data_directory, run_id=context.run_id)
        filename = read_ingress_filename(data_directory, run_id=context.run_id) or "remote"
        return self._prepare_file(filename, raw_bytes)

    def _prepare_file(self, filename: str, raw_bytes: bytes) -> _PreparedIngest:
        try:
            prepared_file = _prepare_text_file_content(filename, raw_bytes)
        except TextFileLoadError as exc:
            raise PermanentPipelineStepError(
                REMEMBER_CONTENT_ERROR_CODE, REMEMBER_CONTENT_ERROR_MESSAGE
            ) from exc
        return _PreparedIngest(
            name=prepared_file.original_filename,
            mime_type=prepared_file.mime_type,
            storage_extension=prepared_file.storage_extension,
            text=prepared_file.text,
        )

    def _evict_abandoned(self) -> None:
        cutoff = time.monotonic() - STAGED_STATE_MAX_AGE_SECONDS
        for run_id in [
            run_id for run_id, (staged_at, _) in self._staged.items() if staged_at < cutoff
        ]:
            del self._staged[run_id]

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        staged = self._staged.pop(context.run_id, None)
        if staged is None:
            # Structurally impossible with the current engine (persist is
            # only ever reached immediately after this same attempt's
            # execute succeeded) -- fail loudly, mirroring Cognify's
            # ProcessSourcesStep precedent.
            raise PermanentPipelineStepError(
                REMEMBER_BATCH_MISSING_ERROR_CODE, REMEMBER_BATCH_MISSING_MESSAGE
            )
        _, prepared = staged

        if context.dataset_id is None:
            raise PermanentPipelineStepError(
                REMEMBER_SCOPE_ERROR_CODE, REMEMBER_SCOPE_ERROR_MESSAGE
            )
        dataset = await uow.datasets.get_by_id_for_update(context.dataset_id)
        if dataset is None or dataset.status != DatasetStatus.ACTIVE:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )

        # Fetched early (rather than only at the end, where an earlier
        # revision of this step used it purely to set run.source_id) so a
        # manual-retry run (SM-514 SS 39/40) can be recognized BEFORE the
        # dedup/force decision below: retry_of_run_id is never None for a
        # run created by POST /runs/{id}/retry.
        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )

        force = bool(context.run_input.get("force", False))
        content_sha256 = prepared.text.content_sha256
        existing = await uow.sources.get_latest_by_content_hash(
            dataset_id=dataset.id, content_sha256=content_sha256
        )

        retry_source = await _retry_source_reuse(
            uow, run=run, dataset_id=dataset.id, content_sha256=content_sha256
        )
        source: Source | None
        if retry_source is not None:
            # SM-514 SS 39: a manual retry redoes step 1 from scratch, but
            # must never create ANOTHER Source version just because it is a
            # different PipelineRun -- the original's own committed Source
            # (proven, not guessed: same dataset, same content hash) is
            # reused unconditionally, regardless of `force`. `force` governs
            # "is this new content relative to what existed before THIS
            # submission" -- a retry is not a new submission.
            deduplicated = True
            source = retry_source
        else:
            deduplicated = existing is not None and not force
            source = existing if deduplicated else None
        source_kind = SourceKind(str(context.run_input.get("source_kind")))
        display_name = source_name(name=prepared.name, content_sha256=content_sha256)
        metadata = dict(context.run_input.get("metadata") or {})
        session_id_raw = context.run_input.get("session_id")
        session_id = str(session_id_raw) if session_id_raw is not None else None

        if deduplicated:
            assert source is not None  # noqa: S101 - deduplicated implies a target source
            documents = await uow.documents.list_for_source(source.id)
            if not documents:
                raise PermanentPipelineStepError(
                    REMEMBER_TARGET_MISSING_ERROR_CODE,
                    "Stored source has no normalized document.",
                )
            document = documents[-1]
        else:
            source = await uow.sources.add(
                Source(
                    dataset_id=dataset.id,
                    kind=source_kind,
                    name=display_name,
                    mime_type=prepared.mime_type,
                    original_uri=str(context.run_input["url"])
                    if source_kind == SourceKind.URL
                    else None,
                    storage_uri=None,
                    content_sha256=content_sha256,
                    normalized_sha256=prepared.text.normalized_sha256,
                    byte_size=prepared.text.byte_size,
                    metadata_=metadata,
                    status=SourceStatus.PENDING,
                    version=1 if existing is None else existing.version + 1,
                )
            )
            document = await uow.documents.add(
                Document(
                    dataset_id=dataset.id,
                    source_id=source.id,
                    generation=dataset.active_generation,
                    title=display_name,
                    language=UNDETERMINED_LANGUAGE,
                    normalized_text=prepared.text.normalized_text,
                    text_sha256=prepared.text.normalized_sha256,
                    token_count=UNTOKENIZED_SENTINEL,
                    metadata_=document_metadata(metadata=metadata, session_id=session_id),
                    is_active=True,
                )
            )

        run.source_id = source.id

        result.output.update(
            {
                "dataset_id": str(dataset.id),
                "source_id": str(source.id),
                "document_id": str(document.id),
                "deduplicated": deduplicated,
                "version": source.version,
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id, None)
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 2. finalize_storage -- external filesystem move/copy, then a single,
#    idempotent storage_uri persist.
# ---------------------------------------------------------------------------


def _locate_verified_legacy_final(
    data_directory: Path,
    *,
    dataset_id: UUID,
    source_id: UUID,
    mime_type: str,
    expected_byte_size: int,
    expected_content_sha256: str,
) -> bytes | None:
    """ADR-0011 B1/D35: the *only* legacy-final lookup this step ever
    performs -- deterministic-only, no glob, no recursive search, no
    client-supplied filename. The legacy extension is derived from
    ``Source.mime_type`` via the same centralized mapping STORAGE-001
    established (never from the upload-derived ``storage_extension``, which
    may legitimately differ in edge cases the mapping already resolves
    identically for every kind this pipeline supports today).

    Returns the verified legacy bytes, or ``None`` when nothing recoverable
    exists at the exact deterministic legacy location (B1 case 4, or an
    unmappable ``mime_type`` -- both fail closed the same way, via the
    caller's own ``REMEMBER_INGRESS_MISSING`` error). A file present at the
    exact identity but failing size/hash validation (B1 case 5: wrong
    identity) is never uploaded, overwritten, or deleted -- it is simply
    treated as unusable, and the caller fails closed exactly as case 4.
    """

    try:
        legacy_extension = canonical_storage_extension_for_mime_type(mime_type)
    except ValueError:
        return None

    legacy_path = final_storage_path(
        data_directory,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=legacy_extension,
    )
    if not legacy_path.is_file():
        return None

    legacy_bytes = legacy_path.read_bytes()
    if len(legacy_bytes) != expected_byte_size:
        return None
    if sha256(legacy_bytes).hexdigest() != expected_content_sha256:
        return None
    return legacy_bytes


class FinalizeStorageStep:
    """AMBIGUOUS cancellation recovery (mirrors Forget's
    ``StorageDeletionStep``, SM-512): a PostgreSQL-only reconciliation
    callback cannot prove whether an orphaned attempt already wrote the
    final object before crashing. The write itself is fully idempotent for
    both backends (a target already present with the expected content hash
    is treated as done, a wrong-content collision fails safe), so a retry
    after any crash always converges correctly regardless of how recovery
    classifies it.

    ADR-0011 B1: if the durable ``_ingress`` artifact is absent AND the
    deterministic target does not yet match (the only way this combination
    happens is a prior attempt that removed ingress before ever persisting
    ``storage_uri``, then had ``STORAGE_BACKEND`` flipped to ``s3`` before
    this retry), the *same* retry recovers via the legacy filesystem final
    object left behind by the earlier filesystem-backend attempt --
    verified byte-for-byte against this run's own persisted identity, never
    re-fetched, never re-derived. This is Remember-owned recovery on the
    existing retry path, not a separate migration mechanism (D9/D35): the
    legacy local copy is deliberately left in place afterwards -- a
    redundant copy after a successful repoint is safe and expected, and its
    cleanup is explicitly STORAGE-006's job, not this step's.
    """

    async def execute(self, context: PipelineContext) -> StepResult:
        resources = _resources(context)
        upstream = context.step_outputs.get(PREPARE_AND_INGEST_STEP)
        if upstream is None:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )
        if upstream.get("deduplicated"):
            # Reused source keeps its own already-finalized storage --
            # nothing to write, nothing to persist.
            return StepResult(output={"storage_status": "skipped_dedup", "storage_written": False})

        data_directory = resources.settings.data_directory
        dataset_id = UUID(str(upstream["dataset_id"]))
        source_id = UUID(str(upstream["source_id"]))
        storage_extension = str(upstream["storage_extension"])
        content_sha256 = str(upstream["content_sha256"])
        byte_size = int(upstream["byte_size"])
        mime_type = str(upstream["mime_type"])
        storage = _source_storage(resources)

        target_uri = storage.deterministic_uri(
            dataset_id=dataset_id, source_id=source_id, storage_extension=storage_extension
        )
        try:
            already_matches = await storage.verify(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=target_uri,
                content_sha256=content_sha256,
            )
        except SourceStorageError as exc:
            raise _translate_storage_error(exc) from exc

        if already_matches:
            # Best-effort GC only after the final storage is confirmed to
            # hold the expected content -- never before, and never
            # authoritative (identical placement to the pre-STORAGE-004
            # filesystem-only version of this step).
            delete_ingress_artifact(data_directory, run_id=context.run_id)
            return StepResult(
                output={
                    "storage_status": "already_present",
                    "storage_written": False,
                    "storage_uri": target_uri,
                }
            )

        if ingress_artifact_exists(data_directory, run_id=context.run_id):
            raw_bytes = read_ingress_bytes(data_directory, run_id=context.run_id)
            finalize_result = await self._finalize(
                storage,
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
                original_bytes=raw_bytes,
            )
            delete_ingress_artifact(data_directory, run_id=context.run_id)
            status = "already_present" if finalize_result.already_present else "written"
            return StepResult(
                output={
                    "storage_status": status,
                    "storage_written": not finalize_result.already_present,
                    "storage_uri": finalize_result.storage_uri,
                }
            )

        if resources.settings.storage_backend != "s3":
            # No B1 recovery path exists for the filesystem backend: an
            # absent target with absent ingress is unrecoverable by
            # definition (there is no separate "legacy" location to fall
            # back to when the deterministic target already *is* the
            # filesystem location).
            raise PermanentPipelineStepError(
                REMEMBER_INGRESS_MISSING_ERROR_CODE, REMEMBER_INGRESS_MISSING_MESSAGE
            )

        # ADR-0011 B1: ingress absent, S3 target absent/non-matching --
        # recover from the legacy filesystem final object left behind by an
        # earlier filesystem-backend attempt, verified against this run's
        # own persisted identity only.
        legacy_bytes = _locate_verified_legacy_final(
            data_directory,
            dataset_id=dataset_id,
            source_id=source_id,
            mime_type=mime_type,
            expected_byte_size=byte_size,
            expected_content_sha256=content_sha256,
        )
        if legacy_bytes is None:
            raise PermanentPipelineStepError(
                REMEMBER_INGRESS_MISSING_ERROR_CODE, REMEMBER_INGRESS_MISSING_MESSAGE
            )

        finalize_result = await self._finalize(
            storage,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_extension=storage_extension,
            original_bytes=legacy_bytes,
        )
        # Deliberately NOT deleting the legacy local file here: crash safety
        # requires the repoint to be durably persisted first (persist() runs
        # strictly after this method returns), and that local cleanup is
        # STORAGE-006's job (ADR-0011 D9/D35), not this step's.
        return StepResult(
            output={
                "storage_status": "recovered_legacy",
                "storage_written": True,
                "storage_uri": finalize_result.storage_uri,
            }
        )

    async def _finalize(
        self,
        storage: SourceObjectStorage,
        *,
        dataset_id: UUID,
        source_id: UUID,
        storage_extension: str,
        original_bytes: bytes,
    ) -> FinalizeResult:
        try:
            return await storage.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=storage_extension,
                original_bytes=original_bytes,
            )
        except SourceStorageError as exc:
            raise _translate_storage_error(exc) from exc

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        if result.output.get("storage_status") == "skipped_dedup":
            return
        upstream = context.step_outputs.get(PREPARE_AND_INGEST_STEP, {})
        source_id = UUID(str(upstream["source_id"]))
        source = await uow.sources.get_by_id_for_update(source_id)
        if source is None:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )
        if source.storage_uri is None:
            storage_uri = result.output.get("storage_uri")
            if not isinstance(storage_uri, str) or not storage_uri:
                # Structurally impossible given execute()'s own contract --
                # every non-skipped-dedup branch always sets storage_uri.
                raise PermanentPipelineStepError(
                    REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
                )
            source.storage_uri = storage_uri

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 3. cognify -- reuses CognifyService.prepare_batch/persist_batch directly.
#    Zero nested COGNIFY PipelineRun (SM-513 SS 2).
# ---------------------------------------------------------------------------


class CognifyCompositionStep:
    def __init__(self) -> None:
        self._staged: dict[UUID, tuple[float, Any]] = {}

    async def execute(self, context: PipelineContext) -> StepResult:
        mode = str(context.run_input.get("mode"))
        if mode != "full":
            return StepResult(
                output={
                    "cognify_skipped": True,
                    "sources_processed": 0,
                    "chunks": 0,
                    "entities": 0,
                    "relations": 0,
                }
            )

        resources = _resources(context)
        upstream = context.step_outputs.get(PREPARE_AND_INGEST_STEP)
        if upstream is None or context.dataset_id is None:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )
        source_id = UUID(str(upstream["source_id"]))

        try:
            batch = await resources.cognify_service.prepare_batch(
                dataset_id=context.dataset_id,
                source_ids=[source_id],
                rebuild=False,
            )
        except DependencyUnavailableError as exc:
            raise RetryablePipelineStepError(
                REMEMBER_DEPENDENCY_ERROR_CODE, REMEMBER_DEPENDENCY_ERROR_MESSAGE
            ) from exc
        except SofiasMemoryError as exc:
            raise PermanentPipelineStepError(
                REMEMBER_CONTENT_ERROR_CODE, REMEMBER_CONTENT_ERROR_MESSAGE
            ) from exc

        self._evict_abandoned()
        self._staged[context.run_id] = (time.monotonic(), batch)
        planned = batch.planned_outcome()
        return StepResult(
            output={
                "cognify_skipped": False,
                "sources_processed": planned.sources_processed,
                "chunks": planned.chunks,
                "entities": planned.entities,
                "relations": planned.relations,
            }
        )

    def _evict_abandoned(self) -> None:
        cutoff = time.monotonic() - STAGED_STATE_MAX_AGE_SECONDS
        for run_id in [
            run_id for run_id, (staged_at, _) in self._staged.items() if staged_at < cutoff
        ]:
            del self._staged[run_id]

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        if result.output.get("cognify_skipped"):
            return
        staged = self._staged.pop(context.run_id, None)
        if staged is None:
            raise PermanentPipelineStepError(
                REMEMBER_BATCH_MISSING_ERROR_CODE, REMEMBER_BATCH_MISSING_MESSAGE
            )
        _, batch = staged
        resources = _resources(context)
        outcome = await resources.cognify_service.persist_batch(cast(CognifyUnitOfWork, uow), batch)
        result.output.update(
            {
                "sources_processed": outcome.sources_processed,
                "chunks": outcome.chunks,
                "entities": outcome.entities,
                "relations": outcome.relations,
            }
        )

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        self._staged.pop(context.run_id, None)
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# 4. finalize_result -- pure aggregation, PostgreSQL-only.
# ---------------------------------------------------------------------------


class FinalizeResultStep:
    async def execute(self, context: PipelineContext) -> StepResult:
        del context
        return StepResult(output={})

    async def persist(
        self,
        context: PipelineContext,
        result: StepResult,
        uow: PostgresUnitOfWork,
    ) -> None:
        ingest_output = context.step_outputs.get(PREPARE_AND_INGEST_STEP, {})
        cognify_output = context.step_outputs.get(COGNIFY_STEP, {})

        run = await uow.pipeline_runs.get_by_id_for_update(context.run_id)
        if run is None:
            raise PermanentPipelineStepError(
                REMEMBER_TARGET_MISSING_ERROR_CODE, REMEMBER_TARGET_MISSING_MESSAGE
            )

        remember_result = {
            "dataset_id": ingest_output.get("dataset_id"),
            "source_id": ingest_output.get("source_id"),
            "document_id": ingest_output.get("document_id"),
            "content_hash": ingest_output.get("content_sha256"),
            "chunks": int(cognify_output.get("chunks", 0) or 0),
            "entities": int(cognify_output.get("entities", 0) or 0),
            "relations": int(cognify_output.get("relations", 0) or 0),
            "deduplicated": bool(ingest_output.get("deduplicated", False)),
        }
        result.output.update(remember_result)
        run.metrics = {**run.metrics, REMEMBER_RESULT_METRIC_KEY: remember_result}

    async def compensate(self, context: PipelineContext, result: StepResult) -> None:
        no_op_compensate(context, result)


# ---------------------------------------------------------------------------
# Step input derivation (ADR-0009 SS 11/SS 12).
# ---------------------------------------------------------------------------


def prepare_and_ingest_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Fully derivable from ``run_input`` alone -- hashed at submission time."""

    del step_outputs
    return dict(run_input)


def finalize_storage_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    del run_input
    upstream = step_outputs.get(PREPARE_AND_INGEST_STEP)
    if upstream is None:
        return None
    return {
        "dataset_id": upstream.get("dataset_id"),
        "source_id": upstream.get("source_id"),
        "deduplicated": upstream.get("deduplicated"),
        "storage_extension": upstream.get("storage_extension"),
        "content_sha256": upstream.get("content_sha256"),
        "mime_type": upstream.get("mime_type"),
        "byte_size": upstream.get("byte_size"),
    }


def cognify_step_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    upstream = step_outputs.get(PREPARE_AND_INGEST_STEP)
    if upstream is None:
        return None
    return {"mode": run_input.get("mode"), "source_id": upstream.get("source_id")}


def finalize_result_input(
    run_input: Mapping[str, Any],
    step_outputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    del run_input
    ingest = step_outputs.get(PREPARE_AND_INGEST_STEP)
    storage = step_outputs.get(FINALIZE_STORAGE_STEP)
    cognify = step_outputs.get(COGNIFY_STEP)
    if ingest is None or storage is None or cognify is None:
        return None
    return {
        "source_id": ingest.get("source_id"),
        "storage_status": storage.get("storage_status"),
        "chunks": cognify.get("chunks"),
        "entities": cognify.get("entities"),
        "relations": cognify.get("relations"),
    }


def build_remember_pipeline_definition() -> PipelineDefinition:
    """The single registered Remember pipeline (SM-513)."""

    return PipelineDefinition(
        pipeline_type=PipelineType.REMEMBER,
        steps=(
            PipelineStepDefinition(
                name=PREPARE_AND_INGEST_STEP,
                definition_id=PREPARE_AND_INGEST_DEFINITION_ID,
                step=PrepareAndIngestStep(),
                input_deriver=prepare_and_ingest_input,
                # ATOMIC: execute() writes nothing authoritative (the
                # ingress artifact it may read/fetch is disposable,
                # recomputable staging state, never a source of truth);
                # every authoritative mutation commits inside persist(),
                # together with this step's own succeeded transition.
                cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
            ),
            PipelineStepDefinition(
                name=FINALIZE_STORAGE_STEP,
                definition_id=FINALIZE_STORAGE_DEFINITION_ID,
                step=FinalizeStorageStep(),
                input_deriver=finalize_storage_input,
                # AMBIGUOUS: mirrors Forget's StorageDeletionStep (SM-512) --
                # a real external filesystem side effect a PostgreSQL-only
                # callback cannot prove the state of.
                cancellation_recovery_mode=CancellationRecoveryMode.AMBIGUOUS,
            ),
            PipelineStepDefinition(
                name=COGNIFY_STEP,
                definition_id=COGNIFY_DEFINITION_ID,
                step=CognifyCompositionStep(),
                input_deriver=cognify_step_input,
                # ATOMIC: identical reasoning to Cognify's own
                # ProcessSourcesStep -- execute() only computes/stages,
                # persist_batch() is PostgreSQL-only and commits together
                # with this step's succeeded transition.
                cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
            ),
            PipelineStepDefinition(
                name=FINALIZE_RESULT_STEP,
                definition_id=FINALIZE_RESULT_DEFINITION_ID,
                step=FinalizeResultStep(),
                input_deriver=finalize_result_input,
                cancellation_recovery_mode=CancellationRecoveryMode.ATOMIC,
            ),
        ),
    )


__all__ = [
    "COGNIFY_DEFINITION_ID",
    "COGNIFY_STEP",
    "FINALIZE_RESULT_DEFINITION_ID",
    "FINALIZE_RESULT_STEP",
    "FINALIZE_STORAGE_DEFINITION_ID",
    "FINALIZE_STORAGE_STEP",
    "PREPARE_AND_INGEST_DEFINITION_ID",
    "PREPARE_AND_INGEST_STEP",
    "REMEMBER_RESOURCES_RESOURCE",
    "CognifyCompositionStep",
    "FinalizeResultStep",
    "FinalizeStorageStep",
    "PrepareAndIngestStep",
    "RememberPipelineResources",
    "build_remember_pipeline_definition",
    "cognify_step_input",
    "finalize_result_input",
    "finalize_storage_input",
    "prepare_and_ingest_input",
]
