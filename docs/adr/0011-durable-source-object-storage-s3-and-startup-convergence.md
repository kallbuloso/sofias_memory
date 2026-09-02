# ADR-0011: Durable Source Object Storage, S3 Backend, and Startup Convergence

## Status

accepted

> **Amendment note (recorded after the first architecture review of this proposal,
> status remains `proposed`):** the review approved the core design in principle but
> found 2 blockers, 2 major clarifications, and 1 required invariant, all addressed
> below in D31–D36 and in targeted corrections to D6, D7, D8, D9, D12, D19, D20, D27,
> D28, D29, the startup state machine, the crash-window table, and the validation
> matrix. This is not a rewrite — every already-accepted decision (D1–D5, D10–D11,
> D13–D18, D21–D26, D30, the alternatives, and the consequences) stands unchanged
> except where a cross-reference below narrows its wording.
>
> **Second amendment note (product/architecture clarification, status remains
> `proposed`):** this round deliberately changes the Source-original **deletion**
> contract itself, not merely a gap in the prior text. It freezes the distinction
> between PostgreSQL-authoritative Sofias Memory memory and a Source original as a
> durable *provenance/reprocessing artifact* (D37), introduces a four-outcome
> `StorageDeleteResult` contract including a new `UNRESOLVED` outcome (D37), and
> establishes that full Forget / Dataset Forget / Forget Everything / administrative
> `DATASET_DELETE` must not be permanently blocked solely because physical
> Source-original cleanup could not be proven (D37). This **narrows** ADR-0010's
> prior wording that treated successful physical storage deletion as mandatory before
> `finalize_tombstone` (D28's amendment, below) — PostgreSQL/Neo4j authoritative-memory
> deletion semantics, exact-key safety, no-lock-across-I/O, and strong-verification-
> before-claiming-`DELETED_NOW`/`ALREADY_ABSENT` are all unchanged. See D37–D42.
>
> **Third amendment note (final contract-completeness pass, status remains
> `proposed`):** narrow, documentation-only precision fixes to the D37–D42 round —
> no redesign, no schema/queue/worker/reverse-migration/filesystem-S3-readiness
> addition. Four fixes: (1) D37 gains an explicit per-Source `StorageDeleteResult`
> coverage invariant — a missing result is an internal invariant failure, never an
> implicit `UNRESOLVED`; (2) D15's pre-D37 opening claim ("the Source original must
> actually cease to be retained") is corrected to apply only to what a `DELETED_NOW`/
> `ALREADY_ABSENT` *claim* must prove, not to whether business deletion may proceed;
> (3) D31's OPERATIONAL condition is restated as "every recovery-owned lineage
> reaches a terminal state," matching the terminal-run allowance already present
> elsewhere in D31/D29, resolving an internal inconsistency; (4) D34's Case B now
> requires *proven* compatible destructive `PipelineRun` lineage, not
> `Source.status = DELETING` alone — an unproven case is the new Case D, fail-closed.
> See the amendment markers inside D15, D31, D34, D37, and validation items 55–58.
>
> **Fourth amendment note (final editorial consistency fix, status remains
> `proposed`; design considered CLOSED as of this note):** two remaining internal
> consistency issues, no new decisions. (1) D39's `DELETED`+`storage_uri=NULL` table
> is corrected — that state no longer implies physical deletion/absence was
> positively proven; it may equally result from `NOT_REQUESTED` (D37: `storage_uri`
> already `NULL` before the storage step ran, so there was no known locator to
> clean up). `DELETED`+non-`NULL` still means `UNRESOLVED` cleanup debt, unchanged.
> (2) every remaining live use of "non-`DELETING`" as shorthand for
> "migration-owned/live" is replaced with the precise D40 status list (`PENDING`,
> `PROCESSING`, `ACTIVE`, `FAILED`) across D7, D19, and D34 Case A, since
> "non-`DELETING`" literally includes `DELETED` and therefore contradicted D40's own
> categorical exclusion of `DELETED` tombstones. See the amendment markers inside
> D7, D19, D34, D39, and validation items 33 and 57.
>
> **Fifth amendment note (STORAGE-006 CAS-loss safety audit finding, status remains
> `accepted`; documentation-only, no runtime/schema change) — closes a real gap the
> implementation audit found, not a hypothetical.** A deterministic S3 `PUT` may
> succeed and be strongly verified (D8 steps F/G) before migration's own PostgreSQL
> CAS (D8 step H) commits. If, in that exact window, a concurrent destructive
> pipeline (Forget/`DATASET_DELETE`) transitions the same Source from a live,
> migration-eligible state to `DELETING` and completes — deleting only the `file://`
> original (D14, routed by `storage_uri`'s current scheme, unaware of the parallel S3
> upload) and finalizing `DELETED` + `storage_uri = NULL` — the already-uploaded S3
> object becomes **permanently undiscoverable**: D40 correctly and deliberately
> forbids ever inspecting a `DELETED` Source for storage purposes again, and no
> durable record of the migration attempt exists anywhere to recover it. Namespace-key
> uniqueness (D6/D36) rules out a *collision* with another Source; it does not rule out
> this *orphan* condition. New: **D43** resolves this through **lifecycle exclusion**
> (STORAGE_CONVERGING structurally cannot let a migration-eligible Source begin a new
> destructive transition at all — the race precondition is removed, not reconciled
> after the fact), not through a durable migration-intent ledger, dual-backend
> destructive deletes, DELETED-tombstone scanning, or locks held across S3 I/O — see
> D43's own "Alternatives considered for this amendment" for why each was rejected for
> this increment. D31/D34 gain clarifying (non-narrowing) cross-references to D43; no
> existing decision's substance changes. See D43, the amendment markers inside D31 and
> D34, and validation items 59–66.
>
> **Sixth amendment note (D43 recovery-owned claim consistency fix, status remains
> `accepted`; documentation-only, no runtime/schema change) — corrects a real
> normative gap between D31's original recovery-owned-claim wording and D43's
> lifecycle-exclusion argument, found during D43's own review.** D31's original
> "Recovery-owned destructive work, defined" made a `PipelineRun` recovery-owned
> merely because its target scope *contained* a Source classified D34 Case B. That is
> insufficient: a `DATASET_DELETE` run whose own `deactivate_authoritative` step has
> **not** yet durably succeeded for its complete scope could, if claimed on that
> looser basis, still execute a `DELETING`-causing mutation against a *different*,
> still-live Case-A Source in the same scope during STORAGE_CONVERGING — reopening
> the exact CAS-loss orphan race D43 exists to close. **Corrected:** recovery-owned
> claim eligibility now requires, in addition to Case-B Source membership and
> compatible lineage, that the run's own `authoritative_mutation`
> (Forget)/`deactivate_authoritative` (`DATASET_DELETE`) step has a durable
> `succeeded` `PipelineStep` record — the same accepted step-completion predicate
> ADR-0010 D28 and D34's own Case B proof already use, and the one the STORAGE-006
> implementation's lineage query already applies (this amendment brings the ADR text
> up to what the accepted implementation already does, not the reverse). Because that
> step commits its run's entire target scope atomically (D14), a `succeeded` record
> is proof for the whole scope, not just the Source that prompted classification — a
> worked `DATASET_DELETE` multi-source counterexample and general proof are added to
> D31. D34's Case B classification itself is unchanged (Source-level, not run-level);
> D31's `DATASET_DELETE`/Forget allowance and D43's architecture are both unchanged in
> substance — only the run-claim predicate's precision changed. The single-replica
> MVP boundary (D43) is also sharpened: `replicas = 1` alone does not exclude
> start-first rolling overlap between an old `OPERATIONAL` process and a new
> `STORAGE_CONVERGING` process; the supported MVP deployment model is frozen as
> stop-old-before-start-new process exclusivity (a deployment-configuration
> obligation, not a runtime interlock). See the amendment markers inside D31, D34,
> and D43, and validation items 67–69.

## Context

Today Sofias Memory has exactly one Source-object storage backend, implemented as free
functions rather than a named boundary:

- `sofias_memory/services/remember.py`: `final_storage_directory`, `final_storage_path`,
  `final_storage_uri`, `write_final_storage_bytes`, `final_storage_content_matches`,
  plus the durable ingress staging helpers (`ingress_directory`,
  `write_ingress_bytes`, `read_ingress_bytes`, `delete_ingress_artifact`, ...) used by
  `pipelines/steps/remember.py`'s `FinalizeStorageStep` (SM-513).
- `sofias_memory/services/forget.py`: `delete_source_storage`, `source_storage_path`,
  used by `pipelines/steps/forget.py`'s `StorageDeletionStep` and
  `pipelines/steps/dataset_delete.py`'s `DeleteStorageStep` (ADR-0010 D9 step 4).
- `sofias_memory/services/cognify.py`: `source_storage_path_for_cognify`, used to
  rehydrate a Source's bytes for Cognify processing.

`Source.storage_uri` (`infrastructure/postgres/models/source.py:88`) is already an
unrestricted, nullable `Text` column holding a `file://` URI produced by
`final_storage_uri`. `Settings.data_directory` (`config.py:116`) defaults to
`/data/sources`, is mandatory, and is the sole root every one of the functions above
resolves paths against. `compose.yaml` and `deploy/easypanel/compose.yaml` both mount a
persistent `sofias_memory_sources` volume at that path; `docs/operations.md` documents
it as one of exactly two authoritative-and-must-be-backed-up things (the other being
PostgreSQL).

Every one of these call sites hard-codes the assumption "Source original storage is a
local filesystem path under `DATA_DIRECTORY`, addressed by a `file://` URI." There is no
seam that lets a second backend exist, and no notion that `DATA_DIRECTORY` might one day
hold anything other than Source originals plus Remember's `_ingress/` staging area
(`INGRESS_DIRECTORY_NAME`, `services/remember.py`).

`docs/adr/0005-no-optional-dependencies.md` (accepted) forbids optional dependency
extras, plugin registries, and "provider abstraction packages used to make optional
backends appear interchangeable." `docs/adr/0009-worker-queue-and-pipeline-lifecycle-contract.md`
(accepted) freezes PostgreSQL-only persist transactions, external I/O confined to
`execute()`, `CancellationRecoveryMode` classification, and the no-lock-across-I/O
discipline; it already classifies `StorageDeletionStep`/`DeleteStorageStep` as
`AMBIGUOUS` for exactly the reason this ADR must also respect: a PostgreSQL-only
reconciliation cannot prove an orphaned attempt did or did not already apply its
external effect. `docs/adr/0010-administrative-dataset-deletion-contract.md` (accepted)
generalizes storage deletion into `DATASET_DELETE`'s step 4 using the same
`delete_source_storage`/`source_storage_path` primitives Forget already uses.

Product requirements now call for an S3-compatible object storage backend for Source
originals, usable both for fresh installs and for existing filesystem installs that
redeploy with a changed setting — without a required manual migration step, without
weakening any durability/recovery guarantee above, and without reopening ADR-0005's
prohibition on generic provider ecosystems.

This ADR is architecture/design only. No runtime code, migration, Compose file, or
dependency change is made by this task.

## Decision

### D1. `DATA_DIRECTORY` is redefined as the persistent application-data root

`DATA_DIRECTORY` (canonical default `/data/sources`) remains mandatory in every
supported installation, including `STORAGE_BACKEND=s3`. Its meaning is narrowed from
"the Source storage root" to:

> the persistent, mandatory application-data root, of which Source-original storage
> under the `filesystem` backend is one occupant, not the definition of the whole.

Reserved application-owned namespaces under it, at minimum:

- `_ingress/` — already exists today (`INGRESS_DIRECTORY_NAME`, D3 below); unchanged.
- `_system/` — reserved for possible future persistent application state. Not created
  by this ADR; no code writes to it yet.

No implementation may assume "everything under `DATA_DIRECTORY` is a deletable Source
object." D24 makes this an explicit, enforced cleanup-safety rule, not just a
convention.

### D2. `STORAGE_BACKEND` semantics

New setting, added to `Settings` (`config.py`) alongside `data_directory`:

```text
STORAGE_BACKEND=filesystem | s3        (canonical default: filesystem)
```

`STORAGE_BACKEND` controls **only** where new finalized Source originals are written.
It has no effect on PostgreSQL, Neo4j, `TEMP_DIRECTORY`, durable Remember ingress
(D3), or any other persistent application data.

- `filesystem`: new finalized Source originals continue to be written under
  `DATA_DIRECTORY`, exactly as `write_final_storage_bytes` does today.
- `s3`: new finalized Source originals are written to the configured S3 bucket/prefix
  instead (D6, D12).

### D3. Durable Remember ingress stays local in both modes

`_ingress/<run_id>/...` under `DATA_DIRECTORY` is unaffected by `STORAGE_BACKEND` in
either direction. This ADR does not move ingress to S3.

Rationale: ingress is durable *pipeline staging* (SM-513 SS 9/10 —
`ingress_directory`/`write_ingress_bytes`/`delete_ingress_artifact`), not finalized
Source object storage. Keeping it local means an operator can flip
`STORAGE_BACKEND=filesystem → s3` and redeploy without losing queued/retryable
Remember work: any `_ingress/<run_id>` directory already staged before the flip is
still there and still readable by `FinalizeStorageStep` after restart; only the
*target* of finalization changes, per D2. The existing invariant — ingress may be
garbage-collected only after the finalized Source object is confirmed durable — is
unchanged; `delete_ingress_artifact` continues to run only after `FinalizeStorageStep`
confirms the final write (filesystem or, after this ADR, S3), never before.

### D4. `SourceObjectStorage` port and adapters

Today's storage logic is a set of free functions split across three service modules
with no shared boundary — `remember.py`'s write-side helpers, `forget.py`'s
delete-side helpers, `cognify.py`'s read-side helper — each independently
computing/parsing `file://` paths. This ADR introduces one first-party application
port (conceptual shape, exact module/class names left to implementation):

```text
SourceObjectStorage (port)
  finalize(dataset_id, source_id, storage_extension, content_bytes, content_sha256) -> storage_uri
  read(dataset_id, source_id, storage_uri, expected_byte_size, expected_content_sha256, max_bytes) -> bytes
  delete(dataset_id, source_id, storage_uri) -> StorageDeleteResult   # already-absent is success
  verify(dataset_id, source_id, storage_uri, expected_content_sha256) -> bool

FilesystemSourceObjectStorage   # wraps today's remember.py/forget.py/cognify.py logic
S3SourceObjectStorage
SourceStorageRouter             # the only thing pipelines/services depend on
```

`RememberService`/`FinalizeStorageStep`, `CognifyService`'s rehydration call site, and
`ForgetService`/`StorageDeletionStep`/`DeleteStorageStep` (ADR-0010 D9 step 4) call
only `SourceStorageRouter`. None of them import an S3 SDK, `boto`-style client,
bucket name, or `Path`-based filesystem detail directly after this change — those
belong entirely to the two adapters.

The router has two distinct responsibilities, and they use different signals:

- **Write** (`finalize`): routed by `Settings.storage_backend` — "where do *new*
  originals go right now."
- **Read** / **Delete** (`read`, `delete`, `verify`): routed by the scheme of the
  `Source.storage_uri` already on the row — "where does *this* original actually
  live." `STORAGE_BACKEND` is never consulted for read/delete routing.

This split is mandatory, not an implementation nicety: it is what makes D5 (mixed URI
compatibility) and D7 (startup convergence) possible without any special-casing at
call sites.

### D5. Mixed URI compatibility — read/delete route by scheme, not by config

Supported `Source.storage_uri` schemes: `file://` (existing, unchanged encoding) and
`s3://` (new, D6). The runtime must be able to read and delete both, concurrently,
regardless of the current `STORAGE_BACKEND` value:

```text
Source A -> file://...   (never migrated, or migration not yet reached it)
Source B -> s3://...     (already migrated, or written under STORAGE_BACKEND=s3)
```

`STORAGE_BACKEND=filesystem` does **not** imply "the process can only understand
`file://`" — it only means "new writes go to the filesystem." `STORAGE_BACKEND` means
*current write backend*, not *the only backend this process can read*. This is
required for: mid-migration state (D7/D8), crash recovery (D9), controlled rollback of
the setting without data loss, and old durable `PipelineRun`s created before a backend
change resuming correctly after restart.

**Explicitly out of scope for ADR-0011:** automatic reverse S3 → filesystem migration.
Setting `STORAGE_BACKEND=filesystem` on an installation that already has `s3://`
Sources does **not** download them back. Those Sources remain S3-backed; the process
still needs a correctly configured, usable `S3SourceObjectStorage` adapter to read or
delete them, even while `STORAGE_BACKEND=filesystem` governs new writes. A future
explicit relocation mechanism (D25) is required before reverse migration exists.

### D6. Storage URI format

`file://` URIs are unchanged (`final_storage_uri`'s existing `Path.as_uri()` encoding,
resolved/validated by the existing containment checks in
`source_storage_path`/`source_storage_path_for_cognify`).

S3 URIs are canonical `s3://<bucket>/<key>` — no credentials, endpoint URL, query
string, or signed parameter ever appears in `storage_uri`. `storage_uri` is durable
object *identity*, not a credential/config transport (this rules out Alternative G).

Deterministic key shape, mirroring the existing filesystem layout
(`final_storage_directory`/`final_storage_path`):

```text
<prefix>/v1/sources/<dataset_id>/<source_id>/original<extension>
```

- `dataset_id`/`source_id` are the same application-generated UUIDs already used by
  `final_storage_path` today — never a client-supplied filename or slug.
- `<extension>` is the same normalized `storage_extension` value
  `write_final_storage_bytes` already computes and stores — no new extension logic.
- `<prefix>` is `STORAGE_S3_PREFIX` (D16), normalized (no leading/trailing `/`,
  reused verbatim as a literal path segment — never derived from user input).
- No traversal semantics: because both path segments are UUIDs and the extension is
  server-computed, there is no user-controlled input in key construction at all — a
  strictly narrower attack surface than the filesystem path guards `source_storage_path`
  already needs (those exist because a URI is later *parsed back*; S3 key construction
  never parses anything back from a Source row other than the same two UUIDs).
- Key construction/parsing is centralized in the S3 adapter (mirroring
  `final_storage_path`'s role for the filesystem adapter) — never duplicated at call
  sites.

Determinism here is the single load-bearing property the rest of this ADR (D8–D10)
relies on for idempotent, lock-free, concurrent-safe migration.

**Amendment (review finding M2):** `<extension>` in both the S3 key above and the
filesystem key (`final_storage_path`'s `original<extension>`) must be derivable from
`Source.mime_type` alone, through one centralized, versioned mapping, because D35
requires reconstructing a Source's legacy filesystem path *after* `storage_uri` has
already become `s3://...` (the old `file://` URI is gone by then — CAS overwrites it,
D8 step H). Today's `STORAGE_EXTENSION_BY_SOURCE_EXTENSION`
(`loaders/text.py:26`) is keyed by the *upload's* source extension, which is never
persisted on `Source` — only `Source.mime_type` is durable. Inspection of the current
mapping confirms every source extension sharing a mime type also shares a storage
extension (`.md`/`.markdown` → `MARKDOWN_FILE_MIME_TYPE` → `.md`; `.htm`/`.html` →
`HTML_FILE_MIME_TYPE` → `.html`; every other entry is already 1:1), so a
`STORAGE_EXTENSION_BY_MIME_TYPE` mapping is derivable from today's data without any
behavior change — but it does not exist as its own named, centralized mapping yet.
Implementation must add it and use it as the single source of truth for both original
extension assignment (`FinalizeStorageStep`) and legacy-path reconstruction (D35).

### D7. Startup storage convergence gate

**Amendment (review findings M1, M1.1, M1.2 — this section is corrected, not merely
extended):** the original wording of this section conflated "insert convergence into
`lifespan.py` before `worker.start()`" with "the HTTP application already serves
`/health/live` while that runs," which a blocking FastAPI lifespan startup does not
guarantee by itself, and it treated "migrate all `file://` Sources" as the entire
scope of convergence, which B1/B2 below show is incomplete. This section is now
normative only together with D31 (recovery-owned destructive work and the
BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING → OPERATIONAL state model), D32 (Alembic
interaction), D33 (long-migration liveness contract), D35 (post-CAS legacy locator),
and D36 (managed S3 namespace ownership) — read this section together with those.

Changing `STORAGE_BACKEND=filesystem → s3` on an existing installation must not
require a separate operator-run migration command. Bootstrap gains a **storage
convergence gate**, distinct from and never substituting for Alembic:

- Alembic remains explicit and is never invoked automatically (frozen in detail by
  D32). `lifespan.py`'s existing ordering — PostgreSQL probe → (Neo4j bootstrap) →
  pipeline recovery → worker start → ready — already treats "schema at expected head"
  as a precondition the app does not try to satisfy itself (`docs/operations.md`
  section 2's "migration is explicit, never automatic"); this ADR does not change
  that. If the schema is not at the expected Alembic revision, storage convergence
  must not run and must not attempt to guess schema state, inspect `Source` rows, or
  start normal worker/business processing — the process instead stays in the
  **BOOTSTRAP/MAINTENANCE** state (D31) until an operator runs `alembic upgrade head`
  explicitly.
- Once schema is confirmed current, the gate's actual convergence scope is narrower
  than "every `Source` with `storage_uri = file://...`":
  - **Migration-owned candidates** (D8's actual scope): **(corrected, editorial
    consistency pass — "non-`DELETING`" literally includes `DELETED` and therefore
    contradicted D40; the precise rule is D40's own live-status list)** authoritative
    Sources with `status IN (PENDING, PROCESSING, ACTIVE, FAILED)` — equivalently
    `status NOT IN (DELETING, DELETED)` — and `storage_uri = file://...`. These, and
    only these, are uploaded to S3 and CAS-repointed by D8.
  - **Excluded — Remember-owned recovery** (B1, frozen in D12): a Source with
    `storage_uri = NULL` is never a migration candidate for D8. A `NULL` `storage_uri`
    means Remember's own `FinalizeStorageStep` has not yet durably recorded a final
    location — recovering it (potentially by discovering and reusing an already-valid
    legacy finalized filesystem object, D12) is owned by the existing Remember
    `PipelineRun`/step-retry machinery (ADR-0009), never by the startup scanner. The
    startup scanner performing this recovery itself would be a second, competing
    implementation of `FinalizeStorageStep`'s own idempotent-resume contract — rejected
    as Alternative M below.
  - **Excluded — destructive-pipeline-owned recovery** (B2, frozen in D31/D35): a
    Source with `storage_uri = file://...` **and** `status = DELETING` whose local
    object is missing is not automatically integrity-failed. It may be the durable
    trace of an already-applied, `AMBIGUOUS`-classified `StorageDeletionStep`/
    `DeleteStorageStep` effect (ADR-0009 §I / ADR-0010 D9) that crashed before its
    owning `PipelineRun` reached `finalize_target`/`finalize_tombstone`. Reconciling
    this is owned exclusively by that existing durable Forget/`DATASET_DELETE`
    lineage, resumed through the existing queue/claim/retry machinery — never by
    uploading the (deleted) bytes to S3, and never by the storage-convergence
    subsystem performing the business finalization itself (D31).
- Canonical behavior, inserted into `lifespan.py`'s existing startup sequence, now
  described against the three-state model of D31:

```text
STORAGE_BACKEND=filesystem
    -> no automatic Source-storage migration.
    -> schema-valid check (D32) still gates OPERATIONAL exactly as today.
    -> otherwise unaffected: today's behavior.

STORAGE_BACKEND=s3
    -> BOOTSTRAP/MAINTENANCE until schema is confirmed current (D32);
    -> enter STORAGE_CONVERGING: probe S3 (D21);
    -> classify durable Sources into migration-owned (D8) vs. recovery-owned (D31)
       vs. Remember-owned-NULL (excluded, D12) sets;
    -> run D8's migrate/verify/CAS-repoint over the migration-owned set;
    -> allow the existing pipeline engine (unchanged, ADR-0009) to make progress on
       recovery-owned destructive lineage (D31) -- normal (non-recovery-owned)
       PipelineRun claims remain blocked throughout;
    -> clean confirmed legacy local duplicates using the D35 locator contract;
    -> verify convergence (D19);
    -> only once migration-owned convergence and recovery-owned destructive
       reconciliation are BOTH complete does the process enter OPERATIONAL:
       normal worker claims enabled, readiness=true.
```

- The three-state distinction — **BOOTSTRAP/MAINTENANCE**, **STORAGE_CONVERGING**,
  **OPERATIONAL** — is now the normative process-state model (D31), replacing the
  looser "process exists / maintenance-not-ready versus application operational /
  ready" phrasing this section previously used on its own. `/health/live` reflects
  process liveness in all three states (D33 — this is a maintenance-HTTP-availability
  requirement, not merely "insert a call before `yield`"); `/health/ready` is `false`
  in BOOTSTRAP/MAINTENANCE and STORAGE_CONVERGING and `true` only in OPERATIONAL (D20).
- A fresh install (`STORAGE_BACKEND=s3`, zero `file://` rows, zero recovery-owned
  Sources) passes through STORAGE_CONVERGING trivially — classification finds nothing
  in either set, convergence is vacuously satisfied, and the S3 probe (D21) is the
  only real check performed.
- A fresh filesystem install is entirely unaffected: `STORAGE_BACKEND=filesystem`
  skips the gate's migration logic outright (though the persistent-volume/S3-probe
  distinction in D20 still applies to readiness, and the schema-valid precondition
  D32 describes is unchanged from today).

### D8. Filesystem → S3 migration algorithm

**Scope (amendment, review finding B2 — narrows the original unconditional wording;
further narrowed by the deletion-semantics amendment, D40):** this algorithm applies
only to the **migration-owned** candidate set: authoritative `Source` rows with
`storage_uri = file://...` **and** `status NOT IN (DELETING, DELETED)` — i.e. `status
IN (PENDING, PROCESSING, ACTIVE, FAILED)`, the live `SourceStatus` values
(`domain/enums.py`) other than the two destructive-lifecycle ones. It never applies
to:

- `storage_uri = NULL` rows (owned by Remember's own recovery, D12/B1);
- `storage_uri = file://...` rows with `status = DELETING` (owned by the existing
  destructive pipeline lineage, D31/B2) — these are never uploaded to S3 by this
  algorithm, regardless of whether their local object is present or absent;
- **`storage_uri = file://...` or `s3://...` rows with `status = DELETED`** (D39's
  unresolved-cleanup tombstone, D40) — a `DELETED` Source with a retained locator is
  the durable record of *unresolved* storage cleanup (D39), never a live Source
  awaiting migration; uploading or otherwise touching it would resurrect/duplicate an
  artifact Sofias Memory's business-authoritative state already says is deleted.

For each Source in the migration-owned set, found by the startup scan (D7):

**A. Snapshot.** A short PostgreSQL read (`id, dataset_id, storage_uri, byte_size,
content_sha256, status`) — no open transaction/row lock held across the filesystem or
S3 I/O that follows (ADR-0009's no-lock-across-external-I/O discipline, already
enforced for `FinalizeStorageStep`/`StorageDeletionStep`, applies identically here).

**B. Validate the legacy URI** using the existing containment/identity rules —
literally `source_storage_path`'s existing checks (correct `DATA_DIRECTORY` root,
expected `dataset_id`, expected `source_id`, no escape) — reused, not reimplemented.

**C. Read the local original** (bytes).

**D. Validate `byte_size` and `content_sha256`** against the PostgreSQL snapshot. On
missing file or hash/size mismatch: **fail closed** — do not fabricate success, do not
rewrite `storage_uri`. This source is left `file://`, reported in convergence
diagnostics (D19), and blocks readiness (D19) until an operator resolves it (a future
recovery decision, not invented here).

**E. Compute the deterministic target `s3://` URI/key** (D6) — pure, no I/O.

**F. Inspect the target object:**

- absent → upload.
- present, matching expected `content_sha256`/size (Sofias Memory's own hash, not
  provider `ETag`, D11) → already copied by a prior attempt or a concurrent process
  (D10); treat as done, skip to step G.
- present, different content identity → **fail closed** with a storage-content
  conflict; never silently overwrite a conflicting deterministic object (this is the
  concrete backstop behind the "never destroy unexplained data" principle carried
  through from D24).

**G. Strongly verify** the S3 object contains the expected bytes, using Sofias
Memory's own SHA-256 identity (D11) — before PostgreSQL is ever repointed.

**H. Compare-and-swap, short transaction:**

```sql
UPDATE sources
   SET storage_uri = :new_s3_uri
 WHERE id = :source_id
   AND storage_uri = :old_file_uri   -- CAS on the value read in step A
COMMIT;
```

Only proceeds if the row still has the expected identity/old URI (closes the
concurrent-convergence race, D10). Commit.

**I. Only after that commit** may the legacy local file be deleted — and only by the
crash-safe cleanup pass in D9, never inline in the same pass that just wrote it,
because inlining would reopen exactly the ordering hazard rejected as Alternative K.

Ordering is fixed and never reordered:

```text
upload -> strongly verify -> CAS PostgreSQL to s3:// -> (separately) delete local copy
```

Never `upload -> delete local -> update PostgreSQL` (Alternative K) — a crash in that
window would leave PostgreSQL pointing at a deleted file, violating ADR-0002's
authority ordering the same way ADR-0010 D10 already forbids for administrative
delete.

### D9. Crash safety after the PostgreSQL repoint

Explicit crash window: S3 verified → PostgreSQL updated to `s3://` → process crashes
**before** deleting the local duplicate.

This state is safe by construction:

```text
PostgreSQL -> s3://...           (authoritative, already verified)
S3         -> valid object       (already strongly verified in step G)
filesystem -> redundant legacy copy (inert until cleaned)
```

The next startup convergence pass (this is itself the D7 gate, run again on the next
boot — no separate "cleanup pass" mechanism) must detect and remove such **confirmed**
local duplicates.

**Amendment (review finding M2):** the original wording assumed the legacy `file://`
path could simply be "the path that would have held its predecessor," but after D8
step H's CAS commits, `Source.storage_uri` no longer holds that `file://` value —
it has already become `s3://...`. The legacy predecessor path can therefore no longer
be read back from the Source row's current `storage_uri` at all. D35 freezes the exact,
non-discovery-based contract for deriving that path anyway (from `dataset_id`,
`source_id`, and `Source.mime_type` via the centralized extension mapping, D6's
amendment) and the exact pre-deletion verification sequence. This section's cleanup
rule is now stated in terms of that contract:

- A candidate is "confirmed" only when: `Source.storage_uri` is already `s3://` for
  the deterministic key matching this Source's `dataset_id`/`source_id` (D6), **and**
  the D35 locator contract can unambiguously derive a legacy local path for this
  Source, **and** that exact path exists with content matching the same
  `content_sha256`. Only then is deletion permitted.
- If a candidate local file exists but its content hash does not match: **fail
  closed**, leave it untouched, surface it in diagnostics — never destroy unexplained
  data (same principle as D8 step F's conflict case, and the same "leave it" default
  ADR-0010 already uses for `AMBIGUOUS` cancellation recovery).
- If D35's locator contract cannot unambiguously derive a legacy path for a migrated
  Source (an unmappable/unknown `mime_type`): **fail closed** — do not guess, do not
  delete, surface a convergence/integrity error, readiness stays blocked (D19) until
  explicitly resolved.
- Deletion is derived exclusively from the deterministic Source identity (D6's
  `dataset_id`/`source_id`-only key shape) plus D35's locator contract — never a
  directory scan, `glob`, or "first file found" of unrelated content.

Post-convergence invariant this ADR requires and later tests must assert (D19 item
17): zero unresolved authoritative `file://` Sources remain for anything the S3
target could confirm; zero confirmed redundant legacy copies remain for migrated
Sources; everything else under `DATA_DIRECTORY` — `_ingress/`, `_system/`, and any
content this ADR does not recognize — is untouched (D24).

### D10. Concurrent startup

No PostgreSQL transaction, row lock, or advisory lock is held across S3/network I/O —
this preserves the project's existing no-long-lock-over-external-I/O principle
(ADR-0009 §D "Commit before I/O"). Correctness under more than one process attempting
startup convergence concurrently comes entirely from:

- deterministic S3 keys (D6);
- idempotent upload/verification (D8 step F: re-uploading identical bytes to the same
  key, or finding it already present with matching identity, is a no-op success, not
  an error);
- PostgreSQL compare-and-swap of `storage_uri` (D8 step H) — the losing writer's CAS
  simply matches zero rows and moves on, exactly the same pattern ADR-0009 §M already
  uses for concurrent manual-retry `INSERT`s racing on a unique idempotency key;
- re-read/revalidation when another process appears to have won: if a process
  observes the local file missing at step C, it re-reads PostgreSQL (a fresh, cheap
  read) — if `Source.storage_uri` is already the expected verified `s3://` value,
  convergence for that Source is already done by the other process; if PostgreSQL
  still shows `file://` while the local file is missing, that is **not** "someone
  else migrated it" (no verified evidence of that) — fail closed exactly as D8 step D
  requires for any other missing-local-file case;
- idempotent exact-file cleanup (D9): re-running the confirmed-duplicate check is
  itself side-effect-free once a duplicate is already gone.

No new migration-coordinator table, no new advisory lock, and no new "is convergence
in progress" flag is introduced. The single-replica MVP assumption already documented
in ADR-0009 (`Explicit Non-Goals`) means concurrent-startup correctness here is a
safety net for restart races and operator error, not a scale requirement — the design
above is still fully correct under N concurrent attempts because every step is
either read-only, deterministic-and-idempotent, or a PostgreSQL CAS.

### D11. S3 object integrity — cheap vs. strong verification

`Source.content_sha256` remains the sole authoritative content identity (ADR-0002).
S3 `ETag` is **never** assumed to equal SHA-256 — it is not, in general (multipart
uploads, some server-side-encryption modes, and provider-specific behavior all break
that assumption; Alternative H).

Two distinct verification tiers, both defined by this ADR so implementation cannot
blur them:

- **Cheap / idempotency check** (D8 step F, D10): a `HEAD`-equivalent existence check
  plus comparison against Sofias-Memory-owned object metadata (at minimum, the
  expected SHA-256 and byte size, stored as the adapter's own object metadata/tag at
  upload time) — sufficient to decide "already copied, skip" without downloading the
  object.
- **Strong migration verification** (D8 step G, required before any CAS/PostgreSQL
  repoint): sufficient evidence that the object's actual bytes match
  `content_sha256` — e.g. a full read-back hash, or a provider capability proven
  equivalent to it for the exact upload path used. `ETag`-only or ambiguous
  multipart-`ETag` evidence never satisfies this tier by itself.

The exact mechanism for strong verification (full download-and-hash vs. an
alternative proven equivalent) is an SM-level implementation choice, not frozen here
beyond the requirement that it never trusts `ETag` as SHA-256.

### D12. Normal Remember finalization under S3

`FinalizeStorageStep`'s existing idempotency contract (SM-513: an external write
outside the persist transaction; persist records `storage_uri` idempotently; a retry
determines whether the expected final object already exists; expected-existing
content = success; conflicting existing content = permanent conflict) is preserved
verbatim, generalized from filesystem to "whatever `SourceStorageRouter.finalize`
resolves to":

```text
durable local ingress (_ingress/<run_id>, unchanged, D3)
    -> finalize into deterministic target (filesystem path or S3 key, per STORAGE_BACKEND)
    -> verify expected identity
    -> persist storage_uri (file:// or s3://) idempotently
    -> garbage-collect local ingress (delete_ingress_artifact, unchanged)
```

Under S3, a timeout **after** the remote PUT may be ambiguous (network failure after
the object landed but before the response was observed). Retry must inspect the
deterministic target key (same cheap check as D11) and continue idempotently — never
blindly issue a second PUT that could, in principle, create a second unrelated
object. This is a direct extension of the existing filesystem contract's own
"file already present at the expected path = idempotent replay" rule
(`final_storage_content_matches`), not a new idea.

**Amendment — B1: `storage_uri = NULL` legacy-object recovery across a backend
change (review blocker, resolved here, not by the startup scanner).** The existing
`FinalizeStorageStep` contract already tolerates this crash window under the
filesystem backend alone:

```text
1. prepare_and_ingest already committed the Source (storage_uri = NULL);
2. FinalizeStorageStep.execute() writes the deterministic final filesystem object;
3. the final file is verified by content hash;
4. _ingress/<run_id> is garbage-collected;
5. process crashes BEFORE FinalizeStorageStep.persist();
6. therefore Source.storage_uri remains NULL, durably.
```

Durable state after crash: `Source.storage_uri = NULL`;
`DATA_DIRECTORY/<dataset_id>/<source_id>/original<extension>` present and valid;
`DATA_DIRECTORY/_ingress/<run_id>` absent. Today, a plain filesystem retry recovers
by re-observing that deterministic local object. **ADR-0011 must preserve this
recovery property even if the operator changes `STORAGE_BACKEND=filesystem → s3`
before that `PipelineRun` resumes** — the crash window is orthogonal to which backend
is currently configured; only the eventual write target changes.

Frozen recovery rule: a resumable Remember `FinalizeStorageStep` executing under
`STORAGE_BACKEND=s3` **must** be able to recover when all of the following hold:

- `Source.storage_uri` is `NULL`;
- the deterministic S3 target object is absent;
- the run's local `_ingress/<run_id>` artifact is absent;
- the exact deterministic legacy finalized filesystem object exists at
  `DATA_DIRECTORY/<dataset_id>/<source_id>/original<extension>`;
- that object's `byte_size` and content hash match the Source's already-committed
  `byte_size`/`content_sha256`, and its extension matches the canonical extension for
  the Source's `mime_type` (D6's amendment).

In that state, the verified legacy finalized filesystem object is valid recovery
input for the **existing** Remember finalization attempt — not a new Source, not a
re-fetch:

```text
legacy finalized filesystem object (found by the SAME deterministic path
    FinalizeStorageStep would already resolve for this Source under filesystem,
    reusing final_storage_path -- no discovery/glob)
    -> verify identity/hash/size against the already-committed Source row
    -> upload to the deterministic S3 target (D6, D8's upload primitive reused)
    -> strongly verify the S3 object (D11)
    -> return the target s3:// URI to the SAME FinalizeStorageStep attempt
    -> normal PostgreSQL persist() records Source.storage_uri = s3://... exactly as
       any other successful finalization would
    -> only after that persist is durable may the now-redundant legacy local
       object be garbage-collected (never inline before the persist -- same
       ordering discipline as D8's "upload -> verify -> CAS -> (separately) delete")
```

This recovery **must not**:

- create a second `Source` row;
- re-fetch URL content or re-read the original client-supplied bytes from anywhere
  other than the already-verified legacy local object;
- fabricate new input to the pipeline;
- fail with `REMEMBER_INGRESS_MISSING` (or an equivalent ingress-required error)
  merely because the backend changed underneath an otherwise-recoverable attempt;
- perform S3 I/O from `FinalizeStorageStep.persist()` — the upload/verify sequence
  above runs in `execute()`, exactly like every other external effect this ADR
  defines; `persist()` stays PostgreSQL-only (D27);
- write `storage_uri` before the S3 object has been strongly verified (D11) — same
  ordering rule as D8 step H.

If neither (a) durable `_ingress` bytes, nor (b) a verified deterministic legacy
finalized object, is available, the step fails closed exactly as it does today
(`REMEMBER_INGRESS_MISSING` or equivalent) — this amendment adds a second valid
recovery input, it does not weaken the existing fail-closed floor.

**This recovery belongs to the existing Remember pipeline's own step-retry semantics
(ADR-0009), not to a generic startup scanner.** The startup storage convergence gate
(D7) must never treat an arbitrary `storage_uri = NULL` Source as a migration
candidate on its own initiative — D7's Scope note and D8's Scope note both make this
exclusion explicit. Ownership stays with `FinalizeStorageStep`'s normal claim/retry
path; the startup gate's only relationship to this case is that STORAGE_CONVERGING
must not race or duplicate it (D31).

### D13. Cognify rehydration

`source_storage_path_for_cognify` currently converts `storage_uri` into a `Path` and
returns it for direct filesystem reads. After this ADR, Cognify's rehydration call
site instead calls `SourceStorageRouter.read(...)`, supplying `storage_uri`,
`dataset_id`, `source_id`, `expected_byte_size`, `expected_content_sha256`, and the
existing configured maximum size (`Settings.max_source_size_mb`, already enforced
today). The router returns verified bytes or a typed failure
(`DependencyUnavailableError`, matching the existing error today) — Cognify never
learns, and never needs to learn, whether the bytes came from `file://` or `s3://`.
LLM/embedding provider transaction boundaries are entirely unaffected.

### D14. Forget and administrative Dataset DELETE

`delete_source_storage` (Forget, `services/forget.py:872`) and `DeleteStorageStep`
(ADR-0010 D9 step 4, `pipelines/steps/dataset_delete.py`) both delete finalized Source
originals through the same URI-aware router (`SourceStorageRouter.delete`), routed by
`storage_uri`'s scheme (D4) — never by `STORAGE_BACKEND`, since a Dataset being
deleted may contain a mix of `file://` and `s3://` Sources if it was only partially
converged (D5).

Existing external-effect semantics are preserved, and extended by the deletion-
semantics amendment (D37):

- delete of an existing object → deleted (`StorageDeleteStatus.DELETED_NOW`);
- delete of an already-absent object → success/already-absent
  (`StorageDeleteStatus.ALREADY_ABSENT`) — this is the property D15/D38 require the
  adapter to actually guarantee with positive evidence, not merely assume;
- **(amendment, D37) an operationally-recognized inability to determine or complete
  physical deletion** (credentials unavailable, bucket/endpoint unreachable,
  `AccessDenied`, timeout, Object Lock/retention, or the storage URI cannot be safely
  resolved) → `StorageDeleteStatus.UNRESOLVED`, a **typed, successful step outcome**,
  not a `PipelineStep` failure — D37 defines this fully;
- an interrupted attempt (a crash mid-operation, before the step itself reaches any
  terminal outcome) is handled by ADR-0009 §I / ADR-0010 D9's existing `AMBIGUOUS`
  `CancellationRecoveryMode` classification, unchanged — retry re-observes the exact
  target and reclassifies into `DELETED_NOW`/`ALREADY_ABSENT`/`UNRESOLVED` per D37's
  "Recovery/retry" rule, never by assuming success and never by inferring deletion
  from PostgreSQL state alone.

`memory_only=true` Forget continues to preserve the Source object exactly as today —
this ADR adds no new code path that could delete storage on that flag. Full Source
Forget, Dataset Forget, Forget Everything, and administrative Dataset DELETE preserve
their existing destructive semantics for PostgreSQL/Neo4j authoritative memory;
D37 amends only whether *physical* Source-original cleanup must be proven before
those business-authoritative transitions may complete.

### D15. S3 versioning / destructive-delete semantics

This is solved explicitly, not assumed away.

**Amendment (contract-completeness round) — narrows this section's opening claim,
which predates D37 and is no longer globally true.** The original wording ("Sofias
Memory's Forget/Dataset-DELETE contract requires that the Source original actually
cease to be retained") stated an unconditional requirement that D37 has since made
conditional. The corrected invariant:

> Whenever Sofias Memory **reports** physical storage cleanup as confirmed
> (`DELETED_NOW` or `ALREADY_ABSENT`), the Source original must actually satisfy the
> strong absence semantics this section and D38 define. Sofias Memory's business
> deletion itself is **not** conditioned on that physical confirmation being reached
> (D37) — only the *claim* of confirmation is held to this strong standard.

Restated per outcome, for a versioned bucket specifically:

- `DELETED_NOW` ⇒ all versions/delete markers removed **and** absence verified;
- `ALREADY_ABSENT` ⇒ positive inspection proves no retained object/version exists;
- `UNRESOLVED` ⇒ the object **may** still physically exist; business deletion may
  still succeed (D37); `storage_uri` remains preserved (D39).

This amendment removes only the obsolete implication that physical absence is
mandatory for business deletion to succeed. It does **not** weaken this section's
versioning proof requirement below — a `DeleteObject` call that merely inserts a
delete marker on a versioned bucket, leaving prior versions retrievable, still does
**not** satisfy `DELETED_NOW`; it satisfies, at best, nothing yet (the adapter must
keep working through the version list, or return `UNRESOLVED` if it cannot).

Required S3 adapter behavior:

- If the configured bucket has versioning enabled, `delete()` must delete **all**
  versions and delete markers for the exact deterministic key (D6) and then verify no
  retained version remains for that key **before** it may report
  `StorageDeleteStatus.DELETED_NOW`.
- The adapter must never report `StorageDeleteStatus.DELETED_NOW` merely because the
  current version is hidden behind a fresh delete marker while older versions are
  still retrievable — that state is not "deleted."
- **(amended by D37, superseding this section's original wording) if bucket
  permissions, Object Lock, a retention policy, legal hold, or an incompatible
  provider behavior prevents the required deletion from being proven complete, the
  adapter returns `StorageDeleteStatus.UNRESOLVED`** — a positively-typed, recognized
  operational outcome, not `StorageDeleteStatus.DELETED_NOW` and not
  `StorageDeleteStatus.ALREADY_ABSENT` (D37, D38). The step itself still completes
  successfully with that typed result; per D37, this no longer by itself fails the
  `PipelineStep` or blocks the pipeline's business finalizer. What the adapter must
  never do is report `DELETED_NOW`/`ALREADY_ABSENT` without the positive evidence
  this section requires — that discipline is unchanged and is the one part of the
  original wording this amendment does not relax.
- Deletion targets **only** the exact deterministic Source key — no prefix-wide or
  bucket-wide destructive operation is ever issued (same "no global wipe" principle
  ADR-0010 D27 already applies to Neo4j projection deletion).
- Required S3 permissions for this contract (at minimum: delete-object,
  delete-object-version where versioning is in use, and enough list/head capability to
  verify no version remains) must be documented alongside the eventual
  implementation's configuration reference.

### D16. S3 compatibility scope

Supported: AWS S3, and explicitly configured S3-compatible endpoints that implement
the specific operations this contract requires (deterministic-key PUT/GET/HEAD,
exact-key DELETE including version enumeration/deletion, and the integrity mechanism
D11 needs). This ADR does **not** claim universal compatibility with every
S3-compatible product.

Configuration surface (added to `Settings`, following existing naming/validation
conventions in `config.py` — e.g. `SecretStr` for credential fields, a `field_validator`
for the endpoint URL matching the existing `validate_http_url` pattern):

```text
STORAGE_BACKEND               filesystem | s3   (D2)
STORAGE_S3_BUCKET
STORAGE_S3_PREFIX
STORAGE_S3_REGION
STORAGE_S3_ENDPOINT_URL        # optional, for S3-compatible endpoints / MinIO
STORAGE_S3_ACCESS_KEY_ID       # optional if provider credential chain is used
STORAGE_S3_SECRET_ACCESS_KEY   # optional if provider credential chain is used
STORAGE_S3_SESSION_TOKEN       # optional
```

Any further addressing/TLS setting proven necessary during implementation follows the
same naming convention. Credential design allows either explicit environment-based
static credentials or, where the selected SDK supports it cleanly, the standard
provider credential chain / instance role — without forcing static secrets on
deployments that do not need them.

Never logged, never persisted, never returned to a client, and never included in the
config fingerprint (D18): credentials, `STORAGE_S3_ENDPOINT_URL`,
`STORAGE_S3_ACCESS_KEY_ID`/secret/session token. `storage_uri` itself, per D6, already
excludes all of this by construction.

### D17. Relationship to ADR-0005

ADR-0005 prohibits `[project.optional-dependencies]`, extras, plugin registries, and
provider-abstraction packages "used to make optional backends appear interchangeable."
This ADR **amends** ADR-0005, not silently ignores it:

- `filesystem` and `s3` are the complete, closed, first-party, versioned set of
  supported Source-storage backends after this ADR ships — not a plugin point.
- No plugin registry, no dynamic backend loading, no `STORAGE_PROVIDER=<free text>`
  style open-ended selection — `STORAGE_BACKEND` is a two-value closed enum (D2).
- No optional dependency extra (no `[s3]` extra). Whatever S3 SDK is chosen at
  implementation time is a normal `[project.dependencies]` entry, installed for every
  deployment exactly like `asyncpg`/`neo4j`/every other current runtime dependency —
  consistent with ADR-0005's "all supported runtime dependencies for v1 belong to the
  main installation."
- `SourceObjectStorage`/`SourceStorageRouter` (D4) is a **narrow, closed, two-adapter**
  boundary, not a generic provider-abstraction package of the kind ADR-0005
  specifically rejected — it exists because two backends must coexist for one release
  cycle (D5's mixed-URI requirement), not to make backends "appear interchangeable"
  to some hypothetical third implementation. Any future backend beyond
  `{filesystem, s3}` requires its own explicit ADR, exactly like any other supported-stack
  change under ADR-0005.

### D18. Config fingerprint exclusion

`build_config_fingerprint_payload` (`config.py:307`) currently hashes `llm`,
`embeddings`, `chunking`, `retrieval`, `improve`, and `prompt_versions` — the
*semantic processing* configuration ADR-0009 §J's `config_fingerprint` mismatch check
exists to protect (stale-`RUNNING` recovery fails a run whose semantic configuration
changed underneath it, rather than silently resuming under new LLM/embedding/chunking
behavior).

`STORAGE_BACKEND`, `STORAGE_S3_*`, and any future storage-location setting are **not**
added to this payload. Storage location is physical persistence configuration, not
semantic Cognify/Recall processing configuration — a Source's bytes and their SHA-256
identity are unchanged by *where* they are stored, so a `PipelineRun` resuming after a
filesystem→S3 flip is not processing "different" work in the sense §J's mismatch
check exists to catch. Adding storage settings to the fingerprint would make an
otherwise fully resumable `PipelineRun` fail purely because the physical backend
changed underneath it mid-flight — exactly the outcome D7's "no required manual
migration step" goal is designed to avoid. Existing pipeline recovery fingerprints
continue protecting only semantic processing configuration; this ADR adds no new
fingerprint dimension.

### D19. Startup failure policy

Storage convergence fails **closed**. At minimum, each of the following blocks
worker start / readiness:

- S3 credentials invalid or bucket unreachable (D21 probe failure);
- required S3 permissions missing (D21, D15);
- a deterministic migration target already contains conflicting bytes (D8 step F);
- **(amended per review finding B2, precise rule; corrected again in the editorial
  consistency pass — "non-`DELETING`" literally includes `DELETED`, which D40
  excludes from migration/file-presence inspection entirely, before this bullet
  would even apply)** a **live, migration-eligible** (`status IN (PENDING,
  PROCESSING, ACTIVE, FAILED)`, D40) authoritative Source's `file://` object is
  missing on disk, or its SHA-256/size does not match the PostgreSQL-recorded
  identity (D8 step D, D34 Case A). A `DELETING` Source with a missing local
  `file://` object is **not** automatically this failure — it is classified per D34's
  Case B (proven lineage) or Case D (no proven lineage, a distinct failure bullet
  below), never folded into this generic integrity-failure condition. A `DELETED`
  Source is excluded from this bullet even more fundamentally: D40/D34 Case C never
  inspects a `DELETED` Source's storage object presence or absence for convergence
  purposes at all — its retained or missing artifact is simply irrelevant to
  migration, not a passing/failing check;
- the target S3 object cannot be strongly verified (D8 step G / D11);
- a PostgreSQL repoint cannot be safely reconciled (e.g. the CAS target row no longer
  matches any expected pre/post state — an internal-consistency condition, not a
  normal race outcome, which D10's races already resolve without reaching this case);
- a confirmed legacy duplicate (D9) cannot be safely classified (hash mismatch, or
  D35's locator contract cannot unambiguously derive a legacy path);
- recovery-owned destructive lineage (D31) fails to reach its legitimate durable
  terminal state within the process's normal ADR-0009 retry/stale-recovery bounds —
  STORAGE_CONVERGING must not silently proceed to OPERATIONAL while such lineage is
  still unresolved;
- a mutation is observed inside the managed S3 Source namespace (D36) that breaks a
  deterministic-identity assumption this contract depends on (e.g. an object at a
  Sofias-Memory-owned key whose content does not match the integrity metadata Sofias
  Memory itself wrote) — treated as a configuration/integrity violation, not silently
  trusted or overwritten;
- **(contract-completeness amendment)** a `DELETING` + `file://` Source with a
  missing local object and **no** provable compatible destructive `PipelineRun`
  lineage (D34 Case D) — this is never migrated, never classified recovery-owned, and
  blocks STORAGE_CONVERGING → OPERATIONAL as an internal consistency failure requiring
  operator diagnosis, distinct from the ordinary Case A integrity failure above.

No partially migrated backend is ever silently treated as fully operational. Log
lines for every phase above must include: backend, Source id, migration phase,
whether the condition is retryable-transient versus an integrity/configuration
problem, and running counts/progress — and must never include source content,
credentials, or `STORAGE_S3_ENDPOINT_URL`/access-key values.

### D20. Readiness

Extends the existing readiness contract (`docs/operations.md` section 2's
"`/health/ready` detects... reports `not_ready`" pattern) with a source-storage
dimension, distinguishable in observability from PostgreSQL/Neo4j/worker failure.

**Amendment (review finding M1):** readiness is now defined directly against D31's
three-state process model rather than the original two-way "process exists /
operational" split:

- **BOOTSTRAP/MAINTENANCE** and **STORAGE_CONVERGING** both report `/health/ready =
  NOT_READY`; `/health/live` reports healthy in both, because the process is up and
  serving its maintenance HTTP surface (D33) even though business operations are not
  yet enabled.
- **OPERATIONAL** is the only state in which `/health/ready` may report ready.
- `filesystem`: readiness verifies the persistent application-data root is present
  and usable (the same requirement that already exists implicitly today via the
  mandatory volume mount) — no behavior change from today; there is no
  STORAGE_CONVERGING phase for this backend (D7).
- `s3`: readiness requires (a) the configured S3 target passed its startup probe
  (D21), (b) startup storage convergence over the migration-owned set completed
  successfully (D7, D8), (c) recovery-owned destructive lineage (D31) has reached its
  legitimate durable terminal state, and (d) the runtime storage adapter is currently
  operational. Readiness must **not** perform an expensive full-bucket or
  full-Source-set verification on every `/health/ready` call — the startup gate (D7)
  is where the expensive, one-time convergence work happens; steady-state readiness
  reads a cheap, already-computed "convergence succeeded" state plus a lightweight
  adapter health signal.
- No secret value is ever exposed through `/health/ready` or `/api/v1/info`.

### D21. S3 startup probe

Startup S3 validation must exercise the actual operations this contract needs — put,
get/head, and delete (including version-aware delete where versioning is enabled) —
rather than only confirming "the bucket exists / credentials parse." The eventual
implementation may use a namespaced temporary probe object under a reserved system
prefix (distinct from the `v1/sources/...` key space, D6) to validate these
operations; probe cleanup must itself be idempotent (a leftover probe object from an
interrupted prior boot must not fail a later probe). Exact probe object naming and
cleanup mechanics are implementation-level detail; the capability requirement — prove
put/get/delete work, not just connectivity — is frozen here.

### D22. Backup / restore

The mandatory persistent `/data/sources` volume remains required in both modes.

- **filesystem mode** authoritative durable data: PostgreSQL; `/data/sources`
  finalized originals plus other persistent application data (`_ingress`, future
  `_system`).
- **s3 mode** authoritative durable data: PostgreSQL; the configured S3 bucket's
  Source originals; `/data/sources` persistent application data, including durable
  in-flight ingress (D3) and any future persistent application assets/state — the
  volume's authority over *originals* narrows, but it does not become optional or
  disposable (this rejects Alternative B/L).
- Neo4j remains reconstructible/non-authoritative in both modes, unchanged from
  ADR-0002/ADR-0008.

`docs/operations.md`'s backup/restore procedures will need a separate S3-mode section
once implemented (D19 in "Repository areas that later implementation must change,"
below) — not written by this ADR.

### D23. No public business API change

This ADR adds no presigned upload/download endpoint, no direct browser/client S3
access, no bucket administration API, and no new public Remember semantics. Clients
continue to send text/files/URLs to Sofias Memory exactly as today
(`POST /api/v1/remember*`, unchanged request/response contracts). S3 is purely an
internal persistence implementation behind `SourceStorageRouter`.

`storage_uri` is not present in any public `Source`-facing response schema today
(confirmed by inspection of `schemas/` — Source's public projections expose identity,
kind, status, and content metadata, never the storage location itself); this ADR
preserves that. No public schema change is required by this ADR for that reason.

### D24. Local filesystem cleanup safety

Migration cleanup (D9) may remove only finalized Source-original files whose identity
is proven by the deterministic Source-key pairing (D6) plus a matching confirmed
PostgreSQL `s3://` repoint. It must never recursively clear `DATA_DIRECTORY`, and must
never delete `_ingress/`, `_system/`, any unrecognized future persistent data, or any
unexplained file. Existing legacy Source-directory cleanup (removing an empty
`<dataset_id>/<source_id>/` directory after its one file is confirmed gone) may remove
only directories that are actually empty — this mirrors, and does not need to change,
the containment discipline `source_storage_path`/`write_final_storage_bytes` already
enforce. Unknown content anywhere under `DATA_DIRECTORY` is left untouched by
construction: nothing in D8/D9's algorithm ever performs a directory scan for deletion
candidates — deletion candidates are always derived from a specific Source row's own
identity, never discovered by listing a directory.

### D25. S3 → filesystem / S3 → S3 relocation — explicit non-goals

Not implemented or promised by this ADR:

- automatic S3 → filesystem reverse migration;
- automatic relocation between S3 buckets or prefixes;
- multi-cloud/generic blob-storage plugin support;
- direct-to-S3 client uploads;
- CDN or public object serving;
- arbitrary user-controlled object keys.

`SourceStorageRouter`'s scheme-based read/delete routing (D5) does not make future
relocation architecturally impossible — a future relocation feature could reuse the
same deterministic-key/CAS/verify pattern this ADR defines for filesystem→S3 — but no
such feature is implemented or promised here.

### D26. PostgreSQL schema

`Source.storage_uri` (`infrastructure/postgres/models/source.py:88`) is already an
unrestricted, nullable `Text` column — it already holds arbitrary URI text, including
`s3://...`, with zero constraint change. **ADR-0011 requires no `Source` schema
migration merely to introduce S3.**

No storage-migration-state table, and no new `Source` columns (a
`storage_migrated_at`, a `storage_backend` column mirroring `storage_uri`'s own
scheme, etc.) are introduced. The architecture analysis above does not find an
invariant this ADR needs that `storage_uri` + deterministic keys (D6) + idempotent
external operations (D8, D14) + compare-and-swap/revalidation (D8 step H, D10) cannot
already satisfy. Any future schema change discovered necessary during implementation
still follows the existing explicit-Alembic-only policy (§14 of `AGENTS.md`) and, per
D26's own principle, would need to justify itself against an otherwise-unsatisfiable
invariant rather than being added preemptively.

### D27. Relationship to ADR-0009's pipeline recovery contract

Storage external I/O remains outside PostgreSQL persist transactions in both
directions (finalize, delete) — unchanged from today's `FinalizeStorageStep`/
`StorageDeletionStep` boundary discipline. `FinalizeStorageStep`'s `persist()` phase
still only records an already-computed `storage_uri` string; it never itself performs
S3 I/O, exactly as it never performs filesystem I/O today (`final_storage_uri` is
pure/no-I/O, `write_final_storage_bytes` is the I/O, called from `execute()`).
`StorageDeletionStep`/`DeleteStorageStep` remain `AMBIGUOUS` `CancellationRecoveryMode`
(ADR-0009 §I, ADR-0010 D9) — S3 does not make an orphaned delete attempt any more
provable from PostgreSQL-only state than a filesystem delete was; if anything, D15's
versioning semantics make it *less* trivially provable, reinforcing rather than
weakening the existing `AMBIGUOUS` classification. Retry safety, idempotency,
cancellation recovery, and stale-run recovery are all unchanged by this ADR — the
storage backend swap is invisible to ADR-0009's lifecycle machinery, which is exactly
the point of routing through `SourceStorageRouter` (D4) instead of teaching the engine
about backends.

**Amendment (review finding B2.1) — qualifying "unchanged":** the statement above is
precise for step-level and run-level machinery, but the amendment requires one
explicit qualification at the *worker-claim* level, not the step/engine level: during
STORAGE_CONVERGING (D31), normal `PipelineRun` claims (any pipeline type, any
dataset) are held back exactly as they are during today's pre-worker-start recovery
window, **except** that already-existing Forget/`DATASET_DELETE` lineage whose
progress is required to reconcile a B2-classified recovery-owned destructive state
(D31) may continue to be claimed and executed through the unmodified ADR-0009
claim/retry engine. This is a claim-eligibility filter on *which* runs the existing
engine is allowed to pick up during this one process state, not a second engine, not
a new lifecycle state for `PipelineRun`/`PipelineStep`, and not a change to any
transition matrix in ADR-0009 §A/§B. No fabricated success, no PostgreSQL lock/
transaction spanning storage I/O, and no second pipeline engine are introduced by
this qualification — D31 states the exact mechanism and its limits. Ordinary
Remember/Cognify/Improve/Forget/`DATASET_DELETE` business transitions remain owned
exclusively by the existing pipeline lifecycle in every process state; storage
convergence never performs a business transition (`ACTIVE`→`DELETING`→`DELETED`,
`SUCCEEDED`, etc.) itself.

### D28. Relationship to ADR-0010

ADR-0010's `delete_storage` step (D9 step 4) is described in filesystem-deletion
terms because that was the only backend that existed at the time. ADR-0011
**generalizes** that step's external effect from "filesystem deletion" to "Source
object storage deletion via `SourceStorageRouter.delete`," routed by `storage_uri`
scheme (D5, D14) exactly like Forget's own storage step.

**Amendment (deletion-semantics round, D37) — explicit, deliberate supersession of
one specific ADR-0010 requirement, stated precisely so the scope of the change is not
mistaken for a general weakening.** ADR-0010's original text required `delete_storage`
to *complete* (i.e., durably prove deletion or absence, exact-key, never a
global/prefix wipe) before `finalize_tombstone`. **ADR-0011 supersedes only that one
clause.** The corrected rule:

- `delete_storage` remains an external, `AMBIGUOUS`-classified effect (ADR-0009 §I,
  ADR-0010 D9) and must still be **attempted** before `finalize_tombstone` — this part
  is unchanged;
- its durable result must distinguish `DELETED_NOW`, `ALREADY_ABSENT`, and (new)
  `UNRESOLVED` (D37);
- `finalize_tombstone` **may now proceed after a typed `UNRESOLVED` outcome** — it is
  no longer blocked waiting for physical proof that will not come while the backend
  is inaccessible;
- when the outcome is `UNRESOLVED`, `Source.storage_uri` is **preserved**, not cleared
  (D39) — the tombstone's retained locator is the durable record of what remains
  unresolved.

Every other part of D9's/ADR-0010's contract is unchanged and remains in force: exact-
key targeting only, never a global/prefix wipe; no PostgreSQL lock/transaction across
storage I/O; no external I/O inside `persist()`; idempotent retryability;
`CancellationRecoveryMode = AMBIGUOUS` for an interrupted (crashed) attempt; and the
requirement that `DELETED_NOW`/`ALREADY_ABSENT` may only ever be reported with the
positive evidence D15/D38 already require. ADR-0010 itself is not rewritten by this
task; this section is the explicit supersession/generalization note ADR-0010's own
text anticipates, now covering both the storage-backend generalization (filesystem →
router-routed) and this deletion-outcome refinement.

**Amendment (review finding B2) — the backend transition does not bypass ADR-0010's
own state machine.** A `Source`/`Dataset` sitting `DELETING` with `storage_uri =
file://...` and a missing local object, discovered while `STORAGE_BACKEND=s3`, is
never treated by this ADR as evidence to be migrated, nor as evidence to finalize
`DELETED`/tombstone status directly from the storage-convergence subsystem. D9 step 4
(`delete_storage`, `AMBIGUOUS`) and ADR-0010 D9 step 4 (`DeleteStorageStep`,
`AMBIGUOUS`) remain the sole steps that classify "already-absent" as reconcilable
success, and `finalize_target`/`finalize_tombstone` remain the sole steps that ever
commit the resulting business-terminal state — both still run only through the
existing engine, per D9's/ADR-0010's own step definitions, unmodified by this ADR.
D31 freezes exactly how STORAGE_CONVERGING lets that existing lineage keep making
progress without turning storage convergence into a second Forget/`DATASET_DELETE`
implementation.

### D29. Configuration switch behavior — operator experience

Existing filesystem installation:

```env
STORAGE_BACKEND=filesystem
DATA_DIRECTORY=/data/sources
```

Operator adds valid S3 settings (D16) and sets `STORAGE_BACKEND=s3`, then
redeploys/restarts. Expected bootstrap, now stated against D31's state model
(amended per review findings M1/M1.1/M1.2 — the original 11-step list conflated
schema-gating, migration, and recovery-owned reconciliation into one undifferentiated
sequence):

```text
0. process starts; enters BOOTSTRAP/MAINTENANCE (D31); maintenance HTTP surface up
   (D33); /health/live = healthy; /health/ready = NOT_READY;
1. load settings (Settings now validates STORAGE_S3_* per D16 when backend=s3);
2. confirm PostgreSQL schema is at the expected Alembic head (D32 -- if not, remain
   in BOOTSTRAP/MAINTENANCE indefinitely; Alembic is never invoked automatically;
   operator runs `alembic upgrade head` explicitly, per docs/operations.md);
3. schema confirmed current -> enter STORAGE_CONVERGING (D31);
4. probe S3 (D21);
5. classify durable Sources: migration-owned (status NOT IN (DELETING, DELETED),
   file://, D8/D40) vs. recovery-owned (DELETING, file://, missing local object,
   AND a provable compatible destructive PipelineRun lineage -- D34's amended
   Case B classifier) vs. excluded-NULL (owned by Remember's own retry path,
   D12/B1 -- storage convergence does not touch these at all) vs. excluded-DELETED
   (D39/D40 tombstones, never touched) vs. unresolvable-integrity-failure (DELETING
   + file:// + missing object + NO provable lineage -- D34 Case D; fails closed,
   surfaced for operator diagnosis, D19);
6. migrate the migration-owned set idempotently (D8), strongly verify each in S3
   (D8 step G, D11), CAS-repoint PostgreSQL source-by-source (D8 step H);
7. concurrently/sequentially (implementation choice), the existing pipeline engine
   is allowed to keep claiming and progressing only the recovery-owned destructive
   lineage identified in step 5 (D31) -- no other normal PipelineRun claims occur
   yet;
8. clean confirmed local duplicates using the D35 legacy-locator contract (D9);
9. verify convergence: migration-owned set fully repointed, recovery-owned lineage
   reached its legitimate durable terminal state, no unresolved D19 failure
   condition remains;
10. verified -> enter OPERATIONAL: start/resume normal worker operation for all
    pipeline types (existing worker.start(), unaffected); readiness becomes ready
    (D20);
    not verified -> remain STORAGE_CONVERGING, /health/ready stays NOT_READY, no
    time limit is imposed on this state (D33) -- progress is observable via logs/
    metrics, not via an arbitrary deadline.
```

`/data/sources` remains mounted permanently — there is no operator step "remove the
source volume," and no mandatory manual `storage migrate` command for this normal
transition (D30).

### D30. Optional operational CLI — not required for the normal path

Automatic startup convergence (D7) is the supported normal path. A future
implementation may still add diagnostic/manual tooling, e.g.:

```text
sofias-memory storage status
sofias-memory storage verify
sofias-memory storage migrate
```

as operational/recovery conveniences (mirroring `scripts/rebuild_graph.py`'s role for
Neo4j). This ADR does not specify or require such a CLI, and correctness of the normal
filesystem→S3 upgrade path must never depend on an operator remembering to run one.

### D31. Recovery-owned destructive work and the process state model

**New section, added by this amendment (review findings B2.1, M1) — resolves the
worker/readiness chicken-and-egg the original D7 left implicit.**

Three observable process states, in order, each with a fixed HTTP/worker contract:

**BOOTSTRAP/MAINTENANCE**

- process is alive;
- a maintenance HTTP surface is available (D33);
- `/health/live` = healthy/200;
- `/health/ready` = `NOT_READY`;
- normal business API operations (`POST /api/v1/remember*`, `cognify`, `recall`,
  `improve`, `forget`, `DELETE /api/v1/datasets/{id}`, etc.) do not execute;
- normal worker claims are disabled;
- Alembic is **never** invoked automatically (D32).

**STORAGE_CONVERGING**

- process remains alive; `/health/live` remains healthy; `/health/ready` remains
  `NOT_READY`;
- entered only under `STORAGE_BACKEND=s3`, only after BOOTSTRAP/MAINTENANCE's schema
  check (D32) confirms the schema is current;
- S3 probe (D21), migration-owned-set convergence (D8), and confirmed-duplicate
  cleanup (D9/D35) run here;
- normal (non-recovery-owned) business work remains blocked, identically to
  BOOTSTRAP/MAINTENANCE;
- **exactly one exception**: narrowly-defined **recovery-owned destructive work**
  (defined below) may progress, through the existing pipeline engine, if required to
  reach convergence;
- progress is observable through logs/metrics (D19's logging requirements apply
  identically here) — never gated behind a fixed maximum duration (D33).

**OPERATIONAL**

- schema is valid/current (D32);
- storage convergence is complete, precisely defined as:
  **(amendment, contract-completeness round — corrects an internal inconsistency:
  the bullet below previously read "no unresolved recovery-owned destructive state
  remains," which the "Behavioral requirement" subsection further down already
  contradicted by allowing a `failed`/`cancelled` terminal run to unblock
  OPERATIONAL. This bullet is now the single definition; the "Behavioral
  requirement" subsection restates it, it does not compete with it.)**
  1. the migration-owned set (D8) is fully repointed, **and**
  2. **every recovery-owned `PipelineRun` lineage identified at the start of this
     convergence pass (D34's proven-lineage classifier) has reached a legitimate
     durable terminal state per ADR-0009's own transition matrix** —
     `succeeded`, `failed`, or `cancelled`. This is a condition on the **lineage
     reaching a terminal state**, not on the **Source reaching `DELETED`**: a
     lineage that terminates `failed`/`cancelled` satisfies this condition and does
     not block OPERATIONAL, even though its Source(s) may remain `DELETING` pending
     an operator's SM-514 manual retry (ADR-0010 D18/D19) — exactly as the
     "Behavioral requirement" subsection below already specifies;
- required dependencies (PostgreSQL, Neo4j, S3 where configured) are ready;
- normal worker claims allowed for every pipeline type;
- normal business API fully enabled;
- `/health/ready` may become ready (D20).

**Recovery-owned destructive work, defined.**

**Amendment (sixth amendment, D43 recovery-owned claim consistency fix) — the
original wording below made a `PipelineRun` recovery-owned merely because its scope
*contained* a Case-B Source. That is corrected here: Case-B **Source**
classification and recovery-owned **run**-claim eligibility are related but
distinct, and the older wording did not make the distinction explicit enough for an
implementer to avoid conflating them. See the worked DATASET_DELETE counterexample
below.**

**Case-B Source classification != recovery-owned run-claim eligibility.** D34's Case
B answers a narrower question — "is this one `DELETING` Source with a missing local
object explained by some durable destructive lineage, so the startup scan must not
treat it as ordinary migration/integrity failure?" It says nothing, by itself, about
whether any *particular* `PipelineRun` may be claimed and resumed by the worker
during STORAGE_CONVERGING. That second question — the one STORAGE-007's claim filter
must actually answer — requires the stronger predicate below.

A `PipelineRun` is recovery-owned (claim-eligible during STORAGE_CONVERGING) **iff
all** of the following hold:

- its `pipeline_type` is `forget` or `dataset_delete` (ADR-0009, ADR-0010), **and**
- it is non-terminal (`queued`, `running`, or `cancelling`) or freshly eligible for
  manual retry, **and**
- **PostgreSQL durably proves that this run's own authoritative destructive
  mutation has already completed for its complete target scope** — concretely, the
  existing accepted durable step-completion predicate already used elsewhere in
  this codebase for the identical "don't infer ownership from status alone" problem
  (ADR-0010 D28's `exists_administrative_delete_ownership`, mirrored by D34 Case B's
  own lineage proof): this run's own `authoritative_mutation` step (Forget) or
  `deactivate_authoritative` step (`DATASET_DELETE`) has a persisted `PipelineStep`
  row with `status = succeeded`. Because that step's `persist()` commits the
  authoritative mutation for the run's **entire** target scope in one transaction
  (Forget: the one targeted Source; `DATASET_DELETE`: every Source in the targeted
  Dataset that was not already `DELETED`), a `succeeded` status on that one step row
  is proof the mutation is durably complete for the run's whole scope — not merely
  for whichever Source happened to prompt the classification.

**Case-B membership alone is never sufficient.** The correct composition is:

```text
Case-B Source classification (D34)
    +
a compatible existing destructive PipelineRun (same lineage predicate D34 already
uses)
    +
that run's OWN authoritative_mutation/deactivate_authoritative step durably
SUCCEEDED for its complete target scope
    =>
recovery-owned run-claim eligibility (this section)
```

A `DELETING` Source with a missing local object and **no** provable compatible
lineage is never classified recovery-owned at all (D34 Case D) — it is an integrity
failure, not a claim-eligible run. Symmetrically, a `PipelineRun` whose own
authoritative mutation step has **not** yet durably succeeded is never claim-eligible
during STORAGE_CONVERGING, even if its scope happens to already contain a Case-B
Source made `DELETING` by some *other*, already-completed lineage — resuming such a
not-yet-mutated run could still execute a `DELETING`-causing mutation against a
Case-A Source elsewhere in its own scope, which D43 forbids. See D43's own
cross-reference below and the worked counterexample immediately following.

**DATASET_DELETE multi-source safety — worked counterexample (sixth amendment).**

```text
Dataset D contains:
    Source A = DELETING + file:// + missing local object   (D34 Case B)
    Source B = ACTIVE   + file://                            (D34 Case A)

Existing PipelineRun R = DATASET_DELETE(D), non-terminal.

Case 1 -- R's own deactivate_authoritative step has NOT yet durably succeeded:
    => R is NOT claim-eligible as recovery-owned, despite D's scope containing
       Case-B Source A;
    => Source B remains classified Case A;
    => no ACTIVE -> DELETING transition may occur for B during
       STORAGE_CONVERGING;
    => any normal claim of R (or of a fresh DATASET_DELETE targeting D) remains
       blocked until OPERATIONAL, exactly as D31's "no new normal claim" rule
       already requires.
    (Source A's own Case-B classification in this scenario must in fact be
    explained by some OTHER, already-completed compatible lineage -- not R --
    since Source.status=DELETING can only ever be set by an already-committed
    authoritative-mutation transaction, D14; if no such other lineage exists,
    A is Case D, not Case B.)

Case 2 -- R's own deactivate_authoritative step HAS durably succeeded for D's
complete scope:
    => by construction, that single atomic transaction already transitioned
       every non-DELETED Source in D (including B) to DELETING in the same
       commit that produced Source A's DELETING status;
    => Source B can therefore never still be observed ACTIVE+file:// under
       this case -- it is already DELETING, and D43's exclusion (Case-A and
       destructive-transition ownership are disjoint during
       STORAGE_CONVERGING) is satisfied by construction, not by policy;
    => R IS claim-eligible as recovery-owned, and resuming it through its
       existing remaining steps (storage_deletion/delete_storage,
       finalize_target/finalize_tombstone, finalize_result) is exactly the
       narrowly-scoped work D31 already allows.
```

This is the general proof, not merely an example: because `deactivate_authoritative`/
`authoritative_mutation` transitions its run's *entire* target scope to `DELETING` in
one atomic transaction (D14), there is no durable state in which R's own step has
succeeded yet any Source in R's scope remains live (Case A). The D31 predicate above
(step durably `succeeded` for the run) is therefore sufficient, by itself, to
guarantee no recovery-owned claim can ever coexist with a live Case-A Source still
inside that same run's scope — closing exactly the gap this amendment was written to
close.

**Behavioral requirement (frozen, exact implementation left to backlog):**

- no new **normal** `PipelineRun` claim of any pipeline type may begin during
  STORAGE_CONVERGING;
  **(amendment, D43, STORAGE-006 CAS-loss safety audit) — stated explicitly because
  the audit found a real gap this bullet already implies but did not spell out: this
  prohibition is precisely what makes it structurally impossible for a
  migration-eligible (D34 Case A) Source to begin a *new* destructive transition
  during STORAGE_CONVERGING. Recovery-owned runs remain claimable only because their
  authoritative destructive mutation is already durable and their Source(s) are
  already `DELETING` before this convergence pass even starts (D31's own
  "Recovery-owned destructive work, defined" above) — this bullet never authorizes a
  *new* authoritative destructive mutation against a Case-A Source. See D43.**
- an already-existing recovery-owned run **may** be claimed and progressed by the
  same, unmodified ADR-0009 engine (`FOR UPDATE SKIP LOCKED`, advisory-lock
  arbitration, heartbeat, retry, stale recovery — all unchanged);
- no second queue, no second engine, and no direct execution of destructive business
  finalization (`_finalize_dataset_target`, `finalize_tombstone`, or equivalent)
  inside the storage-convergence service itself — recovery-owned work always
  finishes through the same steps (`StorageDeletionStep`/`DeleteStorageStep`,
  `finalize_target`/`finalize_tombstone`) any other Forget/`DATASET_DELETE` run would
  use;
- operational readiness (OPERATIONAL state, above) is withheld until every
  recovery-owned run identified at the start of STORAGE_CONVERGING has reached its
  own legitimate durable terminal state (`succeeded`, `failed`, or `cancelled` per
  ADR-0009's own transition matrix) — a run stuck `failed` awaiting manual retry
  (D18/D19 of ADR-0010) is itself a legitimate terminal state for this purpose; it
  does not block OPERATIONAL forever, but the Source(s) it targeted remain
  `DELETING`/`file://` and continue to be excluded from D8's migration-owned set on
  every subsequent boot (D8's Scope note) until an operator resolves them via SM-514
  manual retry, exactly as ADR-0010 already requires today with no S3 involved at
  all.
  **(deletion-semantics amendment, D37)** in practice this terminal state is reached
  far more readily than the pre-D37 text implied: `StorageDeletionStep`/
  `DeleteStorageStep` no longer block `finalize_target`/`finalize_tombstone` merely
  because physical cleanup could not be proven — a recognized-inability outcome now
  completes as `UNRESOLVED` (D37) and lets the run proceed to `succeeded` with
  unresolved-storage metrics (D39), rather than looping on retry or sitting `failed`
  awaiting an operator. A recovery-owned run only fails to converge here for the
  narrower set of reasons D37 still treats as genuine `PipelineStep` failures
  (unrecognized/programming defects) or an actual crash mid-attempt still being
  reconciled per ADR-0009 §I's `AMBIGUOUS` classification.
- the exact worker/claim-filtering query mechanism (a `pipeline_type IN (...)` claim
  filter active only during STORAGE_CONVERGING, or an equivalent) is left to backlog
  implementation; the behavioral requirement above — normal claims blocked,
  recovery-owned claims allowed, no second engine — is not.

This section is the answer to B2.1's "chicken-and-egg" framing: storage convergence
cannot unconditionally require *all* `DELETING`-with-missing-file Sources to be
"resolved" before OPERATIONAL, because resolving them **is** normal Forget/
`DATASET_DELETE` pipeline work that itself needs the pipeline engine running — which
is precisely why that specific, narrowly-scoped work is allowed to run *during*
STORAGE_CONVERGING rather than waiting for OPERATIONAL to unlock it.

### D32. Alembic and first install — explicit interaction with storage convergence

**New section (review finding M1.1) — makes explicit what D7 already implied but did
not spell out as its own gate.**

If the PostgreSQL schema is absent, behind the application's expected head, or
otherwise not confirmed safe for this application version, the application **must
not**:

- automatically run Alembic;
- inspect `Source` rows using an assumed schema shape;
- attempt filesystem→S3 convergence (D7/D8) against an unknown/unverified schema;
- start normal worker/business processing.

Instead the process remains in **BOOTSTRAP/MAINTENANCE** (D31): `/health/live`
available, `/health/ready = NOT_READY`, exactly as `docs/operations.md`'s existing
"migration is explicit, never automatic" first-start procedure already documents for
today's filesystem-only installs — this ADR adds no new automatic-migration
behavior, it only names the pre-existing state explicitly and makes storage
convergence (D7) strictly ordered *after* it, never concurrent with it and never a
substitute for it. The operator runs the already-documented explicit
`alembic upgrade head` when appropriate; only after schema convergence does normal
application/storage bootstrap (BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING or directly
→ OPERATIONAL for `filesystem`) proceed, per the supported deployment lifecycle
already described in `docs/operations.md` section 2.

### D33. Long storage migration / orchestrator safety

**New section (review finding M1.2).**

Storage convergence (STORAGE_CONVERGING) may take minutes or hours on a large
installation's first `STORAGE_BACKEND=s3` boot. A healthy, progressing storage
migration must not be mistaken for a dead/hung container merely because the
application has not reached OPERATIONAL within some short, arbitrary window.

Required observable contract, frozen here (exact FastAPI/ASGI orchestration
mechanics — e.g. whether the maintenance surface is served by the same process
before `lifespan` completes, a minimal pre-`yield` app, or an equivalent — are left to
implementation, per the task's own allowance):

- a maintenance HTTP surface is available throughout BOOTSTRAP/MAINTENANCE and
  STORAGE_CONVERGING — this is a requirement on **observable behavior**, not merely
  "insert the convergence call somewhere before `worker.start()`," which the original
  D7 wording could be misread as claiming was sufficient on its own for a blocking
  FastAPI `lifespan` (a blocking pre-`yield` `lifespan` body does not, by itself,
  guarantee the ASGI server is already routing requests to `/health/live` — the
  implementation must ensure the maintenance surface is actually reachable during
  this phase, by whatever concrete mechanism satisfies that);
- `/health/live` reports process/bootstrap liveness continuously through all three
  D31 states;
- `/health/ready` reports `NOT_READY` throughout BOOTSTRAP/MAINTENANCE and
  STORAGE_CONVERGING, becoming ready only in OPERATIONAL;
- this ADR does **not** freeze one arbitrary maximum migration duration, and does
  **not** rely solely on increasing a Docker `start_period` (or equivalent
  healthcheck grace period) as the mechanism that makes long convergence safe —
  `start_period` tuning, if used at all, is a deployment-level mitigation layered on
  top of the observable-liveness contract above, never a substitute for it;
- convergence progress (Sources classified, migrated, verified, cleaned; recovery-
  owned runs pending/resolved) must be observable through logs/metrics (D19) so an
  operator can distinguish "still working" from "stuck," without depending on guessing
  a duration.

### D34. Destructive-pipeline missing-file classification (Case A / Case B / Case D; Case C is `DELETED` tombstones, added later by D40)

**New section (review finding B2) — the classification D7's Scope note, D8's Scope
note, and D19's corrected wording all reference.**

**Amendment (contract-completeness round) — Case B requires *proven* lineage, not
`Source.status` alone.** The heading already said "durable destructive pipeline
lineage owns the deletion," but the classifier below did not make provable lineage a
condition — it read as though `status = DELETING` plus a missing object were
sufficient by themselves. That is corrected here: `Source.status = DELETING` records
*intent*, not *proof of an active, compatible destructive `PipelineRun`*. Never infer
destructive ownership from `status = DELETING` alone.

The original ADR text treated every `Source.storage_uri = file://...` row with a
missing local object as migration/integrity failure unconditionally. That is
incorrect: Forget's `StorageDeletionStep` and `DATASET_DELETE`'s `DeleteStorageStep`
are both existing, accepted `AMBIGUOUS`-classified external effects (ADR-0009 §I,
ADR-0010 D9) whose crash-safe contract already treats "object already absent" as
legitimate, reconcilable, idempotent progress — a real, accepted crash window
produces exactly this durable state:

```text
1. Source.status = DELETING;
2. StorageDeletionStep (or DeleteStorageStep) deletes the local object;
3. process crashes BEFORE finalize_target (Forget) / finalize_tombstone
   (DATASET_DELETE);
4. PostgreSQL still contains the old file:// storage_uri (never rewritten by
   these steps -- storage_uri is only ever cleared/repointed by their owning
   pipeline's finalize step, D14).
```

Frozen classification, evaluated by the startup scan (D7) before D8 ever runs:

**Case A — ordinary live, migration-eligible Source.** **(renamed, editorial
consistency pass — "non-`DELETING`" literally includes `DELETED`, which is Case C
below, not Case A; Case A is precisely D40's live-status list.)**

```text
status IN (PENDING, PROCESSING, ACTIVE, FAILED) + file:// storage_uri
    + required local object missing
    => integrity failure (D8 step D, D19)
    => fail closed
    => do not migrate
    => do not rewrite storage_uri
```

**Clarification (amendment, D43, STORAGE-006 CAS-loss safety audit) — a Case A
Source observed transitioning to `DELETING` mid-migration is now structurally
excluded, not merely reconciled.** D43 makes it an internal lifecycle invariant
violation for a Source this convergence pass already snapshotted as Case A
(migration-eligible) to be observed as `DELETING` at CAS time (D8 step H) — never a
benign "another legitimate pipeline now owns it" outcome to shrug off. Under D43,
during STORAGE_CONVERGING no migration-eligible Source can begin a *new* destructive
transition at all, so this specific CAS-loss shape should never arise from a
correctly-supported single-replica MVP deployment (D43); if it is nonetheless
observed, it fails closed as a genuine invariant violation rather than being
classified as ordinary CAS contention. This does not change Case B's own
requirements (a `DELETING` Source with **proven** compatible lineage remains Case
B, unconditionally) or Case D's fail-closed default — it only forecloses the
specific race a live Case-A migration attempt could previously have raced against.

**Case B — `Source.status = DELETING` AND a compatible durable destructive
`PipelineRun` lineage can be proven to own that Source/Dataset's destructive
transition.**

Case B requires **both** conditions to hold, not `status = DELETING` alone:

1. `Source.status == DELETING` **and** the expected local object is absent, **and**
2. a compatible durable `forget` or `dataset_delete` `PipelineRun` lineage
   (non-terminal, or terminal-but-not-yet-manually-retried per D18/D19 of ADR-0010)
   targeting this exact Source/Dataset can be **proven** to exist — i.e., a
   `PipelineRun` row is found whose scope includes this Source and whose
   `pipeline_type`/target make it the compatible owner of the `DELETING` transition
   (the same lineage `administratively_deleting(D)`-style provable-ownership
   discipline ADR-0010 D28 already uses for its own analogous "don't infer ownership
   from status alone" problem — this section applies the identical principle to
   startup convergence's classifier).

```text
file:// storage_uri + status == DELETING + local object absent
    + PROVEN compatible destructive PipelineRun lineage exists (condition 2)
    => Case B: MAY represent an already-completed AMBIGUOUS destructive effect
    => MUST NOT be uploaded to S3 (D8's Scope note excludes it outright)
    => MUST NOT be treated as ordinary migration corruption / D19 integrity failure
    => startup convergence MUST NOT clear or rewrite storage_uri itself
    => startup convergence MUST NOT perform Forget/DATASET_DELETE finalization
       (that remains exclusively the owning PipelineRun's finalize_target /
       finalize_tombstone step, run through the unmodified engine, D31)
    => final reconciliation is owned by the existing durable destructive pipeline
       lineage (D31's recovery-owned destructive work)
```

The storage-convergence subsystem never becomes a second Forget/`DATASET_DELETE`
engine (D31). It only (a) refrains from touching Case B Sources at all beyond
classifying them, and (b) lets the existing engine's already-defined claim/retry path
finish the business transition it already owns.

**Relationship to D31's run-claim predicate (amendment, sixth amendment, D43
recovery-owned claim consistency fix) — Case B is a Source-level classification, not
a run-claim decision.** This section only answers "is this `DELETING` Source's
missing local object explained by durable lineage" — it deliberately says nothing
about whether the `PipelineRun` that lineage points to may itself be *claimed and
resumed* during STORAGE_CONVERGING. That is a strictly stronger question, answered
exclusively by D31's "Recovery-owned destructive work, defined": the run's own
`authoritative_mutation`/`deactivate_authoritative` step must have durably
`succeeded` for the run's **complete** target scope, not merely for the one Source
that happened to be classified Case B. A Source may be correctly classified Case B
(satisfying this section) while the `PipelineRun` its lineage points to is still
*not* claim-eligible under D31, if that run's own authoritative mutation has not yet
durably completed — see D31's worked `DATASET_DELETE` multi-source counterexample.
This is one formulation, applied consistently: Case B classification never itself
implies run-claim eligibility; D31 alone decides that.

A `DELETING` Source whose local object is still **present** is not Case B at all —
that is an ordinary in-flight or not-yet-executed deletion, unaffected by this
section; D8's Scope note excludes every `DELETING` Source from the migration-owned
set regardless of local-object presence, precisely so this distinction never has to
be made twice.

**Case D — `Source.status = DELETING`, local object absent, but NO compatible durable
destructive lineage can be proven.**

```text
file:// storage_uri + status == DELETING + local object absent
    + NO provable compatible Forget/DATASET_DELETE PipelineRun lineage (condition 2
      fails)
    => internal consistency / integrity failure
    => NOT migration-owned (never upload to S3)
    => NOT recovery-owned (D31 never claims/progresses anything for this Source --
       there is nothing durable to progress)
    => do not rewrite storage_uri
    => fail closed / block STORAGE_CONVERGING -> OPERATIONAL (D19, D31's terminal-
       state condition cannot be satisfied for a Source with no lineage to reach a
       terminal state in the first place)
    => surfaced for operator diagnosis (D19's logging requirements)
```

Case D is deliberately **not** folded into Case A's "ordinary integrity failure"
bucket, because a `DELETING`-status Source is not "ordinary" (Case A is defined only
over the live, migration-eligible statuses — `PENDING`/`PROCESSING`/`ACTIVE`/
`FAILED`) — it gets its own classification so diagnostics and future
tooling can distinguish "a live Source lost its file" (Case A) from "a Source
mid-deletion has no traceable owner for that deletion" (Case D), which point to
different operational root causes (the former: storage/filesystem problem; the
latter: a `pipeline_runs` bookkeeping or historical-data problem — e.g. `DELETING`
set by code outside the documented Forget/`DATASET_DELETE` paths, or `PipelineRun`
history that was manually altered/lost, both of which this ADR's design otherwise
assumes cannot happen and therefore treats as a genuine defect requiring operator
attention, not an automatic classification this ADR can safely resolve on its own).

### D35. Post-CAS legacy locator contract

**New section (review finding M2) — required because after D8 step H's CAS commits,
`Source.storage_uri` no longer carries the `file://` value D9's cleanup pass would
otherwise have parsed.**

Current canonical legacy filesystem layout (unchanged, `final_storage_path`):

```text
DATA_DIRECTORY/
  <dataset_id>/
    <source_id>/
      original<canonical-storage-extension>
```

The legacy final path for a Source whose `storage_uri` has already become `s3://...`
**must** be derived only from durable Source identity plus the centralized,
versioned canonical extension mapping (D6's amendment) — never rediscovered by
inspecting the filesystem.

Required durable inputs, all already persisted on the `Source` row today:

- `Source.dataset_id`;
- `Source.id`;
- `Source.mime_type`;
- the centralized `mime_type → canonical storage extension` mapping (D6's amendment
  — implementation must add this as its own named mapping; it is derivable from,
  but not identical to, today's `STORAGE_EXTENSION_BY_SOURCE_EXTENSION` /
  `MIME_TYPE_BY_SOURCE_EXTENSION` pair in `loaders/text.py`).

**Explicitly forbidden mechanisms:**

- `glob("original.*")` or any other wildcard/pattern match;
- recursive directory search;
- arbitrary "first file found" selection;
- use of a client-supplied filename;
- directory enumeration as destructive authority of any kind.

**If a canonical legacy extension/path cannot be derived unambiguously** (an unknown
or unmappable `mime_type`):

- do not guess;
- do not delete anything;
- leave the local object untouched;
- surface a convergence/integrity error (D19);
- readiness remains `NOT_READY` until the operator explicitly resolves it.

**Required pre-deletion sequence** (D9's cleanup pass, restated precisely):

1. confirm `Source.storage_uri` in PostgreSQL points to the expected deterministic
   `s3://` object for this Source's `dataset_id`/`id` (D6);
2. confirm that S3 object is valid (the same cheap check as D8 step F/D11, re-run —
   not re-trusted blindly from a prior pass);
3. derive the exact legacy local predecessor path via this section's contract;
4. confirm that exact path exists;
5. hash the local predecessor's content;
6. require the hash to equal `Source.content_sha256`;
7. only then `unlink()` that exact file — never any other path.

Empty `<dataset_id>/<source_id>/` directories may be removed only when actually
empty after that unlink (unchanged from D24). Unknown files anywhere under
`DATA_DIRECTORY` remain untouched by construction — nothing in this contract ever
enumerates a directory to decide what to delete.

### D36. Managed S3 namespace ownership — required invariant

**New section (review-required invariant, not previously stated).**

The Sofias Memory Source-object namespace is application-managed:

```text
s3://<bucket>/<STORAGE_S3_PREFIX>/v1/sources/...
```

**Sofias Memory must be the exclusive writer of Source objects inside this managed
namespace.** External actors/applications must not:

- create Source keys inside it;
- overwrite existing Source objects inside it;
- create new versions of Source objects inside it;
- delete Source objects inside it;
- mutate the Sofias-Memory-owned integrity metadata (expected SHA-256/size, D11)
  attached to objects inside it.

Read-only backup/replication/audit tooling is permitted, provided it never mutates
the managed namespace.

**Rationale.** This entire ADR's migration/finalize/delete contract depends on:

- deterministic key identity (D6);
- idempotent existing-object checks (D8 step F, D10, D11's cheap tier);
- strong verification before any durable repoint (D8 step G, D11's strong tier);
- conflict detection treating an unexpected existing object as a hard failure, never
  a silent overwrite (D8 step F);
- Forget/`DATASET_DELETE` proving that **all** retained versions for an exact key are
  gone (D15).

None of these guarantees can remain strong if an independent external writer may
race Sofias Memory by recreating, versioning, or deleting objects inside the same
managed namespace out of band — a concurrent external write could, for example, make
D8 step F's "already copied, matching hash" check pass against bytes Sofias Memory
never wrote, or resurrect a version D15 had already proven fully deleted.

**Operational/IAM consequence** (documentation obligation for the eventual
implementation, not a runtime check this ADR can itself enforce): credentials/bucket
policies must restrict write access to the managed namespace to the Sofias Memory
application's own credentials and any explicitly authorized maintenance identity
(e.g. an operator running a future diagnostic CLI, D30). This is a
**prefix-scoped**, not bucket-wide, exclusivity requirement — this ADR does not claim
or require that the entire configured bucket be exclusively owned by Sofias Memory,
only the managed `STORAGE_S3_PREFIX` namespace within it; a bucket may be shared with
other prefixes/applications provided the managed namespace itself is write-exclusive.
A detected violation (D19's new bullet) is treated as a configuration/integrity
problem, surfaced and fail-closed, never silently trusted or auto-repaired.

### D37. Storage deletion result contract and business-delete convergence

**New section (deletion-semantics amendment) — the core decision of this round.**

**Core decision.** PostgreSQL is authoritative for Sofias Memory's derived/structured
memory (ADR-0002, unchanged). A finalized Source original stored through `file://` or
`s3://` is a durable **Source provenance / reprocessing artifact** — important and
normally retained while the Source is live, but it is **not** the authority for
already-derived Dataset memory. A full Source Forget, full Dataset Forget, Forget
Everything, or administrative Dataset DELETE must therefore **not** be permanently
blocked solely because the original storage artifact is already absent or cannot
currently be accessed/deleted. This supersedes D15's and ADR-0010's earlier assumption
that physical Source-object deletion must always be proven before the business
deletion `PipelineRun` can succeed (D28's amendment). PostgreSQL/Neo4j
authoritative-memory deletion semantics remain unchanged.

**`StorageDeleteResult` contract.** Four semantic outcomes, frozen (exact
enum/class names are implementation detail):

| Outcome | Meaning |
|---|---|
| `NOT_REQUESTED` | No storage deletion was requested/applicable (e.g. `memory_only=true`). |
| `DELETED_NOW` | The adapter successfully removed the exact Source object and proved the required post-delete absence semantics (D15/D38's positive-evidence requirement). |
| `ALREADY_ABSENT` | The adapter could access/inspect the relevant backend and **positively established** that the exact Source object was already absent. |
| `UNRESOLVED` | The adapter could not safely prove physical deletion or absence. |

`UNRESOLVED` covers recognized/expected storage conditions including: backend
configuration unavailable; credentials unavailable/invalid; bucket/endpoint
unavailable; `AccessDenied`; network timeout; provider temporarily unavailable;
Object Lock/retention/legal hold; version deletion could not be completed; post-delete
verification could not prove absence; storage URI cannot be safely resolved/validated
for deletion.

**Invariant: "cannot access the backend" must never become "object already
absent."** `ALREADY_ABSENT` requires positive evidence of absence (a successful,
completed inspection that found nothing) — never inferred from an inability to check.
This is the one discipline this amendment does not relax; it is the same positive-
evidence requirement D15/D38 already state for `DELETED_NOW`, extended symmetrically
to `ALREADY_ABSENT`.

**Business delete must converge.** For full destructive operations:

```text
authoritative PostgreSQL mutation
    -> graph projection convergence
    -> Source-original cleanup attempt
    -> PostgreSQL business finalization
```

`StorageDeleteResult = UNRESOLVED` is **not, by itself, a `PipelineRun` failure**. The
storage-deletion step (`StorageDeletionStep`/`DeleteStorageStep`) returns the
unresolved outcome as durable step output and allows the existing PostgreSQL
finalizer (`finalize_target`/`finalize_tombstone`) to execute. The business deletion
may therefore succeed while original-storage cleanup remains unresolved. This applies
to: full Source Forget; full Dataset Forget; Forget Everything; administrative
`DATASET_DELETE`. `memory_only=true` Forget is **unchanged** — it never requests
Source-original deletion at all, so this contract does not apply to it (`NOT_REQUESTED`
is its only possible outcome).

**Explicit per-Source storage result coverage (amendment, contract-completeness
round).** For every Source that a full destructive operation is about to transition
`DELETING → DELETED`, the storage-deletion step **must have produced exactly one
explicit `StorageDeleteResult` for that Source**. This applies to full Source Forget,
full Dataset Forget, Forget Everything, and administrative `DATASET_DELETE` — the
same four operations `UNRESOLVED` applies to.

**The PostgreSQL finalizer must not interpret the absence of a per-Source storage
result as implicit `UNRESOLVED`.** A missing result is an **internal invariant
failure**, not a fifth `StorageDeleteResult` value:

```text
source is being finalized as DELETED
+ no explicit storage result exists for that source_id
    => finalizer fails (a genuine PipelineStep/finalize failure, per D37's
       expected-failure-vs-defect distinction below)
    => the Source is NOT silently finalized based on missing evidence
    => do not add a fifth StorageDeleteResult value for "missing" -- the absence
       itself is the invalid internal state, not a storage outcome to encode
```

This preserves D37's core distinction: a **recognized operational storage
condition** produces an explicit `UNRESOLVED`; a **programming/bookkeeping/invariant
defect** — including "the storage step silently never ran for this Source" — remains
a real failure. Letting a missing result masquerade as `UNRESOLVED` would erase that
distinction by making bookkeeping bugs indistinguishable from legitimate operational
inability.

**`NOT_REQUESTED`, clarified.** Two distinct legitimate sources of `NOT_REQUESTED`:

- `memory_only=true` operations do not transition their Source to `DELETED` at all
  and use `NOT_REQUESTED` exactly as already defined above (out of scope for the
  per-Source coverage requirement, since no `DELETING → DELETED` transition occurs);
- within a full destructive operation, if the storage-deletion step encounters a
  Source whose `storage_uri` was **already `NULL`** before the step ran, the
  implementation may explicitly produce `NOT_REQUESTED` for that Source — there is no
  known external locator to delete. This **still requires** the explicit per-Source
  result: the step must record `NOT_REQUESTED` for that `source_id`, not omit the
  Source from its output bookkeeping entirely.

Storage step output coverage and finalizer validation are therefore **source-for-
source complete**: every Source in a full destructive operation's scope has exactly
one recorded `StorageDeleteResult` (one of the four outcomes, `NOT_REQUESTED`
included) before that Source may be finalized `DELETED`.

**Expected storage failure vs. software defect.** This contract must **not** be
implemented as a blanket `except Exception: return UNRESOLVED`. Only typed/recognized
storage outcomes (the list above) may become `UNRESOLVED`. Unexpected programming
errors, violated internal invariants, assertion failures, or unclassified adapter
defects remain genuine `PipelineStep` failures, governed by ADR-0009's existing
retryable/permanent error classification (§K, §X) exactly as any other step failure
today. The storage adapter must distinguish operational inability (a condition the
adapter itself recognizes and can name) from software failure (everything else) — the
former is a successful, typed step outcome; the latter is not.

**Relationship to `AMBIGUOUS` `CancellationRecoveryMode` (do not conflate the two
axes).** `AMBIGUOUS` classification (ADR-0009 §I, ADR-0010 D9) governs recovering a
step that was **interrupted mid-attempt** — a crash before the step itself reached any
terminal outcome, where stale-run recovery must reconcile the `PipelineStep`'s own
status. `UNRESOLVED` governs a step that **completed normally** and determined, with
recognized evidence, that it cannot currently prove physical deletion. These are
orthogonal: a crashed attempt is still reconciled per ADR-0009 §I/ADR-0010 D9's
existing case A/B/C classification (unchanged by this amendment); once reconciled (or
on a fresh, uninterrupted attempt), the step's own completed result is one of the four
`StorageDeleteResult` outcomes above.

**Recovery/retry.** A retry after an `AMBIGUOUS`-classified interrupted storage
operation re-observes the exact target:

- if it can prove absent → `ALREADY_ABSENT`;
- if it deletes and proves absence → `DELETED_NOW`;
- if it still cannot determine the physical state → `UNRESOLVED`.

Never infer external deletion solely from PostgreSQL state (e.g., `Source.status =
DELETED` is never itself taken as proof the S3/filesystem object is gone — D39's
tombstone semantics exist precisely because that inference is not safe).

### D38. S3 versioning and filesystem semantics under the `UNRESOLVED` contract

**New section (deletion-semantics amendment) — restates D15's versioning rule and
D24's filesystem rule against D37's four-outcome contract, without weakening either's
positive-evidence requirement.**

**S3 versioning.** The strong definition of `DELETED_NOW` from D15 is preserved
exactly: for a versioned bucket, `DELETED_NOW` may be returned only after all
retained versions/delete markers for the exact Source key have been removed **and**
absence has been verified. If that cannot be proven because of permissions, Object
Lock, retention, provider limitations, timeout, or verification failure, the adapter
returns `UNRESOLVED` — never `DELETED_NOW`, never `ALREADY_ABSENT`. The authoritative
Sofias Memory deletion may still finalize (D37); the unresolved `s3://` `storage_uri`
remains on the `DELETED` Source tombstone (D39).

**Filesystem.** The same contract, restated for the filesystem adapter:

- the exact deterministic path exists and `unlink()` succeeds → `DELETED_NOW`;
- the exact safe path does not exist (positively confirmed via the existing
  containment-checked resolution, `source_storage_path`) → `ALREADY_ABSENT`;
- the path/storage dependency cannot be safely resolved (e.g. the URI fails
  containment validation, or the underlying filesystem itself is unavailable) →
  `UNRESOLVED`, where the condition is a recognized storage condition per D37 — never
  a guessed or partially-validated path.

Never delete a guessed path, under either backend — this is the same discipline D35's
"forbidden mechanisms" list already establishes for post-CAS cleanup, extended here to
ordinary Forget/`DATASET_DELETE` storage deletion.

### D39. Tombstone / `storage_uri` retention semantics

**New section (deletion-semantics amendment).**

**Amendment (editorial consistency pass) — corrects `DELETED`+`NULL`'s meaning,
which was stated too strongly before D37's `NOT_REQUESTED`-for-already-`NULL`-
`storage_uri` case was frozen.** `DELETED`+`storage_uri = NULL` does **not**
necessarily mean physical deletion or absence was positively proven through
`DELETED_NOW`/`ALREADY_ABSENT` — it can equally mean the Source's `storage_uri` was
already `NULL` before storage deletion ran, so `NOT_REQUESTED` was the correct,
explicit per-Source outcome (D37) because there was no known external locator to
clean up in the first place. The corrected, frozen meaning:

> `DELETED` + `storage_uri = NULL` ⇒ Sofias Memory memory is deleted **and there is
> no known outstanding external Source-artifact cleanup obligation.**

This state may result from any of three distinct, per-Source `StorageDeleteResult`
outcomes:

- `DELETED_NOW` — physical deletion positively confirmed;
- `ALREADY_ABSENT` — physical absence positively confirmed;
- `NOT_REQUESTED` (full destructive operation, `storage_uri` already `NULL`) — no
  known external locator existed to clean up, so there was nothing to confirm.

`DELETED` + `storage_uri != NULL` is **unchanged** and continues to mean exactly one
thing: physical cleanup was not confirmed (`UNRESOLVED`); the retained locator
represents cleanup debt.

Final state table:

| `Source.status` | `Source.storage_uri` | Meaning | Possible `StorageDeleteResult`(s) |
|---|---|---|---|
| `DELETED` | `NULL` | Memory deleted; **no known outstanding external cleanup obligation.** Does **not** by itself prove physical deletion/absence was confirmed — it may instead mean there was never a known locator to clean up. | `DELETED_NOW`, `ALREADY_ABSENT`, or `NOT_REQUESTED` |
| `DELETED` | `!= NULL` (`file://...` or `s3://...`) | Memory deleted; physical cleanup of the original artifact was **not** confirmed; retained locator is cleanup debt. | `UNRESOLVED` |

Do not weaken the positive-evidence meanings of `DELETED_NOW`/`ALREADY_ABSENT`
themselves (D37, D38 unchanged) — this correction only narrows what the *tombstone
state* `DELETED`+`NULL` can be inferred to prove, since `NOT_REQUESTED` now also
reaches that state without either of those two proofs. Never interpret
`NOT_REQUESTED` itself as proof of physical absence — it asserts only "no known
locator existed," not "a locator existed and was found absent" (that distinction is
`ALREADY_ABSENT`'s job).

Finalizer behavior (unchanged by this correction):

- `DELETED_NOW`, `ALREADY_ABSENT`, or `NOT_REQUESTED` → the finalizer clears
  `Source.storage_uri` to `NULL` (for `NOT_REQUESTED` this is a no-op, since
  `storage_uri` was already `NULL`);
- `UNRESOLVED` → the finalizer **must preserve** `Source.storage_uri` unchanged.

**No new PostgreSQL column is added to represent cleanup debt in this increment.**
The existing combination `status = DELETED` + `storage_uri != NULL` is itself the
durable representation of unresolved Source-original cleanup — no migration is
required for this amendment (consistent with D26's principle: do not add a column
without an otherwise-unsatisfiable invariant). A future maintenance feature may use
that retained locator for explicit, operator-driven cleanup, but such a feature is
**out of scope** for ADR-0011.

**Pipeline result metrics.** Later implementation must expose safe run metrics
conceptually equivalent to `storage_deleted`, `storage_already_absent`,
`storage_unresolved`, and optionally a boolean summary such as
`storage_cleanup_complete` (exact public/internal naming is implementation planning,
consistent with ADR-0010 D24's existing metrics-naming latitude). `storage_deleted`
and `storage_already_absent` count only `DELETED_NOW`/`ALREADY_ABSENT` outcomes
respectively; a `NOT_REQUESTED` outcome increments neither (it was never a cleanup
attempt at all, successful or otherwise) and is tracked, if at all, by a separate
counter distinct from all three of `storage_deleted`/`storage_already_absent`/
`storage_unresolved` — never folded into `storage_deleted`/`storage_already_absent`
merely because its Source ends up `storage_uri = NULL`. A `PipelineRun` may
**succeed** with `storage_unresolved > 0`, provided the authoritative
memory/projection/business deletion itself completed successfully. This is not
fabricated storage success — it is successful Sofias Memory deletion with explicitly
unresolved external artifact cleanup, visible in the run's own durable output. Do not
claim physical deletion when it was not proven, in metrics or in any other surface —
and, symmetrically, do not claim `DELETED`+`NULL` alone proves physical deletion was
proven, since `NOT_REQUESTED` can produce the identical durable state without that
proof.

### D40. Startup migration classification — tombstones are never migration candidates

**New section (deletion-semantics amendment) — makes the filesystem→S3 migration
candidate set fully explicit against the real `SourceStatus` enum (`domain/enums.py`:
`PENDING, PROCESSING, ACTIVE, FAILED, DELETING, DELETED`) and closes a gap D8's
original Scope note left open: it excluded `DELETING` but not `DELETED`, which — before
D39 introduced the "`DELETED` + retained locator" tombstone — could not yet occur for
a `file://` Source, but can now.**

Only **live** Source originals may be migrated:

```text
status IN (PENDING, PROCESSING, ACTIVE, FAILED) + file://
    => eligible migration-owned Source, subject to the existing D8 rules.

status == DELETING
    => destructive pipeline ownership (D34 Case B); NEVER migrate.

status == DELETED + storage_uri != NULL
    => unresolved storage-cleanup tombstone (D39, D37's UNRESOLVED outcome);
    => NEVER migrate;
    => NEVER upload/resurrect the original into S3 merely because
       STORAGE_BACKEND=s3.
```

A `DELETED` Source with a retained `file://` or `s3://` locator is not an active
Source-storage migration candidate under any configuration. D8's Scope note is amended
to state this exclusion explicitly (`status NOT IN (DELETING, DELETED)`), and D34 gains
a third case below for completeness against the same missing-file question D34
originally answered only for `DELETING`.

**D34, Case C (addendum) — `DELETED` Source, any storage_uri state.** Whether the
retained locator's object is present, absent, or unreachable is irrelevant to startup
convergence: a `DELETED` Source is never inspected for migration purposes at all
(D8's Scope note excludes it outright, before any file-presence check would even run).
This differs from Case B (`DELETING`), where the Source is still mid-transition and
its *absence* is specifically what the classification reasons about; a `DELETED`
Source is already past that transition — its tombstone (D39) is the final word, and
the retained locator (if any) is inert data for a possible future explicit cleanup
feature (D39), not a migration or reconciliation input for this ADR.

### D41. Rollback / lost S3 configuration — worked example

**New section (deletion-semantics amendment) — documents the exact supported
behavior when an operator moves away from S3 configuration while `s3://` Sources
still exist, then later triggers a deletion.**

```text
1. Source was finalized while STORAGE_BACKEND=s3.
2. PostgreSQL contains Source.storage_uri = s3://...
3. Operator later switches STORAGE_BACKEND=filesystem.
4. Operator removes/loses the S3 configuration entirely (credentials revoked,
   bucket decommissioned, etc.).
5. Existing derived Dataset memory remains in PostgreSQL, unaffected, until
   explicitly forgotten/deleted -- nothing about losing S3 config forces or
   implies any memory deletion.
6. A full Forget / Dataset DELETE later targets that Source.
7. SourceStorageRouter sees the s3:// scheme (D5 -- read/delete route by URI
   scheme, never by STORAGE_BACKEND) but cannot instantiate/access the
   required S3 backend.
8. Storage deletion result: UNRESOLVED (D37).
9. Authoritative memory deletion still completes (D37's business-delete-must-
   converge rule): PostgreSQL mutation, graph convergence, business
   finalization all proceed normally.
10. Source becomes status=DELETED, storage_uri=s3://... (D39 -- UNRESOLVED
    preserves the locator).
11. The PipelineRun SUCCEEDS, with unresolved-storage cleanup recorded in its
    metrics (D39).
```

The system does **not**:

- pretend the S3 object is absent;
- pretend the S3 object was deleted;
- block the worker indefinitely;
- resurrect/copy that deleted Source into the filesystem (D40 already forbids
  treating a `DELETED` Source as a migration candidate, independent of this
  scenario);
- erase the retained `s3://` locator.

Operational documentation (`docs/operations.md`, flagged again in "Repository areas,"
below) must warn that the external S3 object may still physically exist and Sofias
Memory cannot guarantee its removal until the backend becomes accessible again.

### D42. Consistency of D37–D41 with everything already frozen

**New section (deletion-semantics amendment) — the explicit "does not weaken" list
the review requested, stated once rather than repeated in every section above.**

This round of amendment does **not** weaken:

- PostgreSQL authority for Sofias Memory memory (ADR-0002) — unchanged;
- graph projection convergence requirements (D9 step 3, ADR-0010 D9 step 3,
  ADR-0008) — `converge_projection` still must complete before storage deletion is
  even attempted, exactly as today; this amendment touches only the storage step and
  what follows it;
- exact-key storage safety (D6, D15, D24, D35) — no prefix/global operation is ever
  introduced by `UNRESOLVED`; an unresolved outcome is a *refusal to guess*, not a
  broadened target;
- no external I/O in `persist()` (D27) — `StorageDeletionStep`/`DeleteStorageStep`
  still perform all storage I/O, including determining `UNRESOLVED`, in `execute()`;
  `persist()` still only records the already-computed typed outcome and (for
  `DELETED_NOW`/`ALREADY_ABSENT`/`NOT_REQUESTED`, D39) clears `storage_uri`, or (for
  `UNRESOLVED`) leaves it untouched — a pure, no-I/O PostgreSQL write in either case;
- no PostgreSQL lock/transaction across storage I/O (D10, D27) — unchanged;
- deterministic S3 keys (D6) — unchanged;
- the requirement that `DELETED_NOW`/`ALREADY_ABSENT` may only be claimed with
  positive evidence (D15, D38) — this is in fact *reinforced*, not weakened: the
  explicit `UNRESOLVED` outcome exists precisely so that an adapter is never tempted
  to report a false positive merely to let the pipeline proceed, since the pipeline no
  longer needs a false positive to proceed at all;
- `storage_uri` preservation when cleanup is unresolved (D39) — this is the new
  mechanism this amendment adds, not a relaxation of an existing one.

### D43. Convergence / destructive-lifecycle exclusion (STORAGE-006 CAS-loss safety
amendment)

**New section (fifth amendment) — resolves the real architecture gap the STORAGE-006
implementation audit found: a deterministic S3 `PUT` may succeed and be strongly
verified (D8 steps F/G) before migration's own PostgreSQL CAS (D8 step H) commits; if
a concurrent destructive pipeline transitions the same Source from live+`file://` to
`DELETING` in that exact window, deletes only the `file://` original, and finalizes
`DELETED`+`NULL`, the already-uploaded S3 object becomes permanently undiscoverable
(D40 correctly forbids ever inspecting a `DELETED` Source again, and no durable
record of the migration attempt exists to recover it). See the Fifth amendment note
in Status above for the full finding.**

**Decision.** During STORAGE_CONVERGING, no migration-eligible live Source may begin
a *new* destructive authoritative transition. Migration-eligible means exactly D34
Case A's set:

```text
Source.status IN (PENDING, PROCESSING, ACTIVE, FAILED)
    AND
storage_uri = file://...
```

Normal business activity and normal worker claims capable of transitioning such a
Source to `DELETING` remain blocked throughout STORAGE_CONVERGING, exactly as D31
already requires for every other normal `PipelineRun` claim — this decision adds no
new blocking mechanism, it makes explicit that D31's existing "no new normal
`PipelineRun` claim" rule already has this consequence and freezes that consequence
as load-bearing rather than incidental.

Only **recovery-owned** destructive work may progress during STORAGE_CONVERGING,
unchanged from D31. Recovery-owned destructive work requires durable PostgreSQL proof
that:

1. the Source is already `DELETING`;
2. a compatible existing `forget`/`dataset_delete` `PipelineRun` lineage exists (D34
   Case B's proof predicate, unchanged);
3. that lineage's authoritative destructive mutation has already durably occurred
   (i.e. the Source did not enter `DELETING` *during* this convergence pass — it was
   already `DELETING`, with proven lineage, before the pass began classifying).

Therefore, for the duration of one STORAGE_CONVERGING pass, D34 Case A (migration-
owned) and destructive-transition ownership are **disjoint sets**: a Source this pass
classifies Case A cannot simultaneously be a Source a recovery-owned run is entitled
to transition into `DELETING`, because recovery-owned runs only ever act on Sources
**already** `DELETING` when the pass starts. The race precondition — a live Case-A
Source acquiring a *new* `DELETING` transition while a migration attempt for it is
mid-flight — is removed structurally, not reconciled after the fact.

**CAS-loss semantics, refined (does not alter D8/D10's mechanics, only their
classification of outcomes).**

- CAS loss because the Source changed to *another live migration-eligible* status
  (e.g. `ACTIVE` → `FAILED`, both still D34 Case A) — safe reclassification; the
  deterministic S3 target remains discoverable and gets adopted on this or a future
  pass. Unchanged from STORAGE-006.
- CAS loss because another migrator already adopted the exact deterministic `s3://`
  target — `ALREADY_CONVERGED`/idempotent success. Unchanged from STORAGE-006 (D10).
- CAS loss because the Source is observed `DELETING` — under D43, this must never
  arise from a validly-supported single-replica MVP deployment, because D43 forecloses
  a live Case-A Source from acquiring a *new* `DELETING` transition during
  STORAGE_CONVERGING in the first place. If it is nonetheless observed, it is an
  **internal lifecycle invariant violation**, not a benign "another legitimate
  pipeline now owns it" outcome — it must fail closed and be surfaced as an integrity
  condition (D19), never silently treated as ordinary CAS contention. (A Source that
  was *already* `DELETING` with proven lineage before the pass began was never Case A
  to begin with — D34's classifier excludes it from migration at the classification
  step, long before any CAS could be attempted; this refined CAS-loss case is about a
  Source that *was* validly snapshotted as Case A and is later found `DELETING`,
  which D43 says cannot happen.)

**Crash consequence — the PUT-before-CAS window is self-healing, without a new
durable migration-attempt ledger.** Explicit crash window:

```text
1. S3 PUT succeeds and is strongly verified (D8 steps F/G);
2. migration's PostgreSQL CAS (D8 step H) has not yet committed;
3. process crashes.
```

Durable state left behind: `Source.status` still live/migration-eligible,
`Source.storage_uri` still `file://...`, and (possibly) a matching, already-verified
`s3://` object at the deterministic key. On restart, STORAGE_CONVERGING runs again
*before* any normal business/worker activity (D7/D31's fixed ordering) — and, per
D43, before any *new* destructive transition of this Source can occur at all. The
Source is therefore still classified D34 Case A on the next pass; `finalize()`'s
idempotent existing-object check (D8 step F) rediscovers the matching target,
`verify()` re-confirms it (D8 step G), and CAS (D8 step H) commits normally. No new
durable bookkeeping of in-flight migration attempts is required for this crash window
specifically, because D43 guarantees the Source cannot have left the migration-owned
set in the interim.

**Relationship to D31/D34 (clarification only, no narrowing).** D31's "no new normal
`PipelineRun` claim" rule and D34's Case A/B/D classifier already implied this
outcome; D43 states it explicitly as the property the STORAGE-006 audit needed and
did not find spelled out. D31's recovery-owned-work allowance is unchanged: it exists
precisely because a recovery-owned lineage's authoritative destructive mutation is
**already durable** and its Source is **already** `DELETING` before STORAGE_CONVERGING
begins — D43 does not touch that allowance, it only forecloses *new* destructive
mutations against Case-A Sources. D34's Case B proof predicate (compatible, provable
lineage) is unchanged; D34's Case D fail-closed default is unchanged and not weakened
by D43 in any way — a `DELETING` Source with no provable lineage is still Case D
regardless of when it entered `DELETING`.

**D43's safety proof depends explicitly on D31's sixth-amendment claim predicate
(cross-reference, not a new decision).** D43's central claim — "a valid
recovery-owned run cannot create a new Case-A → `DELETING` transition, because all
authoritative destructive mutation for its complete scope is already durable before
it becomes claimable during STORAGE_CONVERGING" — is only true given D31's
sixth-amendment correction: recovery-owned claim eligibility requires the run's own
`authoritative_mutation`/`deactivate_authoritative` step to have durably `succeeded`
for its **complete** target scope, not merely that the run's scope contains a Case-B
Source. Under D31's original (pre-sixth-amendment) wording, a run could in principle
be claimed while still needing to execute a `DELETING`-causing mutation against a
live Case-A Source elsewhere in its own scope, reopening exactly the race D43 exists
to close (see D31's `DATASET_DELETE` multi-source worked counterexample). D43's
architecture (lifecycle exclusion) is unchanged by this cross-reference; only the
precision of the claim-eligibility predicate it relies on changed.

**Single-replica MVP boundary (explicit, sharpened by the sixth amendment).** This
safety argument depends on the already-accepted MVP constraint of exactly one
operational application replica (ADR-0009, `Explicit Non-Goals`). Concurrent/
idempotent migration attempts *within* that one process (or a crash-restarted
successor never running concurrently with its predecessor) remain safe under D10's
existing analysis. However, **one process already `OPERATIONAL` while a second,
independent process is concurrently `STORAGE_CONVERGING` against the same
PostgreSQL/S3 state is not a supported MVP deployment mode** — D43's exclusion
argument assumes a single process is the sole author of both destructive transitions
and migration attempts at any given time.

`replicas = 1` alone does not guarantee this. An orchestrator configured for
start-first ("rolling") deployment overlap will, by design, start the new process
(which enters BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING) *before* stopping the old
one (which may still be `OPERATIONAL`, still holding normal worker claims and still
able to transition a live Case-A Source to `DELETING` at the exact moment the new
process is migrating it) — precisely the unsupported overlap above, achieved even
under `replicas = 1` semantics. Excluding the new process from receiving load
(readiness/load-balancer exclusion) is *also* insufficient by itself, because
readiness governs inbound HTTP traffic, not whether the *old* process still owns
worker claims and business-mutation capability.

**Frozen requirement:** the supported MVP deployment model uses **stop-old-before-
start-new process exclusivity** (a recreate/stop-first deployment strategy, or an
equivalent mechanism that guarantees the previous process has fully exited — no
longer claiming work, no longer serving business routes — before a new process
begins BOOTSTRAP/MAINTENANCE) for any `STORAGE_BACKEND=s3` deployment. This is a
**deployment-configuration/documentation obligation**, not a runtime interlock this
ADR designs or enforces in code — no distributed lock, advisory lock, or migration
ledger is introduced to enforce it. Future rolling-overlap or multi-replica support
remains explicitly out of scope and would require a durable, cross-process
ownership/interlock mechanism (e.g. a migration-intent ledger or equivalent),
deferred to a future architecture decision.

**Alternatives considered for this amendment (STORAGE-006 audit's own candidates,
recorded here rather than only in the audit report).**

1. **Durable migration-intent ledger** (a new durable record of "migration attempted
   key K for source S, not yet adopted"). Rejected for this increment: correct in
   principle, but adds schema/state complexity that D43's lifecycle exclusion makes
   unnecessary under the accepted single-replica MVP constraint.
2. **Destructive pipeline also deletes the deterministic S3 target for `file://`
   Sources** (an auxiliary, best-effort exact-key delete alongside the existing
   `file://` delete). Rejected: insufficient by itself — an in-flight migration `PUT`
   could land *after* the destructive pipeline's auxiliary S3 delete runs, recreating
   the exact same orphan under a different ordering, without resolving the underlying
   race.
3. **Holding a PostgreSQL lock across S3 I/O** (to serialize migration and destructive
   transitions at the row level). Rejected: violates the already-accepted
   no-lock-across-external-I/O invariant (ADR-0009 §D, D10, D27) this entire ADR
   depends on elsewhere.
4. **Scanning `DELETED` Sources or the S3 namespace for orphans.** Rejected: violates
   D40's categorical exclusion of `DELETED` tombstones from migration inspection and
   D36's managed-namespace-ownership discipline (no broad/discovery-based operations),
   and adds unnecessary startup cost with no bound on Source count.

**Does not weaken (added to D42's list, same discipline):** D31's recovery-owned-work
allowance; D34's Case B proof predicate and Case D fail-closed default; D8–D10's
migration mechanics; D36's managed-namespace exclusivity; D37–D40's business-delete-
must-converge/tombstone contract. D43 adds a structural precondition
(migration-eligible and destructive-transition ownership are disjoint during
STORAGE_CONVERGING); it changes no existing outcome's meaning.

## Startup convergence state machine

**Amendment (review findings M1, M1.2) — replaces the original diagram, which
collapsed schema-gating, migration, and recovery-owned reconciliation into a single
undifferentiated "migrate all `file://`" branch and did not name the process states
explicitly.** This diagram is normative together with D31–D36.

```text
PROCESS START
    |
    v
BOOTSTRAP/MAINTENANCE  (D31)
live=yes / ready=no ; maintenance HTTP surface available (D33)
    |
    v
SCHEMA VALID? (D32)
    | no
    +----> remain BOOTSTRAP/MAINTENANCE indefinitely
    |      Alembic remains explicit; operator runs `alembic upgrade head`
    |      no Source inspection, no storage convergence, no worker claims
    |
   yes
    |
    v
STORAGE_BACKEND ?
    |
    +-- filesystem --> verify persistent storage root usable (D20)
    |                        |
    |                        v
    |                  OPERATIONAL: normal worker enabled, live=yes/ready=yes
    |
    +-- s3 ---------> enter STORAGE_CONVERGING (D31)
                       live=yes / ready=no ; maintenance HTTP surface available
                             |
                             v
                       probe S3 (D21) --fail--> stay STORAGE_CONVERGING,
                             |                   NOT_READY (D19); retried on
                            pass                 next restart / next poll
                             |
                             v
                       classify durable Sources (D7 Scope note, D34)
                             |
              +--------------+----------------+------------------+
              |                                |                  |
        migration-owned                  recovery-owned      excluded: NULL
     (file://, status!=DELETING)    (file://, DELETING,     storage_uri (owned
              |                       D34 Case B)             by Remember's own
        D8 A-I convergence:                |                 retry, D12/B1 --
        snapshot -> validate URI      existing pipeline       storage convergence
        -> hash-check local           engine (ADR-0009,       never touches these)
        -> compute target key         unmodified) claims
        -> inspect target (D8 F)      and progresses this
        -> strong verify (D11)        lineage only (D31)
        -> CAS repoint PostgreSQL           |
        -> any step fails closed      reaches its own
           per D19 -> NOT_READY       legitimate durable
              |                       terminal state
              +----------------+-----------------+
                                |
                                v
                cleanup confirmed local duplicates
                using the D35 legacy-locator contract (D9)
                                |
                                v
                verify convergence: migration-owned set fully
                repointed AND recovery-owned lineage terminal
                AND no unresolved D19 condition remains
                                |
                     fail       |       pass
               NOT_READY <------+------> OPERATIONAL
               (stay STORAGE_CONVERGING,          normal worker enabled for
                retried on next restart/           every pipeline type
                poll; no time limit, D33)          live=yes / ready=yes
```

Every non-vacuous branch of the `s3` path is idempotent and safe to re-enter on the
next restart if the process crashes anywhere in it (D9, D10, D31) — there is no
"partially-through-migration" state that a subsequent boot cannot resume correctly
from, because every step is either read-only, a deterministic-and-idempotent external
operation, a PostgreSQL CAS, or delegated to the already-crash-safe ADR-0009 engine.

## Crash windows and why each is safe

| # | Crash point | Resulting state | Why it is safe |
|---|---|---|---|
| 1 | Before S3 upload begins | PostgreSQL `file://`, S3 absent | Next boot re-enters D8 from step A; nothing was mutated |
| 2 | During/after S3 upload, before strong verification | PostgreSQL `file://`, S3 object present but unverified | Next boot re-enters step F, finds the object, strongly re-verifies (D8 F/G) before proceeding; a corrupted partial upload fails verification and is treated as step F's "conflicting content" or re-uploaded, never trusted blindly |
| 3 | After strong verification, before PostgreSQL CAS | PostgreSQL `file://`, S3 verified | Next boot's CAS (D8 H) proceeds normally against the already-verified object — cheap check (D11) confirms it, no re-upload needed |
| 4 | After PostgreSQL CAS commits, before local cleanup | PostgreSQL `s3://` (authoritative, verified), local file still present | **D9's core case.** Safe because PostgreSQL already points at proven-good S3 bytes; the local file is redundant, not relied upon. Next boot's cleanup pass (D9) removes it once it re-confirms the match |
| 5 | During local file cleanup (partial unlink, e.g. process killed mid-syscall) | PostgreSQL `s3://`, local file either gone or still present | Filesystem `unlink()` is atomic at the OS level for a single file — there is no partial state to reconcile; next boot's cleanup re-evaluates and finishes if not yet done |
| 6 | Two processes converging the same Source concurrently | Both may upload; only one CAS wins | D10: idempotent upload to a deterministic key is safe for both; the losing CAS matches zero rows and the losing process re-reads PostgreSQL, observes the winner's already-verified `s3://` value, and treats the Source as done |
| 7 | Process crashes with S3 credentials revoked mid-migration | Some Sources converged, others still `file://`, S3 probe now failing | Next boot's S3 probe (D21) fails closed before the scan even runs (D19) — no partial-trust state is ever exposed to the worker/readiness |

### Backend-transition crash windows across Remember/Forget/Dataset DELETE (B1, B2)

**New table, added by this amendment (review findings B1, B2)** — these four cases
involve a `STORAGE_BACKEND` change landing mid-flight on an *existing business
pipeline's* own crash window, not on D8's migration algorithm itself. Each is proven
against: who is authoritative, who owns recovery, whether S3 upload is permitted,
whether local deletion is permitted, and why bytes cannot be lost or resurrected
incorrectly.

| Case | Crash point | Authority (PostgreSQL) | Recovery owner | S3 upload allowed? | Local deletion allowed? | Why safe |
|---|---|---|---|---|---|---|
| A (B1) | Remember: final local object written + verified, `_ingress` already deleted, crash **before** `FinalizeStorageStep.persist()` | `storage_uri = NULL` | The existing Remember `PipelineRun`'s own `FinalizeStorageStep` retry (D12) — **never** the startup scanner | Yes — as recovery input, once the legacy object's identity/hash/size are re-verified against the already-committed Source row (D12) | Only after the S3 object is strongly verified **and** `storage_uri` is durably repointed by that same step's `persist()` (D12's ordering) | The legacy object is proven, by the pre-existing `FinalizeStorageStep` idempotency contract, to be exactly the bytes this Source's own attempt already wrote and hashed; nothing about `STORAGE_BACKEND` changing invalidates that proof — it only changes where the *next* write lands |
| B (B2) | Forget: local delete succeeded, crash **before** `finalize_target` | `storage_uri = file://...`, `status = DELETING` | The existing Forget `PipelineRun`'s own `StorageDeletionStep`/`finalize_target` resume (D31, D34 Case B) | **No** — D8's Scope note excludes every `DELETING` Source outright; the bytes are gone, there is nothing to upload | **N/A** — already deleted by the original attempt; storage convergence performs no filesystem operation on this Source at all | `AMBIGUOUS` classification (ADR-0009 §I) already treats "already absent" as legitimate reconcilable progress; re-uploading here would resurrect deleted content behind PostgreSQL's back — exactly what D34 Case B forbids |
| C (B2) | `DATASET_DELETE`: local delete succeeded, crash **before** `finalize_tombstone` | `storage_uri = file://...`, `Dataset.status = DELETING`, `Source.status` per ADR-0010 D11 | The existing `DATASET_DELETE` `PipelineRun`'s own `DeleteStorageStep`/`finalize_tombstone` resume (D31, D34 Case B, ADR-0010 D9) | **No**, same reasoning as Case B | **N/A**, same reasoning as Case B | Identical to Case B, generalized per D28 to the administrative-delete lineage; storage convergence must not race or duplicate ADR-0010's own tombstone finalization |
| D (M2) | Migration: PostgreSQL CAS to `s3://` succeeded, crash **before** local duplicate deletion, and the old `file://` URI is no longer stored anywhere | `storage_uri = s3://...` (already verified, D8 step G) | Next boot's D9 cleanup pass, using **only** the D35 locator contract (`dataset_id` + `id` + `mime_type` → canonical extension) — never a re-read of the (already-overwritten) old URI | N/A — already uploaded and verified in the prior pass | Yes, but only after D35's full pre-deletion sequence (locate deterministically → confirm S3 target valid → confirm exact local path exists → hash it → require match to `Source.content_sha256` → then unlink) | PostgreSQL already authoritatively points at proven-good S3 bytes (D9's core safety argument, row 4 above); D35 guarantees the local file identified for deletion is derived from durable identity, never from directory discovery, so a wrong/unrelated file can never be removed even though the original `file://` URI is gone |

## Filesystem/S3 routing semantics

- **Write** (`SourceStorageRouter.finalize`): governed by `Settings.storage_backend`
  only (D2, D4).
- **Read** (`SourceStorageRouter.read`) and **Delete**
  (`SourceStorageRouter.delete`/`verify`): governed exclusively by the scheme of the
  `Source.storage_uri` value already on the row (D4, D5, D13, D14) — `STORAGE_BACKEND`
  is never consulted for these paths, so a mixed-scheme Dataset (some Sources `file://`,
  some `s3://`, e.g. mid-convergence or a Dataset created before and after a backend
  switch) is read/deleted correctly without any special-casing at the pipeline/service
  layer.

## S3 versioning / delete semantics

Covered in full at D15: version-aware exact-key deletion required, delete-marker-only
outcomes rejected as "deleted," fail-closed on Object Lock/retention/permission
obstruction, no prefix-wide destructive operation ever issued, documented required
permissions.

## Repository areas that later implementation must change

Findings from the consistency review below (D24's scope: identify, not fix):

1. **`sofias_memory/config.py`** — add `storage_backend` and the `STORAGE_S3_*`
   fields (D16), with validators mirroring existing patterns
   (`validate_http_url`-style for `STORAGE_S3_ENDPOINT_URL`, required-when-`s3`
   cross-field validation similar to `validate_cross_field_rules`). This is a live
   contract file, not historical.
2. **`sofias_memory/services/remember.py`** — `final_storage_directory`,
   `final_storage_path`, `final_storage_uri`, `write_final_storage_bytes`,
   `final_storage_content_matches` currently hard-code filesystem I/O and must become
   (or be wrapped by) `FilesystemSourceObjectStorage`; `prepare_remember_retry_ingress`
   and the `_ingress/*` helpers are explicitly **unchanged** (D3) and must stay
   filesystem-only regardless of `STORAGE_BACKEND`.
3. **`sofias_memory/services/forget.py`** — `delete_source_storage`,
   `source_storage_path`, `StorageDeleteResult`/`StorageDeleteStatus` must be
   generalized behind the router (D14) with `source_storage_path`'s containment logic
   preserved as the filesystem adapter's own read/delete path resolution.
   `invalid_storage_uri_error` needs an S3-scheme-aware counterpart.
4. **`sofias_memory/services/cognify.py`** — `source_storage_path_for_cognify` must
   be replaced by a `SourceStorageRouter.read` call (D13).
5. **`sofias_memory/pipelines/steps/remember.py`** (`FinalizeStorageStep`) — must
   call the router's `finalize` instead of `write_final_storage_bytes` directly;
   `persist()`'s pure-URI-computation discipline (D27) must be preserved for the S3
   adapter's own URI builder, mirroring `final_storage_uri`'s no-I/O contract.
6. **`sofias_memory/pipelines/steps/forget.py`** (`StorageDeletionStep`,
   `_delete_one_source_storage`, `_delete_dataset_storage`) and
   **`sofias_memory/pipelines/steps/dataset_delete.py`** (`DeleteStorageStep`) — must
   call the router's `delete` instead of `delete_source_storage` directly (D14, D28).
7. **`sofias_memory/lifespan.py`** — must gain the storage convergence gate (D7),
   positioned between the existing PostgreSQL probe / pipeline recovery block and
   `worker.start()`; `app_settings`/`app.state` needs a new accessor for the storage
   router (mirroring `app_pipeline_registry`/`app_pipeline_worker`'s existing pattern)
   so routes and pipeline steps resolve the same router instance the lifespan wired up.
   **Amended scope (D31–D33):** this file must also implement (a) the three-state
   BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING → OPERATIONAL model as an observable
   process state (not merely an internal boolean), (b) a claim-eligibility filter so
   the worker, once started, claims only recovery-owned `forget`/`dataset_delete`
   lineage while in STORAGE_CONVERGING and every pipeline type once OPERATIONAL, and
   (c) whatever concrete mechanism (route-level guard, dependency check, or an
   earlier-than-`lifespan` maintenance app) is needed to guarantee `/health/live` is
   actually reachable throughout BOOTSTRAP/MAINTENANCE and STORAGE_CONVERGING, per
   D33's observable-liveness requirement — simply placing a call before
   `worker.start()` inside a blocking `lifespan` body is not, by itself, sufficient
   to satisfy that requirement.
8. **`sofias_memory/infrastructure/postgres/models/source.py`** — no migration
   required (D26), but the module's docstrings/comments describing `storage_uri` as a
   filesystem path (if any are added elsewhere referencing "the file") should be
   revisited for accuracy once S3 exists.
9. **`compose.yaml` / `deploy/easypanel/compose.yaml`** — will eventually need the new
   `STORAGE_BACKEND`/`STORAGE_S3_*` environment entries (commented/optional, default
   `filesystem`) — not changed by this ADR (explicitly out of scope for this task).
10. **`docs/operations.md`** — backup/restore procedures (section referencing
    `/data/sources` as one of the two authoritative-and-must-be-backed-up things,
    lines ~13-20) need a parallel S3-mode section per D22; the first-start procedure
    needs the storage convergence gate documented alongside its existing Alembic
    precondition language (D7).
11. **`docs/adr/0010-administrative-dataset-deletion-contract.md`** — not rewritten by
    this task (per instructions), but D9's step-4 description and D26's storage
    citation should eventually gain a forward-reference to this ADR's generalization
    (D28) rather than continuing to read as filesystem-only.
12. **`sofias_memory/config.py`'s `build_config_fingerprint_payload`** — must remain
    unchanged by the storage feature (D18); a later implementer must resist the
    natural instinct to add `storage_backend` to the fingerprint "for completeness."
13. **API schemas layer (`sofias_memory/schemas/`)** — verified in this review to
    already exclude `storage_uri` from public projections (D23); later implementation
    must not introduce it while adding S3-aware fields for some other reason (e.g. a
    debug/admin surface).
14. **`AGENTS.md` §4 "Stack fixa"** — currently lists the fixed stack without an S3
    SDK; once implementation lands, that list (and any "filesystem local" wording)
    needs updating to reflect the two first-party backends, consistent with D17's
    ADR-0005 amendment.
15. **`sofias_memory/loaders/text.py`** — `STORAGE_EXTENSION_BY_SOURCE_EXTENSION`
    and `MIME_TYPE_BY_SOURCE_EXTENSION` (lines 26–47) must gain a sibling
    `STORAGE_EXTENSION_BY_MIME_TYPE` mapping (or an equivalent centralized function),
    required by D6's amendment and D35's legacy-locator contract — confirmed
    derivable from the existing two tables without any behavior change, but not yet
    expressed as its own named, durable-input-keyed mapping.
16. **`sofias_memory/pipelines/steps/remember.py`**'s `FinalizeStorageStep` — must
    gain the D12/B1 legacy-object-recovery branch (detect `storage_uri = NULL` +
    absent target + absent ingress + present verified legacy local object; upload,
    verify, and persist through the existing step, never a new code path outside it).
17. **`sofias_memory/services/pipeline_queue_claimer.py`** (ADR-0009's claim query
    implementation) — must gain the STORAGE_CONVERGING-scoped claim-eligibility
    filter D31 requires (recovery-owned `forget`/`dataset_delete` lineage only, while
    every other pipeline type's claims stay withheld) — the query itself is
    unmodified in its locking/advisory-lock logic; only its eligibility predicate
    gains a state-dependent narrowing.

**Amendment — added by the deletion-semantics review (D37–D41):**

18. **`sofias_memory/services/forget.py`**'s `StorageDeleteResult`/
    `StorageDeleteStatus` and `delete_source_storage` — must gain the `UNRESOLVED`
    outcome (D37), the positive-evidence `ALREADY_ABSENT` discipline restated in D38,
    and must stop treating every non-`DELETED_NOW`/`ALREADY_ABSENT` outcome as an
    exception to propagate; typed/recognized conditions become a returned value, not
    a raised error (`DependencyUnavailableError` is reserved for genuinely
    unrecognized/defect conditions after this change, per D37).
19. **`sofias_memory/pipelines/steps/forget.py`**'s `StorageDeletionStep` and
    **`sofias_memory/pipelines/steps/dataset_delete.py`**'s `DeleteStorageStep` —
    must stop blocking `finalize_target`/`finalize_tombstone` on physical proof;
    must pass the typed `StorageDeleteResult` through as durable step output; must
    clear `storage_uri` only for `DELETED_NOW`/`ALREADY_ABSENT` and preserve it for
    `UNRESOLVED` (D39) in the finalize step's own `persist()`.
20. **`sofias_memory/infrastructure/postgres/models/source.py` / ADR-0010's
    `finalize_tombstone`, `_finalize_dataset_target`** — the finalizer's own logic
    for clearing vs. preserving `storage_uri` on the `DELETED` transition must be
    updated per D39; no schema migration is required (D39's own no-new-column
    finding).
21. **`sofias_memory/schemas/`** (`DatasetDeleteResult` and any Forget result schema,
    ADR-0010 D24) — must add the D39 metrics (`storage_deleted`,
    `storage_already_absent`, `storage_unresolved`, optionally
    `storage_cleanup_complete`), PostgreSQL-metrics-only, consistent with ADR-0010
    D24's existing "no live Neo4j/filesystem read at result-construction time" rule.
22. **`docs/operations.md`** — gains the D41 operator warning: after a
    `STORAGE_BACKEND` change or lost S3 configuration, a `DELETED` Source's retained
    `storage_uri` means the physical original may still exist and Sofias Memory
    cannot guarantee its removal until the backend becomes accessible again; this is
    additional to the D22 backup/restore section already flagged above (item 10).
23. **D8's Scope note / the startup scan (D7)** — already amended in this document
    (D40) to exclude `DELETED` Sources outright, not only `DELETING` ones; later
    implementation's scan query must filter `status NOT IN ('deleting', 'deleted')`,
    not merely `status != 'deleting'`.

**Amendment — added by the contract-completeness review:**

24. **`sofias_memory/pipelines/steps/forget.py`'s `finalize_target` and
    `sofias_memory/pipelines/steps/dataset_delete.py`'s `finalize_tombstone`** — must
    gain the D37 per-Source coverage check: before transitioning any Source
    `DELETING → DELETED`, confirm an explicit `StorageDeleteResult` exists for that
    `source_id` in the step's own durable output; raise an internal invariant failure
    (a genuine step/finalize failure, never a silent `UNRESOLVED`) if one is missing.
25. **The startup scan's Case B/Case D classifier (D7/D34)** — must implement the
    lineage-proof query: given a `DELETING` + `file://` + missing-object Source, find
    a compatible non-terminal-or-awaiting-retry `forget`/`dataset_delete`
    `PipelineRun` whose scope includes it; absence of such a row routes the Source to
    Case D (fail closed), not Case B.

The following remain open, deliberately, because they are implementation choices, not
architecture:

- Exact S3 SDK (e.g. `aioboto3` vs. `boto3` wrapped in a thread executor vs. an
  alternative async-native client) and its exact version pin.
- Exact strong-verification mechanism for D11 (full download-and-hash vs. a
  provider-specific mechanism proven equivalent).
- Exact probe-object naming/prefix and cleanup mechanics for D21.
- Exact metrics/log field names for D19's observability requirements (the *content*
  of what must be logged is frozen; field naming is not).
- Final class/module names and file layout for `SourceObjectStorage`/
  `FilesystemSourceObjectStorage`/`S3SourceObjectStorage`/`SourceStorageRouter` within
  `sofias_memory/infrastructure/storage/` (a new package, following the existing
  `infrastructure/postgres/`, `infrastructure/neo4j/` sibling pattern).
- Exact concrete mechanism for D33's maintenance-HTTP-availability requirement (a
  guard on every business route, a dependency-level check, or a distinct minimal app
  mounted ahead of the full one) — the *behavior* is frozen (D31, D33), the mechanism
  is not.
- Exact worker claim-filter query shape for D31's STORAGE_CONVERGING eligibility
  narrowing (a `pipeline_type IN (...)` predicate added to the existing claim query,
  a separate pre-check before the existing claim attempt, or an equivalent) — the
  requirement (normal claims blocked, recovery-owned claims allowed, no second
  engine) is frozen, the SQL/query-construction detail is not.
- Exact content of the `STORAGE_EXTENSION_BY_MIME_TYPE` mapping's implementation
  location/shape (D35's amendment to D6 fixes the *requirement*, not the concrete
  data structure or module).

The following are explicitly **not** left open, per the task's requirement, and are
frozen by the sections cited:

- ingress location (D3);
- persistent-volume requirement (D1, D22);
- `storage_uri` schemes (D6);
- active-write-vs-URI-read routing split (D4, D5);
- automatic startup convergence (D7);
- migration ordering (D8);
- crash safety (D9, "Crash windows" table, and the B1/B2/M2 table);
- local cleanup rules (D9, D24, D35's exact locator contract);
- versioned-bucket deletion semantics (D15);
- no PostgreSQL lock/transaction across S3 I/O (D10);
- no automatic reverse migration (D5, D25);
- Remember `storage_uri = NULL` legacy-object recovery ownership and sequence (D12,
  amendment B1);
- destructive-pipeline missing-file classification, Case A vs. Case B (D34,
  amendment B2);
- recovery-owned destructive work's behavioral contract during STORAGE_CONVERGING —
  what may progress, what may not, and who owns business finalization (D31, amendment
  B2.1);
- the BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING → OPERATIONAL process state model
  and its `/health/live`/`/health/ready` contract (D31, D33, amendment M1);
- Alembic strictly precedes and gates storage convergence, with no automatic schema
  migration (D32, amendment M1.1);
- no fixed maximum storage-migration duration and no reliance on `start_period` alone
  (D33, amendment M1.2);
- post-CAS legacy local path derivation contract (D35, amendment M2);
- managed S3 namespace exclusive-write ownership, prefix-scoped not bucket-wide (D36,
  required invariant).

**Amendment — added by the deletion-semantics review (D37–D41), also not left open:**

- the `StorageDeleteResult` four-outcome contract and the requirement that
  `UNRESOLVED` is a typed, successful step outcome rather than a `PipelineStep`
  failure (D37);
- the positive-evidence requirement for both `DELETED_NOW` and `ALREADY_ABSENT`,
  symmetrically (D37, D38);
- business-delete-must-converge: full Forget/Dataset Forget/Forget Everything/
  `DATASET_DELETE` may finalize with unresolved storage cleanup (D37);
- the expected-storage-failure-vs-software-defect distinction and the prohibition on
  a blanket `except Exception: return UNRESOLVED` (D37);
- `storage_uri` retention on `DELETED` as the durable representation of unresolved
  cleanup, with no new PostgreSQL column (D39);
- `DELETED` Sources are categorically excluded from filesystem→S3 migration and from
  Cognify/Remember-recovery treatment as live Sources, regardless of retained
  locator scheme (D40).

Genuinely still open (implementation-level, not architecture): the exact public/
internal metric field names for `storage_deleted`/`storage_already_absent`/
`storage_unresolved`/`storage_cleanup_complete` (D39 already notes this); the exact
mechanism the S3/filesystem adapters use to distinguish a "recognized storage
condition" from a genuine defect at the code level (the *requirement* is frozen by
D37, the classification mechanism — a typed exception hierarchy, a result-type return,
or an equivalent — is not).

## Test / validation consequences to record

Later implementation (an SM-5xx-equivalent story) must produce, against real
infrastructure (PostgreSQL + Neo4j as today; an S3-compatible integration target such
as MinIO for most cases, explicitly **not** assumed sufficient alone for
AWS-S3-specific versioning/delete-marker semantics, which need their own targeted
coverage):

1. all existing filesystem tests remain green under default configuration;
2. fresh filesystem install (unchanged behavior);
3. fresh S3 install (vacuous convergence, D7);
4. Remember text → S3 (D12);
5. Remember file → S3 (D12);
6. Remember URL → S3 (D12);
7. queued Remember with local `_ingress` surviving a filesystem→S3 redeploy (D3);
8. Cognify rehydrates from S3 (D13);
9. `memory_only` Forget preserves the S3 original (D14);
10. full Forget deletes the S3 original (D14, D15);
11. Dataset DELETE deletes S3 originals (D14, D28);
12. versioned-bucket destructive-deletion semantics (D15) — all versions/delete
    markers actually removed, verified;
13. migration target object already exists with the correct hash (D8 F "already
    copied" path);
14. migration target object exists with the wrong hash → fail closed (D8 F conflict
    path);
15. crash after S3 upload but before the PostgreSQL CAS (crash window #2/#3 in the
    table above);
16. crash after the PostgreSQL `s3://` CAS but before local cleanup (crash window #4,
    D9's core case);
17. restart resumes/converges both crash cases above correctly;
18. missing legacy Source file → startup blocked (D8 D, D19);
19. legacy local SHA mismatch → startup blocked (D8 D, D19);
20. S3 unavailable → startup blocked (D19, D21);
21. S3 access denied → startup blocked (D19, D21);
22. unrelated `DATA_DIRECTORY` content survives migration (D24);
23. `_ingress/` survives migration (D3, D24);
24. concurrent startup attempts remain correct (D10);
25. no PostgreSQL transaction/lock spans S3 I/O (D10, structural/code-review assertion);
26. deletion of an already-absent S3 object remains idempotent (D14);
27. S3 object version/delete-marker cleanup is proven, not merely assumed (D15);
28. a production-shaped deployment smoke test with the S3 backend enabled end to end.

**Amendment — added by this review (B1, B2, B2.1, M1, M2):**

29. Remember final filesystem object written + verified, ingress already deleted,
    crash before `storage_uri` persist, then `STORAGE_BACKEND` switched to `s3` and
    restarted: the existing `PipelineRun` recovers without a new Source and without
    a URL/content re-fetch (D12/B1's exact scenario, including a final assertion that
    the legacy local duplicate is safely cleaned only after the S3 repoint is
    durable).
30. Same B1 case with a corrupt or missing legacy final artifact → fails closed
    (`REMEMBER_INGRESS_MISSING` or equivalent), never fabricates success.
31. Forget: local delete completed, crash before PostgreSQL finalization, then
    `STORAGE_BACKEND` switched to `s3` → startup does **not** upload the deleted
    bytes, does **not** classify the missing object as ordinary corruption (D19,
    D34 Case B); the existing destructive `PipelineRun` resumes and completes
    through the unmodified engine while the process is in STORAGE_CONVERGING (D31).
32. Administrative `DATASET_DELETE` equivalent of test 31 (D28's generalization).
33. A live, migration-eligible (`status IN (PENDING, PROCESSING, ACTIVE, FAILED)`)
    `file://` Source with a missing local object still blocks convergence as an
    integrity failure (D34 Case A is unaffected by B2's carve-out); a `DELETED`
    Source with a missing or retained artifact is never subject to this check at all
    (D34 Case C, D40) — distinct from and not merely a variant of this case.
34. Post-CAS `s3://` Source with a matching deterministic legacy local duplicate,
    located via D35's contract: the exact duplicate is removed.
35. Post-CAS `s3://` Source whose D35-derived legacy local path contains the wrong
    content hash: left untouched, fails closed, surfaced in diagnostics.
36. A Source with an unknown/unmappable `mime_type`: no `glob`, no guess, no
    deletion; convergence fails closed (D35).
37. `_ingress/`, `_system/`, and unrelated `DATA_DIRECTORY` content survive every
    cleanup case above, including the B1/B2/M2 paths (D24, D35).
38. Maintenance HTTP surface (`/health/live`) remains reachable and healthy while a
    deliberately long (test-injected) storage convergence operation is still running
    (D33).
39. `/health/ready` remains `NOT_READY` throughout BOOTSTRAP/MAINTENANCE and
    STORAGE_CONVERGING, becoming ready only in OPERATIONAL (D20, D31).
40. Normal business API requests and normal (non-recovery-owned) pipeline claims do
    not execute while storage convergence is incomplete (D31).
41. A recovery-owned Forget/`DATASET_DELETE` run makes progress during
    STORAGE_CONVERGING without enabling any unrelated normal work — asserted by
    submitting a normal Remember concurrently and confirming it stays `queued`,
    unclaimed, until OPERATIONAL (D31).
42. Schema mismatch or an empty/uninitialized schema never triggers automatic
    Alembic invocation or any Source-storage inspection/migration attempt (D32).
43. A detected external mutation inside the managed S3 Source namespace (an object
    at a Sofias-Memory-owned key whose content does not match the integrity metadata
    Sofias Memory itself wrote) is treated as a configuration/integrity violation
    that fails closed wherever it breaks a deterministic-identity assumption (D19,
    D36) — not silently trusted or overwritten.

**Amendment — added by the deletion-semantics review (D37–D41):**

44. filesystem original already absent → business delete succeeds,
    `StorageDeleteResult = ALREADY_ABSENT`, `storage_uri` cleared (D37, D39).
45. S3 original already absent → same outcome via the S3 adapter (D37, D38).
46. S3 configuration missing/inaccessible for an existing `s3://` Source → full
    Forget succeeds, Source reaches `DELETED`, `storage_uri` preserved,
    `storage_unresolved` incremented (D37, D39, D41's worked example).
47. Administrative Dataset DELETE containing a mix of successfully-deleted `file://`,
    already-absent `file://`, and inaccessible `s3://` Sources reaches `DELETED`
    while preserving only the unresolved Sources' `storage_uri` values, clearing the
    rest (D37, D39).
48. A versioned S3 object fully purged → `DELETED_NOW` + `storage_uri` cleared (D38).
49. A versioned S3 object blocked by retention/Object Lock → `UNRESOLVED` + business
    delete still succeeds + `storage_uri` preserved (D37, D38).
50. A timeout/`AccessDenied` outcome is typed as a recognized storage outcome →
    `UNRESOLVED`, no worker/run poisoning, no unbounded retry loop (D37).
51. An unexpected adapter/programming exception still fails the `PipelineStep`
    normally, through ADR-0009's existing retryable/permanent classification — never
    silently absorbed into `UNRESOLVED` (D37's "expected failure vs. software
    defect" rule).
52. A `DELETED` Source with a retained `file://` URI is never selected by the
    filesystem→S3 startup convergence scan (D40, D34 Case C addendum).
53. A `DELETED` Source with a retained `s3://` URI is never treated as a live Source
    by Cognify rehydration or Remember's `storage_uri = NULL` recovery path (D13,
    D12/B1, D40) — both require an authoritative, non-`DELETED` Source.
54. Run metrics never count an `UNRESOLVED` outcome as `storage_deleted` or
    `storage_already_absent` (D39, D41).

**Amendment — added by the contract-completeness review:**

55. Full Source Forget: the storage step's output accidentally lacks a
    `StorageDeleteResult` for the target `source_id` → the finalizer fails as an
    internal invariant violation; it MUST NOT silently interpret the missing result
    as `UNRESOLVED` (D37's per-Source coverage requirement).
56. Administrative Dataset DELETE with N `DELETING` Sources must produce N explicit
    `StorageDeleteResult`s before `finalize_tombstone` may finalize those N Sources
    (D37).
57. Full deletion of a Source whose `storage_uri` is already `NULL` before the
    storage step runs produces an explicit `NOT_REQUESTED` outcome for that
    `source_id` and finalizes normally (D37's `NOT_REQUESTED` clarification) —
    **and** asserts D39's corrected semantic: the resulting `DELETED`+`NULL` state is
    reached via `NOT_REQUESTED`, not via `DELETED_NOW`/`ALREADY_ABSENT`, and a test
    reading only `Source.status`/`storage_uri` after the fact must not be able to
    distinguish this case from a `DELETED_NOW`/`ALREADY_ABSENT` case by tombstone
    state alone — only the run's own durable per-Source `StorageDeleteResult` output
    (D37) tells them apart.
58. `DELETING` + `file://` + missing local object + **no** compatible durable
    Forget/`DATASET_DELETE` `PipelineRun` lineage → fails closed; the Source is never
    migrated and never classified recovery-owned (D34 Case D).

**Amendment — added by the STORAGE-006 CAS-loss safety amendment (D43):**

59. During STORAGE_CONVERGING, a live Case-A Source (`status IN (PENDING,
    PROCESSING, ACTIVE, FAILED)` + `storage_uri = file://...`) cannot enter a *new*
    destructive authoritative transition — no normal `PipelineRun` claim capable of
    doing so may begin (D31, D43).
60. A normal Forget/`DATASET_DELETE` submission targeting a live Case-A Source,
    queued during STORAGE_CONVERGING, remains blocked and unclaimed until OPERATIONAL
    (D31, D43) — it is never claimed merely because its target happens to also be a
    migration candidate.
61. A recovery-owned destructive run whose Source is already `DELETING` with proven
    compatible lineage (D34 Case B) may still be claimed and progressed during
    STORAGE_CONVERGING, unchanged (D31, D43 does not narrow this allowance).
62. Crash window: S3 `PUT` succeeds and is strongly verified, then the process
    crashes before migration's CAS commits. On restart, STORAGE_CONVERGING runs
    again before any normal destructive work may begin; the Source is rediscovered as
    Case A, the deterministic S3 target is reused idempotently, and CAS commits
    normally (D9, D43) — no migration-intent ledger is needed for this window.
63. CAS loss to another *live* migration-eligible status (e.g. `ACTIVE` → `FAILED`)
    remains self-healing: the target stays discoverable and is adopted on this or a
    future pass (D8, D10, D43 — unchanged from STORAGE-006).
64. CAS loss where the Source is observed `DELETING` is, under D43, an internal
    lifecycle invariant violation in a correctly-supported single-replica deployment
    — it must fail closed and be surfaced as an integrity condition, never silently
    classified as ordinary CAS contention (D43).
65. Two independent application processes, one `OPERATIONAL` and one concurrently
    `STORAGE_CONVERGING` against the same durable state, is explicitly outside
    supported MVP deployment (D43's single-replica boundary) — this ADR neither
    guarantees nor tests safety for that configuration.
66. No PostgreSQL transaction/row lock spans S3/filesystem I/O anywhere in D43's
    exclusion mechanism (it adds a claim-time precondition, not a lock), and no new
    migration-intent ledger, schema, table, or column is introduced by D43 (D10,
    D27, D43).

**Amendment — added by the D43 recovery-owned claim consistency fix (sixth
amendment):**

67. `DATASET_DELETE` run `R` targets Dataset `D` containing Source `A` (`DELETING` +
    `file://` + missing, Case B) and Source `B` (`ACTIVE` + `file://`, Case A); `R`'s
    own `deactivate_authoritative` step has **not** yet durably succeeded → `R` is
    NOT claim-eligible as recovery-owned merely because `D`'s scope contains Case-B
    Source `A`; Source `B` remains classified Case A; no `ACTIVE` → `DELETING`
    transition occurs for `B` during STORAGE_CONVERGING; any claim of `R` (or a fresh
    `DATASET_DELETE` targeting `D`) remains blocked until OPERATIONAL (D31 sixth
    amendment, D43).
68. Same `R`/`D` as item 67, but `R`'s `deactivate_authoritative` step **has** durably
    succeeded for `D`'s complete scope → `R` IS claim-eligible as recovery-owned, and
    resuming it through its existing remaining steps is permitted, unchanged from
    D31's original allowance (D31 sixth amendment).
69. No recovery-owned claim can ever execute an `authoritative_mutation`/
    `deactivate_authoritative` step that creates a *new* `DELETING` Source during
    STORAGE_CONVERGING — proven structurally (not merely tested) by D31's sixth-
    amendment predicate: a run is claim-eligible only once that step has already
    durably succeeded for its complete scope, and that step is the *only* code path
    that ever sets `Source.status = DELETING` (D31, D43).

## Alternatives rejected

**A. Replace filesystem entirely with S3.** Rejected: breaks existing installs and
local development, and ignores legitimate local/self-hosted use that has no reason to
require external object storage.

**B. Make `/data/sources` unnecessary in S3 mode.** Rejected: the volume is the
persistent application-data root (D1), not merely "where Source originals happen to
live" — it remains required for durable ingress (D3) and future application state.

**C. Put durable ingress in S3.** Rejected for this ADR: unnecessary while the
persistent volume remains mandatory; would increase remote-storage coupling on every
Remember request's staging phase without improving the actual problem this ADR solves
(finalized Source-object durability).

**D. Require a manual `storage migrate` command after every redeploy.** Rejected as
the *normal* path (D7, D29, D30): configuration should be declarative, and startup
should converge automatically; a manual step an operator can forget to run is not an
acceptable single point of failure for a correctness-sensitive migration.

**E. Auto-migrate S3 → filesystem.** Rejected (D5, D25): not needed for the initial
feature, and materially expands relocation semantics (a second, symmetric
crash-safety/verification design) for no product requirement driving it today.

**F. Generic storage plugins / optional dependencies.** Rejected (D17): conflicts
directly with ADR-0005's fixed, versioned, supported-stack philosophy; a two-backend
closed set is not a plugin ecosystem.

**G. Store S3 endpoint/credentials inside `storage_uri`.** Rejected (D6, D16):
`storage_uri` is durable object identity persisted indefinitely in PostgreSQL and
surfaced in logs/diagnostics; credentials/endpoint are mutable operational
configuration that must never leak into a durable, potentially-long-lived value.

**H. Use `ETag` as the content SHA-256.** Rejected (D11): not universally equivalent
to SHA-256, especially for multipart uploads, some encryption modes, and
provider-specific behavior — would silently weaken the one integrity guarantee this
whole migration algorithm depends on.

**I. Hold a PostgreSQL lock across the entire migration.** Rejected (D10): violates
the project's existing external-I/O locking discipline (ADR-0009 §D) and is
unnecessary given a deterministic/idempotent/CAS design that is already correct under
concurrency without one.

**J. Update PostgreSQL to `s3://` before verifying the S3 object.** Rejected (D8 steps
G-H): could leave the authoritative Source pointer referencing unproven — possibly
corrupt or partial — storage, violating ADR-0002's authority ordering.

**K. Delete the local original before committing `s3://`.** Rejected (D8 "Ordering is
fixed"): creates a crash window where PostgreSQL still points at a file that no longer
exists — the exact hazard D9 exists to prevent by construction rather than detect
after the fact.

**L. Bulk-delete the old `DATA_DIRECTORY` contents after migration completes.**
Rejected (D1, D9, D24): `DATA_DIRECTORY` is persistent application state, not a
disposable Source-only volume; only individually proven, per-Source duplicate files
may ever be removed.

**M. Let the generic startup storage-convergence scanner recover `storage_uri = NULL`
Remember crash windows itself (treating every `NULL` row as a migration-adjacent
candidate).** Rejected (added by this amendment, D7/D12/B1): this would make the
startup scanner a second, competing implementation of `FinalizeStorageStep`'s own
idempotent-resume contract — it would need to reconstruct which ingress run produced
which Source, re-derive the same legacy-object recovery logic Remember's own retry
already owns, and risk racing an actual concurrent Remember retry attempting the same
recovery through the real pipeline engine. Ownership stays with the existing
`PipelineRun`/step-retry machinery (ADR-0009); the startup scanner's only obligation
is to leave `NULL`-`storage_uri` rows alone entirely.

**N. Require bucket-wide exclusive ownership of the configured S3 bucket.** Rejected
(D36): the correctness guarantees this ADR depends on only require the managed
`STORAGE_S3_PREFIX` namespace to be write-exclusive to Sofias Memory; requiring the
entire bucket would needlessly block legitimate use of the same bucket for other
prefixes/applications with no corresponding safety benefit.

**O. Require successful physical Source-storage deletion before business deletion can
finalize (the original D15/ADR-0010 assumption).** Rejected (D37, superseding that
assumption): would let a temporarily inaccessible S3 backend permanently block a
user-requested Forget/`DATASET_DELETE` indefinitely, even though PostgreSQL/Neo4j
authoritative memory deletion has no technical dependency on the original artifact's
physical fate — the artifact is provenance, not memory authority (D37's core
decision). Blocking indefinitely also has no clean recovery path other than "wait for
S3 to come back," which is worse for the product than recording an explicit,
observable `UNRESOLVED` outcome and letting the operator decide.

**P. Silently treat an inaccessible backend as equivalent to "already absent."**
Rejected (D37): this would violate the positive-evidence requirement D15/D38 already
established for `DELETED_NOW` and would make `ALREADY_ABSENT` an unreliable signal —
exactly the "cannot access the backend must never become object already absent"
invariant D37 freezes explicitly.

**Q. Add a new PostgreSQL column (e.g. `storage_cleanup_status`) to represent
unresolved cleanup debt.** Rejected for this increment (D39): the existing
combination `Source.status = DELETED` + `Source.storage_uri != NULL` already
represents the fact unambiguously, requires no migration, and is consistent with
D26's principle of not adding a column without an otherwise-unsatisfiable invariant. A
future explicit maintenance/cleanup feature may still introduce dedicated schema if
its own requirements demand it — out of scope here.

**R. Add a fifth `StorageDeleteResult` value (e.g. `MISSING`/`NOT_RECORDED`) for a
Source with no per-Source result.** Rejected (D37's per-Source coverage amendment):
"no result was recorded" is not a storage outcome — it is evidence of a programming
or bookkeeping defect, which D37 already requires to be a genuine finalize/step
failure, not a typed successful outcome. Adding a fifth enum value would blur exactly
the distinction (recognized operational condition vs. software defect) D37 exists to
keep sharp.

**S. Infer destructive-pipeline ownership from `Source.status = DELETING` alone,
without proving a compatible `PipelineRun` lineage.** Rejected (D34's amended Case B
classifier): this is the same category of mistake ADR-0010 D28 already identified and
rejected for its own `administratively_deleting(D)` predicate (mere historical
existence of a row is not sufficient evidence of active ownership) — applying it
here without the lineage-proof condition would let startup convergence silently
trust a `DELETING` status that has no traceable owner, masking exactly the kind of
data-integrity problem Case D exists to surface instead.

## Consequences

### Positive

- Existing filesystem installations are entirely unaffected until an operator
  explicitly opts into `STORAGE_BACKEND=s3` — zero behavior change to today's
  `Settings`, `Source` schema, or any pipeline step under the default configuration.
- Adding S3 requires zero PostgreSQL migration (D26) and zero new advisory lock/queue
  primitive (D10) — the entire feature is additive at the infrastructure-adapter layer
  plus one new bootstrap gate.
- The write/read-delete routing split (D4, D5) is the single mechanism that makes
  fresh installs, existing-install migration, mixed-URI mid-migration state, and crash
  recovery all fall out of the same small set of rules, rather than needing
  special-cased handling for each.
- ADR-0009's and ADR-0010's existing recovery/idempotency/cancellation machinery is
  reused unchanged (D27, D28) — this feature adds no second engine, no second
  synchronous path, and no new `CancellationRecoveryMode` value.

### Negative / trade-offs

- Operators who enable S3 on an existing installation accept a startup delay
  proportional to the number of legacy `file://` Sources on first boot with
  `STORAGE_BACKEND=s3` — convergence must complete before the worker resumes normal
  operation (D7). This is deliberate (fail-closed correctness over availability) but
  is a real operational cost for a large existing Source set.
- A Source that fails migration validation (D8 D, missing file or hash mismatch) blocks
  overall convergence and therefore readiness until an operator resolves it — there is
  no "skip and continue" partial-convergence mode in this ADR; a future
  operational-CLI addition (D30) may ease diagnosis, but does not change this fail-closed
  posture.
- Two backends must now both be kept correct, tested, and documented indefinitely (D17)
  — a permanent, if narrow, increase in surface area relative to the single-backend
  status quo.
- **(added by this amendment; narrowed by the later deletion-semantics amendment,
  D37)** STORAGE_CONVERGING's duration is not bounded solely by S3
  upload/verification time — it also depends on any recovery-owned
  Forget/`DATASET_DELETE` lineage (D31) reaching its own legitimate durable terminal
  state. Since D37, this is a materially smaller cost than the original D31 text
  implied: a recovery-owned run whose only obstacle is unprovable physical storage
  state now converges to `succeeded` immediately (with `UNRESOLVED` metrics, D39)
  rather than looping on retry or sitting `failed` awaiting manual intervention — the
  remaining, genuinely blocking case is limited to an actual crashed/interrupted
  attempt still under ADR-0009 §I reconciliation, or a true `PipelineStep` defect
  (D37). Time-to-OPERATIONAL on a `STORAGE_BACKEND=s3` upgrade is therefore extended
  only by that narrower set of cases, never by ordinary S3-inaccessibility alone.
- The BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING → OPERATIONAL state model (D31) adds
  a third observable process state beyond today's simple live/ready pair — a modest
  increase in operational/observability surface that D33 requires to be exposed
  through logs/metrics, not hidden behind a single readiness boolean.
- `SourceObjectStorage`/`SourceStorageRouter` is a new architectural boundary that
  every future Source-storage-touching code path must go through instead of resolving
  a `Path` directly — a minor discipline cost for contributors, offset by removing the
  current duplication of containment-check logic across `remember.py`, `forget.py`,
  and `cognify.py`.

## Architecture review recommendation

**Amendment note (updated after three review rounds):** this section originally
recommended proceeding straight to implementation planning. That recommendation was
superseded first by the initial architecture review (2 blockers, 2 major
clarifications, 1 required invariant — addressed in D31–D36 and the targeted
corrections to D6–D9, D12, D19, D20, D27–D29, the state machine, and the
crash-window/validation tables), then by a second, deliberate product/architecture
clarification round that changed the Source-original deletion contract itself
(D37–D42: the `StorageDeleteResult` four-outcome contract, business-delete-must-
converge, tombstone `storage_uri` retention semantics, the `DELETED`-exclusion
correction to the migration candidate set, and the worked rollback/lost-configuration
example), and now by a third, narrow contract-completeness pass that closed four
remaining precision gaps in the second round's own text: explicit per-Source
`StorageDeleteResult` coverage (D37), correcting D15's pre-D37 wording that had not
been updated to match D37's own supersession, reconciling D31's OPERATIONAL condition
with its own already-stated terminal-run allowance, and requiring D34's Case B to
prove destructive-pipeline lineage rather than infer it from `Source.status` alone.
This ADR's `Status` remains `proposed`; it does not recommend itself into `accepted`
— that determination is made by the next review cycle, against the fully amended text
above, and is reported separately from this document rather than asserted here.

The design, as amended through all three rounds, closes every correctness-critical
question the original task listed as non-negotiable, plus every gap identified by all
three review rounds: ingress location, persistent-volume requirement, URI schemes,
write-vs-read/delete routing, automatic convergence, migration ordering, crash safety
(including the B1/B2/M2 backend-transition windows), local cleanup rules (including
the D35 post-CAS locator), versioned-bucket deletion semantics under the four-outcome
result contract, no-lock-across-I/O, no automatic reverse migration, the
BOOTSTRAP/MAINTENANCE/STORAGE_CONVERGING/OPERATIONAL process state model (now
internally consistent between its own OPERATIONAL definition and its terminal-run
allowance), Alembic's strict precedence, recovery-owned destructive work's bounded
exception to "normal claims blocked" (now gated on *proven* lineage, D34), managed S3
namespace exclusivity, the authoritative-memory-vs-provenance-artifact distinction,
unresolved-cleanup tombstone semantics, and source-for-source-complete storage result
coverage with no silent-`UNRESOLVED`-on-missing-evidence gap — all using only
mechanisms this codebase already trusts elsewhere (deterministic keys, PostgreSQL CAS,
`AMBIGUOUS`/idempotent external-effect classification, fail-closed startup gating, the
unmodified ADR-0009 claim/retry engine, typed step-output results in place of
exceptions for recognized conditions, ADR-0010 D28's own proven-ownership discipline
reapplied to a second classifier). No new class of infrastructure (coordinator table,
distributed lock, second queue, second pipeline engine, new PostgreSQL column, fifth
`StorageDeleteResult` value) is introduced by the original design or any of the three
amendment rounds. The remaining open questions (listed above under "Unresolved
implementation-only questions") are genuinely implementation-level and appropriately
deferred.
