"""Filesystem -> S3 storage convergence engine (ADR-0011 D7-D11/D31/D34-D36,
STORAGE-006).

Implements *what* convergence does: deterministic, idempotent, crash-safe
reconciliation of durable ``Source`` state against physical storage. It does
**not** decide *when* this runs, gate HTTP readiness, or filter normal
worker claims -- that lifecycle wiring is STORAGE-007's job (D31's
BOOTSTRAP/MAINTENANCE -> STORAGE_CONVERGING -> OPERATIONAL state model).
This service is directly callable/testable independent of ``lifespan.py``.

Source classification (D34, extended by D40's Case C) is total and
non-overlapping over every durable ``Source`` row:

- ``status == DELETED`` -> Case C tombstone: never inspected for storage at
  all (D40) -- storage presence/absence is irrelevant to migration.
- ``storage_uri IS NULL`` (live/incomplete) -> Remember-owned (D12/B1);
  never a migration or recovery-convergence candidate.
- ``status == DELETING`` + ``file://`` + local object present -> ordinary
  in-flight/not-yet-executed destructive lifecycle; not migration-owned.
- ``status == DELETING`` + ``file://`` + local object missing -> Case B
  (proven compatible destructive ``PipelineRun`` lineage exists) or Case D
  (no proof -- integrity failure, fail closed). Never migration-owned
  either way (D34).
- live status (``PENDING``/``PROCESSING``/``ACTIVE``/``FAILED``) +
  ``file://`` -> Case A migration candidate (D8).
- live status + ``s3://`` -> already converged; eligible only for the D9/D35
  post-repoint local-duplicate cleanup pass, never re-migrated.

Migration (D8) is snapshot -> external I/O -> PostgreSQL CAS -> (separately)
local cleanup; no PostgreSQL transaction/row lock ever spans filesystem or
S3 I/O (D10/D27).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus, PipelineType, SourceStatus
from sofias_memory.infrastructure.postgres.models import Source
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.infrastructure.storage import (
    FinalizeResult,
    InvalidSourceStorageUriError,
    SourceObjectStorage,
    SourceStorageConflictError,
    SourceStorageError,
    SourceStorageRouter,
)
from sofias_memory.infrastructure.storage.filesystem import (
    final_storage_content_matches,
    final_storage_path,
    source_storage_path,
)
from sofias_memory.loaders.text import canonical_storage_extension_for_mime_type

LIVE_MIGRATION_ELIGIBLE_STATUSES = frozenset(
    {SourceStatus.PENDING, SourceStatus.PROCESSING, SourceStatus.ACTIVE, SourceStatus.FAILED}
)
TERMINAL_PIPELINE_RUN_STATUSES = frozenset(
    {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED, PipelineRunStatus.CANCELLED}
)

FILE_URI_SCHEME = "file"
S3_URI_SCHEME = "s3"

type CasRepointFn = Callable[["SourceSnapshot", str], Awaitable["CasOutcome"]]
type LineageLookupFn = Callable[[UUID, UUID], Awaitable["CaseBLineage | None"]]


# ---------------------------------------------------------------------------
# Typed result surface (internal; no public endpoint in this slice).
# ---------------------------------------------------------------------------


class IntegrityFailureReason(StrEnum):
    """Every fail-closed condition this service can produce (D8 step D, D9,
    D34 Case D, D35's unmappable-mime-type case). Never a generic bucket --
    each value names a specific, diagnosable condition (D19)."""

    LOCAL_OBJECT_MISSING = "local_object_missing"
    INVALID_LEGACY_URI = "invalid_legacy_uri"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNMAPPABLE_MIME_TYPE = "unmappable_mime_type"
    S3_TARGET_CONFLICT = "s3_target_conflict"
    S3_UNAVAILABLE = "s3_unavailable"
    S3_VERIFY_FAILED = "s3_verify_failed"
    CASE_D_NO_PROVEN_LINEAGE = "case_d_no_proven_lineage"
    CAS_INCOMPATIBLE_STATE = "cas_incompatible_state"
    UNKNOWN_STORAGE_SCHEME = "unknown_storage_scheme"
    D43_DELETING_DURING_CASE_A_CAS = "d43_deleting_during_case_a_cas"
    """A live D34 Case A Source (snapshotted migration-eligible) was observed
    `DELETING` at CAS time. D43 (fifth/sixth amendments) structurally
    forecloses this under the supported single-process deployment model
    (stop-old-before-start-new): normal claims are blocked throughout
    STORAGE_CONVERGING, and recovery-owned destructive work only ever acts on
    Sources already `DELETING`, with proven lineage and an already-durable
    authoritative mutation, before the pass began classifying. If this is
    nonetheless observed, it is never ordinary CAS contention/benign
    ownership loss -- it is an internal lifecycle invariant violation and
    must fail closed (D19), never silently tolerated."""


@dataclass(frozen=True)
class IntegrityFailure:
    source_id: UUID
    dataset_id: UUID
    reason: IntegrityFailureReason
    message: str


@dataclass(frozen=True)
class CaseBLineage:
    """A DELETING Source whose local object is missing, with a proven
    compatible destructive ``PipelineRun`` lineage (D34 Case B) -- exposed
    so STORAGE-007 can later allow that existing lineage to progress during
    STORAGE_CONVERGING. This service never claims/runs that lineage itself
    (D31)."""

    source_id: UUID
    dataset_id: UUID
    pipeline_run_id: UUID
    pipeline_type: PipelineType
    pipeline_run_status: PipelineRunStatus

    @property
    def is_terminal(self) -> bool:
        return self.pipeline_run_status in TERMINAL_PIPELINE_RUN_STATUSES


@dataclass(frozen=True)
class ConvergenceResult:
    """Internal, dependency-free convergence outcome. Counts only -- never a
    public API in this slice."""

    candidates_examined: int = 0
    migrated: int = 0
    already_converged: int = 0
    local_duplicates_cleaned: int = 0
    recovery_owned_case_b: tuple[CaseBLineage, ...] = ()
    skipped_deleting_present: int = 0
    skipped_deleted: int = 0
    remember_owned_null: int = 0
    cleanup_deferred: tuple[IntegrityFailure, ...] = ()
    integrity_failures: tuple[IntegrityFailure, ...] = ()

    @property
    def converged(self) -> bool:
        """Whether the migration-owned set (D8) is fully repointed with no
        integrity failures -- does NOT by itself mean STORAGE_CONVERGING may
        exit to OPERATIONAL (D31 also requires every Case B lineage to reach
        a terminal state, which this service only classifies, never
        drives)."""

        return not self.integrity_failures


class CasOutcome(StrEnum):
    COMMITTED = "committed"
    ALREADY_CONVERGED = "already_converged"
    OWNED_ELSEWHERE = "owned_elsewhere"
    INCOMPATIBLE = "incompatible"
    D43_INVARIANT_VIOLATION = "d43_invariant_violation"
    """Distinct from `OWNED_ELSEWHERE` on purpose (D43): a benign status
    reclassification (e.g. `ACTIVE` -> `FAILED`, both still Case A) and an
    observed `DELETING` transition are not the same outcome and must never
    share handling -- the former is safe/expected and requires no integrity
    signal; the latter is a structurally-foreclosed invariant violation that
    must fail closed and be surfaced (D19)."""


# ---------------------------------------------------------------------------
# Durable snapshot (extracted from ORM rows before their UnitOfWork closes --
# no attribute access on a Source instance is ever attempted after the
# transaction that read it has closed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSnapshot:
    id: UUID
    dataset_id: UUID
    status: SourceStatus
    storage_uri: str | None
    mime_type: str
    byte_size: int
    content_sha256: str


def _snapshot(source: Source) -> SourceSnapshot:
    return SourceSnapshot(
        id=source.id,
        dataset_id=source.dataset_id,
        status=source.status,
        storage_uri=source.storage_uri,
        mime_type=source.mime_type,
        byte_size=source.byte_size,
        content_sha256=source.content_sha256,
    )


@dataclass
class _Counters:
    candidates_examined: int = 0
    migrated: int = 0
    already_converged: int = 0
    local_duplicates_cleaned: int = 0
    recovery_owned_case_b: list[CaseBLineage] = field(default_factory=list)
    skipped_deleting_present: int = 0
    skipped_deleted: int = 0
    remember_owned_null: int = 0
    cleanup_deferred: list[IntegrityFailure] = field(default_factory=list)
    integrity_failures: list[IntegrityFailure] = field(default_factory=list)

    def freeze(self) -> ConvergenceResult:
        return ConvergenceResult(
            candidates_examined=self.candidates_examined,
            migrated=self.migrated,
            already_converged=self.already_converged,
            local_duplicates_cleaned=self.local_duplicates_cleaned,
            recovery_owned_case_b=tuple(self.recovery_owned_case_b),
            skipped_deleting_present=self.skipped_deleting_present,
            skipped_deleted=self.skipped_deleted,
            remember_owned_null=self.remember_owned_null,
            cleanup_deferred=tuple(self.cleanup_deferred),
            integrity_failures=tuple(self.integrity_failures),
        )


def _failure(
    snapshot: SourceSnapshot, reason: IntegrityFailureReason, message: str
) -> IntegrityFailure:
    return IntegrityFailure(
        source_id=snapshot.id, dataset_id=snapshot.dataset_id, reason=reason, message=message
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StorageConvergenceService:
    """The smallest explicit convergence component (ADR-0011 STORAGE-006).

    Depends only on ``Settings``, a PostgreSQL session factory, and the
    already-implemented ``SourceObjectStorage``/``SourceStorageRouter``
    boundary -- never ``boto3``/``botocore`` directly, never FastAPI,
    ``lifespan.py``, or the S3 adapter's own internals.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: AsyncSessionFactory,
        source_storage: SourceObjectStorage | None = None,
        cas_repoint: CasRepointFn | None = None,
        lineage_lookup: LineageLookupFn | None = None,
        list_sources: Callable[[], Awaitable[list[SourceSnapshot]]] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._explicit_source_storage = source_storage
        # Test-only injection points (mirroring the STORAGE-003/004/005
        # ``source_storage`` pattern): production wiring always leaves all
        # three ``None``, so the real PostgreSQL-backed listing/CAS/
        # lineage-proof queries run. Genuine SQL correctness for each is
        # proven in the integration suite (this repository's existing
        # convention for every other SQLAlchemy-statement-level behavior);
        # unit tests inject fakes here to prove this service's own decision
        # logic against every possible outcome each gateway can produce.
        self._cas_repoint_override = cas_repoint
        self._lineage_lookup_override = lineage_lookup
        self._list_sources_override = list_sources

    def _source_storage(self) -> SourceObjectStorage:
        return self._explicit_source_storage or SourceStorageRouter(self._settings)

    async def _list_sources(self) -> list[SourceSnapshot]:
        if self._list_sources_override is not None:
            return await self._list_sources_override()
        async with PostgresUnitOfWork(self._session_factory) as uow:
            sources = await uow.sources.list_all_for_storage_convergence()
            return [_snapshot(source) for source in sources]

    async def converge(self) -> ConvergenceResult:
        """Run one full convergence pass. A no-op when
        ``STORAGE_BACKEND=filesystem`` (frozen product decision, D7): never
        scans for historical ``s3://`` rows, never requires S3 configuration,
        never reverse-migrates."""

        if self._settings.storage_backend != "s3":
            return ConvergenceResult()

        snapshots = await self._list_sources()
        storage = self._source_storage()
        counters = _Counters()
        for snapshot in snapshots:
            await self._process_source(snapshot, storage=storage, counters=counters)
        return counters.freeze()

    # -- Classification -------------------------------------------------

    async def _process_source(
        self, snapshot: SourceSnapshot, *, storage: SourceObjectStorage, counters: _Counters
    ) -> None:
        if snapshot.status == SourceStatus.DELETED:
            # Case C (D40): a tombstone is never inspected for storage at
            # all -- no Path.exists(), no S3 call, regardless of storage_uri.
            counters.skipped_deleted += 1
            return

        if snapshot.storage_uri is None:
            # B1 (D12): Remember's own FinalizeStorageStep/retry machinery
            # owns recovery here -- never the convergence scanner.
            counters.remember_owned_null += 1
            return

        scheme = urlparse(snapshot.storage_uri).scheme

        if snapshot.status == SourceStatus.DELETING:
            await self._process_deleting(snapshot, scheme=scheme, counters=counters)
            return

        if snapshot.status not in LIVE_MIGRATION_ELIGIBLE_STATUSES:
            # Structurally unreachable given the closed SourceStatus enum
            # (PENDING/PROCESSING/ACTIVE/FAILED/DELETING/DELETED are all
            # handled above) -- a genuine defect, never silently absorbed.
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.UNKNOWN_STORAGE_SCHEME,
                    f"Unrecognized Source.status {snapshot.status!r} for convergence.",
                )
            )
            return

        if scheme == S3_URI_SCHEME:
            # Already converged -- not a migration candidate (no S3->S3
            # relocation, no reverse migration). Only eligible for the
            # D9/D35 post-repoint local-duplicate cleanup.
            await self._cleanup_post_repoint_duplicate(snapshot, storage=storage, counters=counters)
            return

        if scheme != FILE_URI_SCHEME:
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.UNKNOWN_STORAGE_SCHEME,
                    f"Unsupported storage_uri scheme for migration: {snapshot.storage_uri!r}.",
                )
            )
            return

        counters.candidates_examined += 1
        await self._migrate_case_a(snapshot, storage=storage, counters=counters)

    async def _process_deleting(
        self, snapshot: SourceSnapshot, *, scheme: str, counters: _Counters
    ) -> None:
        if scheme != FILE_URI_SCHEME:
            # D34/D8's Scope note excludes every DELETING Source from the
            # migration-owned set regardless of scheme; an s3:// DELETING
            # Source is exclusively the destructive pipeline's concern.
            return

        legacy_path = self._legacy_local_path(snapshot)
        if legacy_path is not None and legacy_path.is_file():
            # Ordinary in-flight/not-yet-executed deletion -- never
            # migration-owned, never touched here.
            counters.skipped_deleting_present += 1
            return

        # Local object missing (or its legacy path cannot even be derived,
        # which is itself never treated as "already migrated" -- D34): prove
        # Case B lineage from PostgreSQL alone, or fail closed as Case D.
        lineage = await self._find_compatible_lineage(snapshot.dataset_id, snapshot.id)

        if lineage is not None:
            counters.recovery_owned_case_b.append(lineage)
            return

        counters.integrity_failures.append(
            _failure(
                snapshot,
                IntegrityFailureReason.CASE_D_NO_PROVEN_LINEAGE,
                "DELETING Source has a missing local object and no provable compatible "
                "destructive PipelineRun lineage.",
            )
        )

    async def _find_compatible_lineage(
        self, dataset_id: UUID, source_id: UUID
    ) -> CaseBLineage | None:
        if self._lineage_lookup_override is not None:
            return await self._lineage_lookup_override(dataset_id, source_id)
        async with PostgresUnitOfWork(self._session_factory) as uow:
            run = await uow.pipeline_runs.find_compatible_destructive_lineage(
                dataset_id=dataset_id, source_id=source_id
            )
            if run is None:
                return None
            return CaseBLineage(
                source_id=source_id,
                dataset_id=dataset_id,
                pipeline_run_id=run.id,
                pipeline_type=run.pipeline_type,
                pipeline_run_status=run.status,
            )

    def _legacy_local_path(self, snapshot: SourceSnapshot) -> Path | None:
        """D35: derive the exact legacy local path from durable identity
        only -- never glob/search. ``None`` when the mime_type cannot be
        mapped to a canonical extension (an unmappable-mime-type condition,
        handled distinctly by each caller)."""

        try:
            storage_extension = canonical_storage_extension_for_mime_type(snapshot.mime_type)
        except ValueError:
            return None
        return final_storage_path(
            self._settings.data_directory,
            dataset_id=snapshot.dataset_id,
            source_id=snapshot.id,
            storage_extension=storage_extension,
        )

    # -- Case A migration (D8) -------------------------------------------

    async def _migrate_case_a(
        self, snapshot: SourceSnapshot, *, storage: SourceObjectStorage, counters: _Counters
    ) -> None:
        assert snapshot.storage_uri is not None  # noqa: S101 - guaranteed by caller (scheme==file)

        # B. Validate the legacy URI via the existing containment/identity
        # rules -- reused, never reimplemented.
        try:
            local_path = source_storage_path(
                self._settings.data_directory,
                dataset_id=snapshot.dataset_id,
                source_id=snapshot.id,
                storage_uri=snapshot.storage_uri,
            )
        except InvalidSourceStorageUriError as exc:
            counters.integrity_failures.append(
                _failure(snapshot, IntegrityFailureReason.INVALID_LEGACY_URI, str(exc))
            )
            return
        if local_path is None:
            # D. Missing local object -- fail closed, never "already migrated".
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.LOCAL_OBJECT_MISSING,
                    "Migration-owned Source's exact local object is missing.",
                )
            )
            return

        # C/D. Read bytes; validate byte_size/content_sha256 against the
        # PostgreSQL snapshot -- never rewritten, never trusted from elsewhere.
        raw_bytes = local_path.read_bytes()
        if len(raw_bytes) != snapshot.byte_size:
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.SIZE_MISMATCH,
                    "Local object byte size does not match Source.byte_size.",
                )
            )
            return
        if sha256(raw_bytes).hexdigest() != snapshot.content_sha256:
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.HASH_MISMATCH,
                    "Local object content_sha256 does not match Source.content_sha256.",
                )
            )
            return

        try:
            storage_extension = canonical_storage_extension_for_mime_type(snapshot.mime_type)
        except ValueError:
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.UNMAPPABLE_MIME_TYPE,
                    f"mime_type {snapshot.mime_type!r} has no canonical storage extension.",
                )
            )
            return

        # E/F. Deterministic target; idempotent finalize (absent -> upload,
        # matching -> reuse, conflicting -> raise) -- delegated entirely to
        # the already-implemented router/adapter, never reimplemented here.
        try:
            finalize_result: FinalizeResult = await storage.finalize(
                dataset_id=snapshot.dataset_id,
                source_id=snapshot.id,
                storage_extension=storage_extension,
                original_bytes=raw_bytes,
            )
        except SourceStorageConflictError as exc:
            counters.integrity_failures.append(
                _failure(snapshot, IntegrityFailureReason.S3_TARGET_CONFLICT, str(exc))
            )
            return
        except SourceStorageError as exc:
            counters.integrity_failures.append(
                _failure(snapshot, IntegrityFailureReason.S3_UNAVAILABLE, str(exc))
            )
            return

        # G. Strong verification (GET + Sofias Memory's own SHA-256) before
        # any CAS -- required regardless of which finalize branch ran.
        try:
            verified = await storage.verify(
                dataset_id=snapshot.dataset_id,
                source_id=snapshot.id,
                storage_uri=finalize_result.storage_uri,
                content_sha256=snapshot.content_sha256,
            )
        except SourceStorageError as exc:
            counters.integrity_failures.append(
                _failure(snapshot, IntegrityFailureReason.S3_VERIFY_FAILED, str(exc))
            )
            return
        if not verified:
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.S3_VERIFY_FAILED,
                    "Strong verification did not confirm the S3 target's expected content.",
                )
            )
            return

        # H. Short PostgreSQL CAS -- no lock held across the I/O above.
        cas_outcome = await self._cas_repoint(snapshot, new_uri=finalize_result.storage_uri)
        if cas_outcome is CasOutcome.COMMITTED:
            counters.migrated += 1
        elif cas_outcome is CasOutcome.ALREADY_CONVERGED:
            counters.already_converged += 1
        elif cas_outcome is CasOutcome.OWNED_ELSEWHERE:
            # A legitimate other pipeline now owns this Source's status
            # (e.g. a benign reclassification to another still-migration-
            # eligible status, ACTIVE -> FAILED) -- never overwrite its newer
            # state, and never delete the just-uploaded S3 object here (a
            # concurrent migrator observing this same Source at a different,
            # still-live status may still legitimately need to adopt this
            # exact deterministic target -- D10). This branch is reached only
            # for status changes D43 classifies as ordinary CAS contention --
            # never for an observed `DELETING` transition, which
            # `_cas_repoint` reports as `D43_INVARIANT_VIOLATION` instead
            # (handled separately below) so this outcome always stays
            # genuinely benign.
            return
        elif cas_outcome is CasOutcome.D43_INVARIANT_VIOLATION:
            # D43 (fifth/sixth amendments) -- RESOLVED, not an open gap: the
            # original STORAGE-006 CAS-loss audit asked what happens if this
            # CAS loss is specifically because the Source transitioned to
            # DELETING mid-migration (D34 permanently excludes DELETING/
            # DELETED from future Case-A reclassification, so an orphaned S3
            # object here would never be revisited by a later pass). D43's
            # accepted answer is architectural exclusion, not bookkeeping:
            # under the supported single-process deployment model (stop-old-
            # before-start-new), a live Case-A Source can never legitimately
            # acquire a *new* DELETING transition while this process is
            # STORAGE_CONVERGING -- normal claims are blocked (D31) and
            # recovery-owned destructive work only ever acts on Sources
            # already DELETING, with its authoritative mutation already
            # durable, before this pass began classifying. If this is
            # nonetheless observed, it is never ordinary CAS contention and
            # must never be silently tolerated as one: it is an internal
            # lifecycle invariant violation (an unsupported deployment
            # overlap, e.g. old-OPERATIONAL and new-STORAGE_CONVERGING
            # processes running concurrently -- D43 explicitly excludes that
            # from the MVP). No durable migration-attempt ledger and no
            # destructive-pipeline-side S3 cleanup were adopted for D43 (both
            # considered and rejected -- see D43's "Alternatives
            # considered"); the accepted resolution is exclusion of the race
            # precondition itself, not detection/recovery after the fact.
            # This service's own obligation when the precondition is
            # nonetheless violated is exactly what D19/D37 already require
            # everywhere else: never adopt the S3 target, never touch the
            # local object, fail closed, and surface it -- never reach a
            # false OPERATIONAL.
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.D43_DELETING_DURING_CASE_A_CAS,
                    "D43 lifecycle invariant violation: a live Case-A Source was observed "
                    "DELETING during its own migration CAS attempt. This is structurally "
                    "excluded under the supported single-process deployment model "
                    "(stop-old-before-start-new) and must never be treated as ordinary CAS "
                    "contention.",
                )
            )
            return
        else:  # INCOMPATIBLE
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.CAS_INCOMPATIBLE_STATE,
                    "Source durable state changed to an unexpected value during migration I/O.",
                )
            )
            return

        # I. Only after a durable, confirmed repoint (this pass's own
        # COMMITTED, or a prior pass's ALREADY_CONVERGED) may exact local
        # cleanup be attempted -- always as a separate step, never inline
        # with the write that produced it (D8 step I/D9). The cleanup pass
        # must verify against the NEW s3:// identity that is now durable in
        # PostgreSQL, not the stale file:// value this migration attempt's
        # own snapshot was taken with.
        repointed_snapshot = replace(snapshot, storage_uri=finalize_result.storage_uri)
        await self._cleanup_post_repoint_duplicate(
            repointed_snapshot, storage=storage, counters=counters
        )

    async def _cas_repoint(self, snapshot: SourceSnapshot, *, new_uri: str) -> CasOutcome:
        """D8 step H / D10: compare-and-swap on the exact snapshotted
        identity, in a short, dedicated transaction opened only for this
        step -- never held across the I/O above."""

        if self._cas_repoint_override is not None:
            return await self._cas_repoint_override(snapshot, new_uri)
        async with PostgresUnitOfWork(self._session_factory) as uow:
            source = await uow.sources.get_by_id_for_update(snapshot.id)
            if source is None:
                # The Source row itself is gone -- nothing left to own the
                # object; not this service's concern to reconcile further.
                return CasOutcome.OWNED_ELSEWHERE
            if source.storage_uri == new_uri:
                # Another convergence attempt (this process or a concurrent
                # one) already committed the identical deterministic target
                # -- idempotent convergence, not a conflict (D10).
                return CasOutcome.ALREADY_CONVERGED
            if source.status != snapshot.status:
                # A legitimate other pipeline changed this Source's status
                # during S3 I/O -- never repoint a Source that is no longer
                # the exact live state this migration snapshotted.
                #
                # D43 (fifth/sixth amendments): under the supported single-
                # process deployment model (stop-old-before-start-new), the
                # only status change a live D34 Case A Source can
                # legitimately undergo here is a reclassification to another
                # still-migration-eligible status (e.g. ACTIVE -> FAILED) --
                # ordinary, benign CAS contention, `OWNED_ELSEWHERE`. A new
                # `DELETING` transition against a Case-A Source is
                # structurally foreclosed for the entire STORAGE_CONVERGING
                # pass (normal claims are blocked, and recovery-owned
                # destructive work only ever acts on Sources already
                # `DELETING` with an already-durable authoritative mutation
                # before the pass began) -- so this is never expected to
                # happen. If it is nonetheless observed, it is NOT benign
                # ownership loss and must never be reported as
                # `OWNED_ELSEWHERE`: it is a D43 lifecycle invariant
                # violation, reported as its own distinct outcome so the
                # caller fails closed and surfaces it (D19), never silently
                # tolerates it.
                if source.status is SourceStatus.DELETING:
                    return CasOutcome.D43_INVARIANT_VIOLATION
                return CasOutcome.OWNED_ELSEWHERE
            if source.storage_uri != snapshot.storage_uri:
                # Same status, but storage_uri is neither the old nor the
                # new expected value -- an unexpected durable state this
                # service cannot explain; fail closed rather than guess.
                return CasOutcome.INCOMPATIBLE
            source.storage_uri = new_uri
            await uow.commit()
            return CasOutcome.COMMITTED

    # -- D9/D35 post-repoint local-duplicate cleanup ---------------------

    async def _cleanup_post_repoint_duplicate(
        self, snapshot: SourceSnapshot, *, storage: SourceObjectStorage, counters: _Counters
    ) -> None:
        """D9/D35: exact, non-recursive cleanup of the redundant legacy
        local copy left behind after a confirmed repoint to ``s3://`` --
        covers both this migration's own D8 step I and B1's (STORAGE-004)
        deliberately-deferred post-repoint cleanup. Applies only to LIVE
        (non-DELETING, non-DELETED) ``s3://`` Sources (D40); never DELETED
        (Case C), never DELETING, never ``storage_uri = NULL``."""

        # 1. Derive the exact legacy local path from durable identity only.
        legacy_path = self._legacy_local_path(snapshot)
        if legacy_path is None:
            # D35: an unmappable mime_type means the legacy path cannot be
            # unambiguously derived -- fail closed, never guess/delete.
            counters.integrity_failures.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.UNMAPPABLE_MIME_TYPE,
                    "Cannot derive the legacy local path for post-repoint cleanup: "
                    f"mime_type {snapshot.mime_type!r} has no canonical storage extension.",
                )
            )
            return

        # 3. Absent -> no-op (this is the common case: nothing left to clean).
        if not legacy_path.is_file():
            return

        # 4. Verify local byte_size/hash against the PostgreSQL Source
        # metadata before ever touching it.
        if not final_storage_content_matches(legacy_path, content_sha256=snapshot.content_sha256):
            # Never destroy unexplained data (D8 step F/D9's shared
            # principle) -- leave it untouched, surface it distinctly.
            counters.cleanup_deferred.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.HASH_MISMATCH,
                    "A local file exists at the exact legacy path but its content does not "
                    "match Source.content_sha256 -- left untouched.",
                )
            )
            return

        # 5. Re-confirm the authoritative S3 target still corresponds to
        # this Source (D35 step 2: re-run the cheap check, never re-trust a
        # prior pass blindly).
        assert snapshot.storage_uri is not None
        try:
            confirmed = await storage.verify(
                dataset_id=snapshot.dataset_id,
                source_id=snapshot.id,
                storage_uri=snapshot.storage_uri,
                content_sha256=snapshot.content_sha256,
            )
        except SourceStorageError as exc:
            counters.cleanup_deferred.append(
                _failure(snapshot, IntegrityFailureReason.S3_VERIFY_FAILED, str(exc))
            )
            return
        if not confirmed:
            counters.cleanup_deferred.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.S3_VERIFY_FAILED,
                    "Authoritative S3 target could not be re-confirmed before local cleanup.",
                )
            )
            return

        # 6. Delete only the exact verified redundant legacy file.
        try:
            legacy_path.unlink()
        except OSError as exc:
            # A PostgreSQL repoint is already durable and verified -- never
            # roll it back, never touch the S3 object, just report the
            # operational cleanup condition (a redundant local copy is
            # safer than undoing a successful repoint).
            counters.cleanup_deferred.append(
                _failure(
                    snapshot,
                    IntegrityFailureReason.S3_UNAVAILABLE,
                    f"Local cleanup unlink failed: {exc}",
                )
            )
            return
        counters.local_duplicates_cleaned += 1

        # 7. Safely clean only the now-empty Source directory (existing
        # filesystem rules, unchanged) -- never recurse into unknown content.
        parent = legacy_path.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            # Best-effort only -- an empty-directory removal failure is
            # never a correctness problem (the file itself is already gone).
            pass


__all__ = [
    "LIVE_MIGRATION_ELIGIBLE_STATUSES",
    "TERMINAL_PIPELINE_RUN_STATUSES",
    "CasOutcome",
    "CaseBLineage",
    "ConvergenceResult",
    "IntegrityFailure",
    "IntegrityFailureReason",
    "StorageConvergenceService",
]
