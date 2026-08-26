# ADR-0010: Administrative Dataset Deletion Contract

## Status

Proposed

## Context

`AGENTS.md` §26 and the B5 backlog (§5.15) reserve `DELETE /api/v1/datasets/{dataset_id}`
for SM-515, and ADR-0009 explicitly declines to resolve it
(`docs/adr/0009-worker-queue-and-pipeline-lifecycle-contract.md:37-38`: "Administrative
`DELETE /api/v1/datasets/{dataset_id}` is explicitly **not** resolved here. It gets its
own `ADR-0010` before SM-515."). This ADR is that gate.

Today there is no dataset-administrative-delete concept in the codebase at all:

- `sofias_memory/api/routes/datasets.py` exposes `POST`, `GET` (list/by-id/sources/stats),
  `PATCH` — no `DELETE` route exists.
- `Dataset` (`infrastructure/postgres/models/dataset.py:40-67`) has exactly `id, name,
  slug, description, status, active_generation, created_at, updated_at`. No
  `deleted_at`, no soft-delete timestamp, no owner/tombstone marker of any kind.
- `DatasetStatus` (`domain/enums.py:8-13`) already defines `ACTIVE, DELETING, DELETED`
  as a native Postgres enum (`migrations/versions/0002_create_datasets.py:21-27`), but
  **`DELETED` has zero write sites in the entire codebase today** — only `ACTIVE` and
  `DELETING` are ever assigned, and both writes belong to `POST /api/v1/forget`
  (`services/datasets.py:99` on create; `pipelines/steps/forget.py:296` and `:597-598`
  for Forget's own dataset-scope mutation/finalize).
- `POST /api/v1/forget` with `scope=dataset` or `scope=everything` is the only existing
  operation that touches `Dataset.status`, and it is explicitly **content deletion**,
  not namespace deletion: `pipelines/steps/forget.py:597-598` unconditionally resets
  `DatasetStatus.DELETING → ACTIVE` at finalize, for both scopes. The `Dataset` row is
  never removed and always ends `ACTIVE`.
- Every content table's FK to `datasets.id` (`Source`, `Document`, `Chunk`, `Entity`,
  `Relation`, `Summary`, `MemoryEntry`) is declared `ondelete="RESTRICT"`. PostgreSQL
  itself refuses a hard `DELETE FROM datasets` while any such row still references it,
  independent of that row's own `status`/`is_active` value. `PipelineRun.dataset_id` is
  the sole exception (`ondelete="SET NULL"`, nullable), and `GraphOutbox.dataset_id`
  carries no FK at all (plain `UUID` column, an append-only ledger by design).
- `Dataset.name`/`Dataset.slug` uniqueness (`migrations/versions/0002_create_datasets.py:70-71`)
  is a plain, unconditional `UniqueConstraint` — not partial, not scoped by status. A
  name/slug can never be reused while the owning row still physically exists.
- The dataset-scoped operational-run mutual-exclusion mechanism already exists and is
  final: the partial unique index `uq_pipeline_runs_dataset_id_operational`
  (`migrations/versions/0010_pipeline_runs_operational_unique_constraint.py:29-35`, on
  `pipeline_runs(dataset_id)` where `status IN ('running','cancelling')`) plus
  `PipelineRunClaimer`'s advisory-lock arbitration
  (`services/pipeline_queue_claimer.py:127-192`: `dataset_lock_key(dataset_id)` /
  `GLOBAL_BARRIER_KEY`). This ADR does not touch either.
- `PipelineType` (`domain/enums.py:53-59`) is a native Postgres `ENUM`
  (`infrastructure/postgres/models/pipeline_run.py:27-33`, `create_type=False`), with
  exactly four values: `remember, cognify, improve, forget`.
- Cancel/retry (SM-514, `3ff5674`) is fully general over `PipelineRunStatus` and
  `pipeline_type`; it makes no pipeline-type-specific assumption that a fifth type
  would break, but it also does not yet know anything about dataset-delete-specific
  interactions (delete-intent barrier, tombstone terminality). Those interactions are
  this ADR's job to freeze.

This ADR complements ADR-0009 and does not reopen queue claiming, heartbeat, automatic
retry, the generic cancellation lifecycle, `PipelineStep` lifecycle, the global
barrier, or `graph_outbox` identity — all of those are inherited as-is.

## Decision

### D1. `DELETE /api/v1/datasets/{dataset_id}` is not Forget

`POST /api/v1/forget` (any scope) and `DELETE /api/v1/datasets/{dataset_id}` are
different operations with different terminal semantics:

| | Forget (existing) | Administrative Delete (this ADR) |
|---|---|---|
| Removes | memory/content per scope | the namespace itself |
| Dataset after success | `ACTIVE` (unconditionally reset, `pipelines/steps/forget.py:597-598`) | `DELETED` (terminal, never reset) |
| Namespace (`name`/`slug`) | remains usable | reserved forever by the tombstone |
| `PipelineType` | `forget` | `dataset_delete` (new) |

Administrative delete **may** reuse Forget's low-level, already-audited primitives
(safe storage-path resolution, `graph_outbox` DELETE-command emission, per-artifact
deactivation queries) as internal helpers. It **must not** reuse Forget's public
lifecycle/finalizer — specifically, it must never call or replicate
`_finalize_dataset_target` (`pipelines/steps/forget.py:561-604`), because that
function's entire purpose is resetting `DatasetStatus.DELETING → ACTIVE`, the exact
outcome administrative delete must never produce (D9 addresses the converse hazard:
Forget must not touch an administratively-owned `DELETING`/`DELETED` dataset either).

### D2. Identity: `PipelineType.DATASET_DELETE`

Freeze `PipelineType.DATASET_DELETE = "dataset_delete"`. Dataset-scoped only
(`dataset_id != NULL`); never global. Never encoded as Forget metadata. SM-515 adds
this Python enum member and a matching `ALTER TYPE pipeline_type ADD VALUE
'dataset_delete'` migration (native Postgres enum, confirmed by
`infrastructure/postgres/models/pipeline_run.py:27-33`; `ALTER TYPE ... ADD VALUE`
cannot be combined in the same transaction as statements that use the new value on
older PostgreSQL — SM-515's migration must account for this, e.g. by not using the new
value in the same migration that adds it). No migration is authored in this ADR.

### D3. `main` is administratively indeletable

`DELETE` on the dataset identified by `slug == "main"` is rejected: `409 Conflict`,
`code: MAIN_DATASET_DELETE_FORBIDDEN`. `main` keeps receiving Remember / Cognify /
Improve / Forget / Forget-content normally.

The guard checks the **authoritative row's slug** (`Dataset.slug`, fetched by the
resolved `dataset_id`), not the raw path parameter — a dataset can only be identified
as `main` by its persisted slug, the same field `services/datasets.py:38,90-91`
(`DEFAULT_DATASET_SLUG`, `reserved_main_error`) already uses to reserve the name at
creation. There is no separate `is_main`/`is_default` column and this ADR does not add
one; `main` is, and remains, identified by convention (its reserved slug), consistent
with how it is created lazily today (`repositories/datasets.py:62-93`,
`get_or_create_by_slug`).

### D4. Tombstone: the `Dataset` row is never physically removed

Terminal state is `Dataset.status = DELETED`. The row itself, with its identity
fields (`id, name, slug, description, created_at, updated_at`, historical
`active_generation`), is preserved permanently. This is not merely a policy
preference — it is **structurally forced** by D1's audit: every content table's FK to
`datasets.id` is `RESTRICT` (D1 table above), so a hard `DELETE FROM datasets` would
fail with a foreign-key violation while any Source/Document/Chunk/Entity/Relation/
Summary/MemoryEntry row still exists for it, which — per D10/D11 below — they always
will, since this ADR does not hard-delete those rows either. Changing every one of
those FK policies to `CASCADE` to make hard deletion possible is out of scope (§45) and
would itself destroy the audit trail this ADR exists to preserve. No "undelete" is
implemented; `DELETED` is terminal for the administrative lifecycle.

### D5. `name`/`slug` reuse: forbidden in v1

A `DELETED` tombstone's `name` and `slug` remain reserved. `POST /api/v1/datasets`
with either value → `409`. This is not just a policy choice: it is the **current,
already-enforced schema behavior** (D4's `UniqueConstraint`s are global and
unconditional, `migrations/versions/0002_create_datasets.py:70-71`), so honoring it
requires zero migration. No suffixing (`deleted-<uuid>`), no tombstone rename, no
partial-unique-index-by-status introduced to enable reuse. A future product decision
to allow namespace reuse needs its own ADR.

### D6. Read semantics of the tombstone

- `GET /datasets/{id}` → `200`, `DatasetResult` with `status: DELETED`. Never `404` —
  the row exists precisely for audit.
- `GET /datasets` → tombstones are included in normal pagination (no filtering added).
- `PATCH /datasets/{id}` → rejected for `status in (DELETING, DELETED)` (extends the
  existing guard at `services/datasets.py:142`, which already rejects rename for any
  non-`ACTIVE` status — no new logic required, just confirming the existing check
  already produces the right outcome here).
- `GET /datasets/{id}/sources`, `GET /datasets/{id}/stats` → remain available for
  `DELETING`/`DELETED` as administrative/audit surfaces, reflecting the persisted,
  inactive historical state. They must not be read as "current active memory" — see D14.
- Recall/Graph reads scoped to a `DELETING`/`DELETED` dataset do not treat its
  artifacts as authoritative active memory (D14).

### D7. What "empty dataset" means

"Empty" is **not** "zero `Source` rows." A dataset can have tombstoned Sources,
Documents, Chunks, Entities, Relations, Summaries, mentions/evidence, MemoryEntries,
Feedback references, `graph_outbox` history, and storage files while having no
*active, destructible* content. Administrative delete's job is to make nothing
authoritative/consultable and remove original storage — it is not obligated to purge
historical/audit rows to make some row-count reach zero. This ADR does not define
"empty" as a precondition or shortcut at all: see D8.

### D8. One lifecycle for empty and non-empty datasets

Every administrative delete — including an apparently empty dataset — runs through the
same durable B5 `PipelineRun`/`DATASET_DELETE` lifecycle, the same fixed
`PipelineDefinition`, the same worker. There is no second, synchronous "delete
immediately if empty" code path in the route.

Reasons this is not just consistency for its own sake:

- FR-100 requires every write to carry a `run_id`.
- "Empty" can stop being true between the `SELECT` that checked it and the `DELETE`
  that acted on it (D19's empty-dataset race) — a concurrent `Remember` can land in
  that gap. A single durable pipeline, serialized through the existing
  dataset-operational-run mechanism, closes this race by construction; a bespoke
  synchronous check-then-delete cannot.
- Cancel/retry/audit stay uniform across every dataset regardless of size.

`begin_delete` (D12 step 1) may find nothing to deactivate and its later steps become
deterministic no-ops — that is a property of the step implementations, not a second
code path.

### D9. Fixed pipeline: phases and safe points

Five fixed steps, `CancellationRecoveryMode` classified per ADR-0009 §I / SM-507
(`pipelines/registry.py:106-133`: `ATOMIC`, `RECONCILABLE`, `AMBIGUOUS` — default
`AMBIGUOUS`, fail-safe):

| # | Step | Boundary | Recovery mode |
|---|---|---|---|
| 1 | `begin_delete` | PostgreSQL-only, ATOMIC | `ATOMIC` |
| 2 | `deactivate_authoritative` | PostgreSQL-only, ATOMIC | `ATOMIC` |
| 3 | `converge_projection` | external (Neo4j via outbox), replay-safe | `RECONCILABLE` (backed solely by `graph_outbox` durable state, same shape as Forget's `ProjectionConvergenceStep`, `pipelines/steps/forget.py:326-328`) |
| 4 | `delete_storage` | external (filesystem), idempotent | `AMBIGUOUS` (same justification as Forget's `StorageDeletionStep`, `pipelines/steps/forget.py:373-381`: a PostgreSQL-only reconciliation callback cannot prove an orphaned attempt did or did not already unlink the file before crashing) |
| 5 | `finalize_tombstone` | PostgreSQL-only, ATOMIC | `ATOMIC` |

1. **`begin_delete`** (PostgreSQL-only/ATOMIC): in one short transaction —
   `Dataset.status: ACTIVE → DELETING`; freeze the administrative intent; administratively
   cancel incompatible still-`QUEUED` runs for the dataset (D16). This is the **only**
   step that ever writes `Dataset.status` to `DELETING`, and it does so only once the
   run already holds the dataset's operational slot (D14/D15).
2. **`deactivate_authoritative`** (PostgreSQL-only/ATOMIC): remove active authority —
   Source lifecycle, Documents/Chunks/Entities/Relations/Summaries deactivation,
   enqueue `graph_outbox` DELETE commands, in the same transactional pattern Forget's
   `AuthoritativeMutationStep` already uses. Zero filesystem, zero Neo4j I/O here.
3. **`converge_projection`** (external/replay-safe): drain the `graph_outbox` DELETE
   commands this run enqueued, same SM-506 leasing/fencing Forget already uses
   (`pipelines/steps/forget.py:326-328`). No global Neo4j wipe.
4. **`delete_storage`** (external/idempotent): remove original files for every Source
   in scope, reusing the audited path guards (D24). Already-absent is replay-safe.
   No PostgreSQL lock held around filesystem I/O.
5. **`finalize_tombstone`** (PostgreSQL-only/ATOMIC): `Dataset.status: DELETING →
   DELETED`; final safe counters/metrics; storage references cleared only after step 4
   durably confirmed.

No nested Forget `PipelineRun` is created at any point.

### D10. Destructive ordering

`begin_delete` → `deactivate_authoritative` (+ enqueue outbox) → `converge_projection`
→ `delete_storage` → `finalize_tombstone`. Storage is never deleted first. Neo4j is
never authoritative. `Dataset.status` never reaches `DELETED` before every prior step
has durably committed. `DELETED` means "administrative deletion converged according to
this contract," not "a delete was requested."

### D11. Retention / FK matrix (audited against current schema)

| Table | `dataset_id` FK today | Row retained? | Terminal marking by `deactivate_authoritative` | Physically deleted? | Why |
|---|---|---|---|---|---|
| `Dataset` | — (root) | Yes, forever | `status = DELETING` then `DELETED` | Never | Tombstone; every child FK is `RESTRICT` (D4) |
| `Source` | `RESTRICT`, not null | Yes | `SourceStatus.DELETED` (existing enum value, same as Forget uses) | Never | Existing lifecycle already models this terminal state |
| `Document` | `RESTRICT`, not null | Yes | `is_active = False` | Never | Same mechanism Forget already uses (`is_active`, no `status` column exists on this table) |
| `Chunk` | `RESTRICT`, not null | Yes | `is_active = False` | Never | Same as `Document` |
| `Entity` | `RESTRICT`, not null | Yes | `is_active = False` | Never | Same as `Document`; frees the partial-unique `(dataset_id, canonical_key)` slot for a hypothetical future dataset — moot since the namespace is reserved (D5) |
| `Relation` | `RESTRICT`, not null | Yes | `is_active = False` | Never | Same as `Document` |
| `Summary` | `RESTRICT`, not null | Yes | `is_active = False` | Never | Same as `Document` |
| `EntityMention` | none (via `entity_id`/`chunk_id`, both `CASCADE`) | Yes | none (no lifecycle column exists) | Never (would only cascade if the parent `Entity`/`Chunk` were hard-deleted, which never happens) | Historical evidence row; inert once its parent is inactive |
| `RelationEvidence` | none (via `relation_id` `CASCADE` / `chunk_id` `RESTRICT`) | Yes | none | Never | Same as `EntityMention` |
| `MemoryEntry` | `RESTRICT`, not null | Yes | none (no lifecycle column exists today) | Never | No terminal-state column exists; ceases to be reachable through any active-scope query once the owning Dataset is `DELETING`/`DELETED` (D14) |
| `Feedback` | none (dataset-scoped only via loose `target_id`, no FK) | Yes | none | Never | Not dataset-FK-scoped at all; out of scope |
| `PipelineRun` | `SET NULL`, nullable | Yes (`SET NULL` never exercised — Dataset row is never hard-deleted) | none | Never | Run history, including this ADR's own delete lineage, must survive |
| `PipelineStep` | none (via `run_id`, `CASCADE`) | Yes | none | Never | Same reasoning as `PipelineRun` |
| `GraphOutbox` | none (plain `UUID`, no FK) | Yes | terminal `DONE`/`FAILED` per normal outbox lifecycle | Never | Append-only ledger by design (SM-506); already survives dataset content changes today |

"Remove content" means: nothing above is authoritative or consultable as active
memory, and no original storage file remains. It does **not** mean hard-deleting
history rows. `MemoryEntry` is flagged as the one table with no existing lifecycle
column — D26 requires `deactivate_authoritative` to exclude `DELETING`/`DELETED`
datasets from every query surface that treats `MemoryEntry` as active, rather than
inventing a new column for it (no invariant found in this audit that requires one).

### D12. Delete-intent barrier — blocking new incompatible writes

Once a `DATASET_DELETE` run for dataset `D` is durably `QUEUED`/`RUNNING`/`CANCELLING`
(any nonterminal status), no **new** submission incompatible with `D` is accepted:
`Remember`, `Cognify`, `Improve`, `Forget` (dataset or source scope on `D`), manual
retry of a dataset-scoped run on `D`, or a second independent `DATASET_DELETE` on `D`
(D23 handles the latter as idempotent observation, not rejection).

Mechanism, PostgreSQL-authoritative, no new column:

```sql
EXISTS (
  SELECT 1 FROM pipeline_runs
  WHERE dataset_id = :dataset_id
    AND pipeline_type = 'dataset_delete'
    AND status IN ('queued', 'running', 'cancelling')
)
```

The existing partial unique index `uq_pipeline_runs_dataset_id_operational`
(`migrations/versions/0010_...py:29-35`) only covers `running`/`cancelling` — it does
**not** by itself prevent two concurrent submissions (one `Remember`, one `DELETE`)
from both landing `QUEUED` for the same dataset. The barrier check above must
therefore be evaluated inside the same transactionally-safe submission path
`PipelineSubmissionService` already uses for idempotency-race safety (row-level
locking on the dataset scope before insert) — this ADR requires SM-515 to extend that
existing pattern, not invent a second one. No new index is mandated by this ADR;
SM-515 may add one for query performance if profiling shows a need, since the existing
`dataset_id` index already used by the operational-unique constraint makes the
predicate above selective without one.

### D13. A writer already `RUNNING` when delete is requested finishes normally

`Dataset.status` does **not** flip to `DELETING` at submission time. It flips only
inside `begin_delete` (D9 step 1), which only executes once the `DATASET_DELETE` run
itself is claimed and holds the dataset's operational slot
(`services/pipeline_queue_claimer.py:127-192`). A writer already `RUNNING` on `D` when
the delete request arrives is unaffected by ADR-0009's own lifecycle: it finishes
(`SUCCEEDED`/`FAILED`) or is cancelled through the generic SM-514 path exactly as
before this ADR. `DATASET_DELETE` sits `QUEUED` behind it; the delete-intent barrier
(D12) stops new incompatible work from queueing behind the delete in turn, so it does
not starve.

### D14. Existing `QUEUED` runs at `begin_delete` time

Within the **same short PostgreSQL transaction** that marks `Dataset.status =
DELETING`, `begin_delete` administratively cancels every other still-`QUEUED`
incompatible run for `D` (an automatic-retry-scheduled `Remember`/`Cognify`/`Improve`/
`Forget`, or a stale manual retry) using the already-general SM-514 transition:
`QUEUED → CANCELLED`, its own `QUEUED` steps → `CANCELLED`, any already-`SUCCEEDED`
steps left as-is, `next_attempt_at → NULL`, no business execution. No new transition is
introduced — this reuses `RUN_TRANSITIONS`'s existing `(QUEUED, CANCELLED)` entry
(`domain/pipeline_lifecycle.py:52-63`) exactly as `RunControlService.cancel()` already
does for a single run. `RUNNING`/`CANCELLING` runs on `D` are never encountered here —
the operational-unique index and advisory-lock arbitration already guarantee
`DATASET_DELETE` cannot itself be `RUNNING` on `D` while another run on `D` is also
`RUNNING`/`CANCELLING`; if the audit ever finds a code path that violates this, it must
fail loudly (an internal consistency error), never silently reclassify or skip.

### D15. Fairness

New submissions on `D` are blocked as soon as the delete intent is durable (D12).
Runs that were already queued ahead of the delete keep the claimer's existing
eligibility order; once `DATASET_DELETE` obtains the operational slot, remaining
queued incompatible work on `D` is terminated per D14. No priority queue, no
process-local mutex — this is a direct consequence of D12+D14, not a new mechanism.

### D16. Interaction with SM-514 manual retry

A retry of `Remember`/`Cognify`/`Improve`/`Forget` targeting `D` must not create a new
`QUEUED` run if a delete-intent barrier exists for `D`, or `D` is `DELETING`/`DELETED`.
`RunControlService.retry()` must revalidate the dataset before creating a new run and
return a stable conflict (`DATASET_DELETING` / `DATASET_DELETED`, new codes — D28)
instead. This is the one integration point this ADR requires inside the existing
`services/run_control.py` (not implemented here — see "Implementation obligations").

The converse — `DATASET_DELETE`'s own retry lineage continuing while `D` is
`DELETING` — is a **deliberate exception** (D22): only a retry whose lineage traces
back to the administrative delete that put `D` into `DELETING` may continue; retry of
any other pipeline type on a `DELETING`/`DELETED` dataset is rejected.

### D17. Cancel `DATASET_DELETE` while still `QUEUED`

`QUEUED → CANCELLED` (reusing the generic SM-514 cancel path unchanged).
`Dataset.status` was never touched (`begin_delete` never ran) — it remains `ACTIVE`.
No content mutation occurred. The delete-intent barrier disappears the moment the run
reaches `CANCELLED`. New submissions on `D` are accepted again. Any other run that had
already been administratively cancelled per D14 stays cancelled — cancellation of the
delete does not resurrect them (no such requirement exists; SM-514's own manual retry
remains available for any of them independently). A manual retry of this `CANCELLED`
`DATASET_DELETE` run may itself create a new `DATASET_DELETE` via the ordinary SM-514
lineage mechanism.

### D18. Cancel after `Dataset.status = DELETING`

Once `begin_delete` has committed, cancellation is **not** rollback/undelete. If the
run reaches `CANCELLED` at a later safe checkpoint, `Dataset.status` **stays
`DELETING`** — it never transitions `DELETING → ACTIVE` under any circumstances after
this point, because the destructive PostgreSQL step may have already committed and
automatically reconstructing deleted content is not safe. New writes on `D` remain
blocked (D12, unaffected by the run's own terminal status — see D16's `DATASET_DELETING`
conflict). Recovery is explicit: SM-514 manual retry of the `DATASET_DELETE` run,
whose fresh steps are idempotent against partial deletion and converge to `DELETED`.

### D19. Failed delete

Same rule as D18: failure **before** `begin_delete` commits leaves `D` `ACTIVE`;
failure **after** leaves `D` `DELETING`. No generic failure handler reactivates the
Dataset. Manual retry (SM-514) is the only recovery path; ADR-0009's stale recovery
continues to apply unchanged to `DATASET_DELETE` like any other pipeline type.

### D20. Manual retry of `DATASET_DELETE`

Fully participates in SM-514: `FAILED`/`CANCELLED` → new `PipelineRun`,
`retry_of_run_id` set, fresh steps, same `dataset_id`/`input`/`payload_hash`, full
redo, idempotent against partial destructive state (steps 1–5 above are each written
to tolerate resuming from any prior partial state — already-absent storage is
replay-safe, already-`DELETING` Dataset is a no-op re-affirmation in `begin_delete`,
etc.). Retry while `Dataset.status = DELETING` is expected and permitted **only** when
the retrying lineage is the one that produced that `DELETING` state (D16). No other
pipeline type's retry is permitted against a `DELETING`/`DELETED` dataset.

### D21. Repeated / idempotent `DELETE` requests

No client `Idempotency-Key` dependency; resource-scoped, PostgreSQL-authoritative:

- `ACTIVE`, no existing nonterminal `DATASET_DELETE` → create a new run.
- `ACTIVE`, existing nonterminal `DATASET_DELETE` → observe/return that same run
  (never a second one) — same existence check as D12.
- `DELETING` → never starts a second parallel deletion lineage. If the latest
  deletion run for `D` is nonterminal, return it. If the latest is terminal
  `FAILED`/`CANCELLED` with no further retry yet, return a stable conflict indicating
  manual retry is required (D16's `DATASET_DELETING` conflict, carrying the run id).
- `DELETED` → idempotent `200`, no new `PipelineRun`, tombstone (+ optionally the
  completed run's id) returned.

Two concurrent `DELETE` requests on `D` converge to exactly one initial deletion
lineage, via the same submission-time locking pattern required in D12 — not a new
mechanism, not a process-local lock, not a custom external idempotency store.

### D22. `main` protection is independent of and precedes D21

`DELETE` on `main`'s dataset id → `409 MAIN_DATASET_DELETE_FORBIDDEN`, checked before
any of D21's state machine, regardless of `main`'s current status.

### D23. HTTP contract

| Situation | Response |
|---|---|
| New deletion accepted | `202 Accepted`, `{run_id, dataset_id, status}` |
| Existing nonterminal deletion observed | `202`, same `run_id` |
| `DELETING`, latest run terminal, awaiting manual retry | `409`, stable code, includes the run id |
| Already `DELETED` | `200`, tombstone / stable delete result, no new run |
| `main` | `409 MAIN_DATASET_DELETE_FORBIDDEN` |
| Dataset missing | `404` |
| Worker disabled, a **new** run would be required | `503 WORKER_DISABLED`, Dataset stays `ACTIVE`, no mutation |
| Worker disabled, existing run/tombstone only observed | works normally — no worker needed to observe |

No `wait` parameter in the MVP; not added for convenience, consistent with §24.

### D24. Public `DatasetDeleteResult`

Minimum public projection for SM-515, in the same public-safe spirit as
`RunDetailResult` (SM-508/514): `run_id`, `dataset_id`, `status`, and — only on
terminal success, and only where deterministically reproducible from PostgreSQL
metrics — `sources_deleted`, `documents_deactivated`, `chunks_deactivated`,
`entities_deactivated`, `relations_deactivated`, `summaries_deactivated`,
`storage_deleted`, `storage_already_absent`, `graph_events_processed`/`converged`.
Counters that cannot be reproduced safely (e.g. anything requiring a live Neo4j or
filesystem read at result-construction time) are not promised. The result is built
exclusively from PostgreSQL-persisted metrics; the route never reads Neo4j or the
filesystem to construct it.

### D25. Authoritative artifact cleanup — no orphan/Neo4j-driven scope discovery

`deactivate_authoritative` determines scope entirely from PostgreSQL (`dataset_id`
equality), the same way Forget's `AuthoritativeMutationStep` does, but over the whole
Dataset rather than Forget's per-source/per-target orphan logic — administrative
delete does not need orphan detection because the entire dataset is in scope by
definition. Neo4j is never consulted to determine what needs deleting.

### D26. Storage

Every original file under `D`'s storage tree is removed via `delete_storage`, reusing
the already-audited safe-path guards Forget's `StorageDeletionStep` uses
(`source_storage_path`-style controlled-root/traversal/symlink-escape checks). Storage
is never deleted before the authoritative PostgreSQL destructive commit (D10).
Already-absent is idempotent/replay-safe. A crash after `unlink()` but before the step
commits converges cleanly on retry (files already gone is a success state, not an
error).

### D27. Graph

Only `graph_outbox` DELETE commands, reusing SM-506 leasing/fencing exactly as Forget
does. No `MATCH (n) DETACH DELETE n`, no global wipe, no "drop and rebuild" shortcut.
Only projection rows owned by `D` are removed; the external Neo4j sentinel/rebuild
mechanism (ADR-0008) is untouched. Projection convergence must complete before
`finalize_tombstone`.

### D28. Interaction with `POST /api/v1/forget`

**Dataset-scoped Forget on a dataset with a delete intent, `DELETING`, or `DELETED`
status is rejected**, not silently reactivating it. This is enforced the same way as
D16: `RunControlService`/`PipelineSubmissionService` must check the D12 barrier before
accepting a new `Forget` (dataset/source scope) submission on `D`.

**Forget Everything is the higher-risk case and is addressed explicitly (this is the
"CRÍTICO" item, §30 of the task).** Audit finding: `list_ids_for_everything_forget()`
(`repositories/datasets.py:105-114`) selects datasets `WHERE status IN (ACTIVE,
DELETING)` — it does **not** exclude `DELETING`, and `_finalize_dataset_target`
(`pipelines/steps/forget.py:597-598`) unconditionally resets any `DELETING` dataset it
touches back to `ACTIVE`. If an administrative `DATASET_DELETE` run on `D` has reached
`DELETING` and then stalled in a terminal `FAILED`/`CANCELLED` state awaiting manual
retry (D18/D19 — a durable, no-active-run condition), nothing today stops a
subsequently-submitted Forget Everything run from selecting `D`, forgetting its
content, and resetting it to `ACTIVE` — **directly reactivating an administrative
tombstone-in-progress**. Global/dataset-scoped mutual exclusion via advisory locks
prevents this only while the delete run is itself `RUNNING`; it does not prevent it
during the stalled-awaiting-retry window.

`DatasetStatus.DELETING` is therefore an **overloaded value** in current code: Forget's
own dataset-scope mutation already uses it as a purely transient, same-run,
always-reset-before-terminal marker (`pipelines/steps/forget.py:296` → `:597-598`,
both inside one Forget run), while this ADR needs a `DELETING` that is **never**
auto-reset — but only when it is genuinely administrative in origin. Rather than
adding a new column to disambiguate (§36 requires justifying any new column against an
otherwise-unsatisfiable invariant), this ADR resolves it with a **derived, not
stored**, ownership rule.

**Mere historical existence of a `DATASET_DELETE` `PipelineRun` row is not
sufficient** and must not be used. Counterexample that rules it out: `D` is `ACTIVE`;
a `DATASET_DELETE` run `A` is submitted, then cancelled while still `QUEUED` —
`begin_delete` never ran, `D` never left `ACTIVE` (D17). `A`'s row persists forever
(D33 — run history is never deleted). Later, an ordinary `Forget Dataset` legitimately
marks `D` `DELETING` for its own transient reasons. If ownership were "any
`dataset_delete` row exists for this `dataset_id`," `D` would be misclassified as
administratively owned from that point on purely because of `A`'s unrelated history,
and Forget's own legitimate recovery (`pipelines/steps/forget.py:283-294`) would be
wrongly blocked from finalizing `D` back to `ACTIVE`.

The correct rule requires durable proof that the owning run actually **crossed the
`begin_delete` boundary** — the one and only step that ever writes `Dataset.status =
DELETING` for administrative reasons (D9 step 1):

> `administratively_deleting(D)` iff:
>
> `D.status == DELETING`
>
> **and** there exists a `PipelineRun R` such that `R.pipeline_type ==
> 'dataset_delete' AND R.dataset_id == D.id`, **and** the persisted `PipelineStep` for
> `R`'s `begin_delete` step has `status == SUCCEEDED`.

This uses only already-durable `PipelineRun`/`PipelineStep` state (no new column, no
migration): `PipelineStep.status` for the fixed, well-known `begin_delete` step name
is exactly as durable and immutable-once-`SUCCEEDED` as `PipelineRun.pipeline_type`,
and D9's own step-boundary contract guarantees `begin_delete` is `ATOMIC` — its
`SUCCEEDED` transition commits in the same transaction as `Dataset.status = DELETING`,
so the two facts can never disagree once committed.

Frozen matrix (this is normative, not illustrative):

| Situation | `administratively_deleting(D)` |
|---|---|
| `DATASET_DELETE` still `QUEUED` (never claimed) | **No** |
| `DATASET_DELETE` cancelled before `begin_delete` ran | **No** |
| `DATASET_DELETE` failed before `begin_delete` committed | **No** |
| `begin_delete` `SUCCEEDED`, run later `FAILED` | **Yes** |
| `begin_delete` `SUCCEEDED`, run later `CANCELLED` (`CANCELLING` reached a safe checkpoint, D18) | **Yes** |
| Retry lineage (`retry_of_run_id` chain) where the **current** attempt's own `begin_delete` step has `SUCCEEDED` | **Yes** |
| `D.status == DELETED` | **Yes** — administrative tombstone; never a Forget target regardless of the rule above (`D.status` is no longer `DELETING`, so it is excluded by the `IN (ACTIVE, DELETING)` filter itself, not by this predicate) |
| `D.status == DELETING` from an ordinary Forget Dataset-scope run, with only unrelated/never-`begin_delete`-succeeded `dataset_delete` history (the counterexample above) | **No** |

SM-515 must therefore change `list_ids_for_everything_forget()` (and any equivalent
dataset-scope-Forget eligibility check) to exclude exactly the datasets for which the
frozen predicate above is true — not merely "a `dataset_delete` row exists" — and,
as defense-in-depth, `_finalize_dataset_target`'s reset-to-`ACTIVE` branch must
re-evaluate the same predicate immediately before writing `ACTIVE` and refuse to fire
if it is true. `DatasetStatus.DELETED` is already excluded by the existing `IN
(ACTIVE, DELETING)` filter and needs no further change. A dataset stuck `DELETING`
**without** a succeeded `begin_delete` for any `dataset_delete` run against it (a
genuinely stuck Forget dataset-scope run, or a `DATASET_DELETE` that was cancelled/
failed *before* `begin_delete`, pre-existing and orthogonal to this ADR) remains
correctly eligible for Forget's own re-entry handling exactly as it is today.

### D29. Reads

While `ACTIVE` and a delete is only `QUEUED`, existing semantic reads may keep using
current authoritative state normally. Once `DELETING`, `deactivate_authoritative`
makes memory inactive and semantic reads must not consume partial-deleting content —
Recall/Graph reads scoped to `D` must not treat its artifacts as active once
`Dataset.status != ACTIVE`, mirroring how those surfaces already exclude
`is_active=False` rows. `DELETED` → no active semantic memory at all. Management/audit
reads (D6) are unaffected — they are not part of the write queue and are explicitly
permitted to reflect historical/inactive state.

### D30. Interaction with Cognify rebuild

`DATASET_DELETE` and a Cognify rebuild are both dataset-scoped; the existing queue
serialization (D12's mechanism, same primitives as ADR-0009) already prevents
simultaneity. The delete-intent barrier blocks a new Cognify/rebuild submission once
delete is accepted. A rebuild already `RUNNING` finishes first (D13). A rebuild still
`QUEUED` when delete obtains the slot is administratively cancelled per D14. No new
generation is ever activated once `D` is `DELETING`/`DELETED`.

### D31. Source terminal state

Every Source in `D` ends `SourceStatus.DELETED` (the existing enum value Forget
already produces, `domain/enums.py:24-32`), `storage_uri → NULL` only after confirmed
storage deletion (D26). No `PENDING`/`ACTIVE` Source survives inside a `DELETED`
Dataset. Rows are preserved as historical tombstones (D11), consistent with existing
FK/audit requirements.

### D32. `active_generation`

Historical only; never reset to `0`, never incremented during administrative delete. A
`DELETED` dataset has no authoritative usable generation, but the last persisted value
is retained for audit (D4).

### D33. FKs and run history

`PipelineRun`/`PipelineStep` history for `D` is never removed. `pipeline_runs.dataset_id`
keeps pointing at the tombstone row (D4/D11). Retry lineage remains fully valid and
walkable after `D` is `DELETED` — this is a primary reason the tombstone must be a
row, not a purge.

### D34. Migration scope required for SM-515

Exactly one class of schema change is required by this ADR: adding
`PipelineType.DATASET_DELETE` to the Python enum and the Postgres native `pipeline_type`
enum type (`ALTER TYPE ... ADD VALUE`, D2). No new column, no new table. Audited and
rejected as unnecessary given the derived-ownership rule (D28) and the existing
`DatasetStatus`/lifecycle columns: `delete_requested_at`, `deleted_at`,
`deletion_run_id`, a separate tombstone table. If SM-515 discovers during
implementation that one of these actually is needed, that is new information requiring
this ADR (or an amendment) to be revisited — not a silent addition.

### D35. Pipeline registry — target state after SM-515

```text
COGNIFY
IMPROVE
FORGET
REMEMBER
DATASET_DELETE
```

Exactly five write pipelines, `DATASET_DELETE` using a fixed `PipelineDefinition`
(D9), no dynamic/request-defined pipeline shape.

### D36. Security / privacy

`DatasetDeleteResult` and any error detail never expose document text, chunk text,
embeddings, storage absolute paths, raw Neo4j payloads, API secrets, the database URL,
or tracebacks. IDs, counters, and status values are the only content.

### D37. New `ErrorCode` values (frozen names, not added by this ADR)

`schemas/common.py`'s `ErrorCode` enum currently has 11 values, none covering this
contract. SM-515 adds exactly these three stable, closed-enum values — the same names
already used throughout D3/D16/D21/D23 above, not placeholders:

- `MAIN_DATASET_DELETE_FORBIDDEN` — `409`, D3/D22's `main` guard.
- `DATASET_DELETING` — `409`, D16's retry-conflict and D21's repeated-DELETE
  awaiting-manual-retry conflict.
- `DATASET_DELETED` — `409`, D16's retry-conflict when the target dataset is already
  a terminal tombstone.

No other new `ErrorCode` value is required by this contract; existing codes
(`WORKER_DISABLED`, `RUN_NOT_RETRYABLE`, standard `404`) cover the remaining D23 rows.

## Transaction / lifecycle boundaries

Boundaries mirror ADR-0009's execute/persist convention exactly (`ATOMIC` steps commit
their PostgreSQL mutation together with their own step-succeeded transition; `external`
steps do PostgreSQL-free I/O in `execute()` and only ever commit already-safe/idempotent
results in `persist()`). No new boundary primitive is introduced — see D9's table and
ADR-0009 for the underlying mechanism.

## Interaction with existing runs

Covered by D12–D16, D28, D30: a durable delete intent for `D` blocks new incompatible
dataset-scoped submissions on `D` (any pipeline type, including a second independent
delete, including Forget Everything's implicit per-dataset targeting), lets an
already-`RUNNING` writer finish under its existing ADR-0009 lifecycle, and
administratively terminates other still-`QUEUED` work on `D` the moment the delete run
claims the operational slot.

## Cancellation and retry

Covered by D17–D22: `QUEUED` cancel is a full no-op rollback (Dataset never touched);
`RUNNING`/later cancel or failure after `begin_delete` commits leaves `Dataset.status =
DELETING` permanently (never auto-reactivated); the only recovery is SM-514 manual
retry of the `DATASET_DELETE` lineage itself, which is uniquely permitted to continue
against a `DELETING` dataset. `CancellationRecoveryMode` classification (D9) follows
ADR-0009 §I / SM-507 exactly, with `delete_storage` explicitly `AMBIGUOUS` (never
`ATOMIC` for convenience) and `converge_projection` `RECONCILABLE`, matching Forget's
own classifications for the structurally identical steps.

## Tombstone and retention

Covered by D4, D5, D11, D31–D33: `Dataset` row permanent; `name`/`slug` permanently
reserved; every content-family table's disposition audited and frozen in D11's table;
`PipelineRun`/`PipelineStep`/`GraphOutbox` history always survives.

## Storage

Covered by D26: audited safe-path guards, destructive-order-last (after PostgreSQL
authoritative commit, before/with `finalize_tombstone`), idempotent, no lock held
around filesystem I/O, `AMBIGUOUS` cancellation recovery.

## Graph projection

Covered by D27: `graph_outbox`-only, SM-506 leasing reused, no global wipe, must
converge before `finalize_tombstone`.

## HTTP/API semantics

Covered by D23–D24: full response matrix, no `wait` parameter, public result shape and
its PostgreSQL-only construction constraint.

## Migration impact

Exactly one native-enum `ADD VALUE` migration (D2/D34) — no other schema change is
required or justified by this ADR.

## Failure/recovery matrix

Per §39 of the task, classified against D9's step boundaries:

| Case | Point of crash | `Dataset.status` after | Recovery |
|---|---|---|---|
| A | before `begin_delete` commits | `ACTIVE` | retry-safe; nothing happened |
| B | after `DELETING`, before authoritative cleanup commits | `DELETING` | retry resumes at step 2 |
| C | after PG cleanup/outbox enqueue, before projection convergence | `DELETING`, no active authority remains | retry re-drains outbox (idempotent) |
| D | during projection convergence | `DELETING` | `graph_outbox` replay (SM-506) |
| E | after projection, during storage deletion | `DELETING` | already-absent files are replay-safe |
| F | after storage, before `finalize_tombstone` commits | `DELETING` | retry finalizes |
| G | after `Dataset.status = DELETED`, before the run itself reaches a terminal status/result | `DELETED` | engine/retry must detect this durable terminal business state (`Dataset.status = DELETED` already true) and must not attempt to redo the destructive effect — `finalize_tombstone` re-run is itself idempotent (setting `DELETING → DELETED` when already `DELETED` is a safe no-op check, not a second destructive act) |

Every case above resolves through the existing ADR-0009 stale-recovery mechanism plus
SM-514 manual retry; no new recovery primitive is introduced.

Cases A and B–G are exactly the boundary D28's `administratively_deleting(D)`
predicate is built on: case A is precisely the "`begin_delete` step not yet
`SUCCEEDED`" state, where the predicate is `False` and `D` is safe for any other
pipeline (including Forget) to treat normally; cases B–G all have `begin_delete`
`SUCCEEDED` (that transition is what produces `DELETING` in the first place, D9 step
1), so the predicate is `True` from case B onward and `D` must not be touched by
Forget regardless of what the `DATASET_DELETE` run itself does next. SM-515's
regression tests for this table (acceptance evidence, below) must include an
assertion of the predicate's value at each case, not just `Dataset.status`.

## Consequences

### Positive

- Reuses 100% of the existing B5 engine, queue, cancel/retry, and Forget's audited
  low-level primitives — no second engine, no second synchronous path.
- The tombstone-forever + reserved-namespace decisions require zero schema migration
  beyond the one enum value, because they are already how the schema behaves.
- The derived-ownership rule (D28) closes a real, concretely-identified reactivation
  hazard (Forget Everything resetting a stalled administrative `DELETING` dataset back
  to `ACTIVE`) without adding a column.
- Every destructive step's cancellation-safety is classified using the same
  `CancellationRecoveryMode` vocabulary already proven by Forget, so SM-507's recovery
  service needs no new logic to handle `DATASET_DELETE`.

### Negative / trade-offs

- `name`/`slug` are permanently unavailable after any delete, even accidental ones —
  accepted deliberately (D5) in favor of audit integrity over reuse convenience; a
  real operational cost for a single-user MVP where a mistaken delete cannot free the
  name back up without a future ADR.
- A `DATASET_DELETE` that fails after `begin_delete` leaves the dataset permanently
  unusable (blocked for new writes, memory inactive) until an operator issues a manual
  retry — there is no automatic self-healing back to `ACTIVE`. This is intentional
  (D18/D19) but means an `AMBIGUOUS` storage-step failure requires manual intervention
  by design, not automatically.
- `MemoryEntry` has no existing lifecycle/`is_active` column (D11), so
  `deactivate_authoritative` must exclude `DELETING`/`DELETED`-dataset entries at the
  query layer rather than by flipping a column on the row itself — a minor asymmetry
  with every other content table that SM-515's implementer must get right in every
  read surface that touches `MemoryEntry`, not just one place.

## Alternatives considered

1. **Implement `DELETE` as an alias/wrapper of Forget.** Rejected: Forget's terminal
   state is `ACTIVE`, the opposite of what an administrative delete must guarantee
   (D1); would require Forget's own finalizer to grow delete-vs-forget branching,
   contaminating an already-audited, working pipeline.
2. **Hard `DELETE` the `Dataset` row.** Rejected: blocked today by `RESTRICT` FKs on
   every content table (D4); would require changing every one of those FK policies,
   destroying the audit trail this ADR exists to preserve, and is explicitly out of
   scope (§45).
3. **Free the `name`/`slug` by renaming the tombstone on delete.** Rejected: defeats
   the audit purpose of the tombstone, and creates an inconsistent historical record
   (D5).
4. **Synchronous special-case for an "empty" dataset.** Rejected: "empty" is not
   determinable race-free outside the durable pipeline (D8/D19's empty-dataset race);
   a second code path also breaks FR-100's run-id-on-every-write requirement and
   duplicates cancel/retry/audit logic.
5. **Mark `Dataset.status = DELETING` at HTTP submission time.** Rejected: would break
   a writer already legitimately `RUNNING` on `D`, whose own `ACTIVE` checks would then
   fail mid-flight (D13/D15).
6. **Accept new writes while the delete run is only `QUEUED`.** Rejected: accepted work
   would be known-doomed the moment the delete claims the slot (D12).
7. **Leave other queued writes on `D` stuck forever once a tombstone exists.** Rejected:
   D14 administratively terminates them explicitly and durably instead.
8. **Use Neo4j to discover deletion scope.** Rejected: violates ADR-0002/ADR-0008 —
   PostgreSQL is the sole authority for scope; Neo4j is a rebuildable projection (D25/D27).
9. **Global Neo4j wipe as a delete shortcut.** Rejected: destroys other datasets'
   projections; violates ADR-0008's per-dataset rebuild contract (D27).
10. **Delete storage before the PostgreSQL authoritative commit.** Rejected: an
    interrupted delete would then have destroyed originals while PostgreSQL still
    claims the content is active — the opposite of ADR-0002's authority ordering (D10).
11. **Auto-reactivate the Dataset after a cancel or failure post-`begin_delete`.**
    Rejected: cannot safely prove no destructive effect has already committed (D18/D19).
12. **Treat a fresh, non-lineage `DELETE` request as an implicit substitute for manual
    retry of a partial destructive operation.** Rejected: would let a second,
    unrelated deletion lineage race the first over the same partially-destroyed state;
    D21 requires convergence onto exactly one lineage.
13. **Physically remove `PipelineRun`/`PipelineStep` history for the deleted dataset.**
    Rejected: destroys retry lineage and audit trail (D33), and the Dataset tombstone's
    entire purpose is to keep this FK chain valid (D4).
14. **Add a new external queue/worker for administrative delete.** Rejected: ADR-0009
    already forbids a second engine; this ADR introduces zero new operational
    infrastructure (D9 reuses the existing worker/engine unchanged).

## Implementation obligations for SM-515

- Add `PipelineType.DATASET_DELETE` (Python enum + `ALTER TYPE ... ADD VALUE`
  migration, D2/D34).
- Add `DELETE /api/v1/datasets/{dataset_id}` route implementing the D23 HTTP matrix.
- Add the fixed `DATASET_DELETE` `PipelineDefinition` with the five D9 steps, each
  classified with the `CancellationRecoveryMode` given in D9's table.
- Extend `PipelineSubmissionService` (or add a sibling path alongside it) to enforce
  the D12 delete-intent barrier and D21's repeated-DELETE convergence, using the same
  transactionally-safe locking pattern already used for idempotency-race safety —
  cite the existing pattern, do not invent a new one.
- Extend `RunControlService.retry()` to add the D16 dataset-state revalidation before
  creating a new dataset-scoped run, returning the stable conflicts from D16/D21.
- Change `list_ids_for_everything_forget()` (and any equivalent dataset-scope-Forget
  eligibility check) to exclude datasets satisfying D28's `administratively_deleting(D)`
  predicate (a `dataset_delete` run whose `begin_delete` step has `SUCCEEDED` —
  mere row existence is explicitly insufficient, D28's counterexample); add the
  corresponding defense-in-depth guard to `_finalize_dataset_target`, and a regression
  test reproducing D28's counterexample exactly (cancel a `DATASET_DELETE` before
  `begin_delete`, then run an ordinary Forget-owned `DELETING`/`ACTIVE` cycle on the
  same dataset, and assert Forget Everything is unaffected).
- Add the D37 `ErrorCode` values.
- Add the D24 `DatasetDeleteResult` schema, PostgreSQL-metrics-only.
- Verify Recall/Graph read surfaces satisfy D29 (no `DELETING`/`DELETED`-dataset
  content served as active semantic memory). The decision is frozen; only the
  mechanics are left to implementation-time verification — whether this already holds
  implicitly via existing `is_active` filtering, or requires an explicit
  dataset-status join, is a code-level fact to confirm, not a new architectural
  choice.
- Update `MemoryEntry`-touching query surfaces to treat `DELETING`/`DELETED`-dataset
  rows as inactive despite the absence of a lifecycle column on that table (D11/D28
  negative consequence).

## Acceptance evidence required

Per `AGENTS.md` §21/§28 and consistent with SM-514's own validation battery, SM-515
must produce, against real PostgreSQL:

- Unit/contract coverage for the full D23 HTTP matrix and D21 repeated-DELETE state
  machine.
- Real-Postgres concurrency tests: delete-vs-claim race, delete-intent barrier
  blocking each of the five incompatible submission types, two concurrent `DELETE`
  requests converging to one lineage, `begin_delete`'s administrative cancellation of
  other queued work, dataset-operational-unique-constraint interaction unaffected.
- A dedicated real-Postgres test proving Forget Everything does **not** reactivate a
  dataset stalled `DELETING` under a `dataset_delete` lineage whose `begin_delete`
  step has `SUCCEEDED` (D28's `administratively_deleting(D)` predicate) — this is the
  single highest-risk regression this ADR identifies and must not ship without a
  passing test exercising exactly this race.
- The converse, equally required real-Postgres regression test for D28's own
  counterexample: submit `DATASET_DELETE` on `D`, cancel it while still `QUEUED`
  (`begin_delete` never runs), then run an ordinary Forget Dataset-scope cycle to
  completion on the same `D` (`ACTIVE → DELETING → ACTIVE`) — and assert Forget
  Everything correctly still selects and finalizes `D` normally, unaffected by `A`'s
  unrelated cancelled history. A ship without this test cannot show the fix for the
  first bullet did not overcorrect into blocking legitimate Forget recovery.
- Real-Postgres tests for D18/D19 (cancel/failure after `begin_delete` never
  reactivates), D20 (manual retry converges a partially-destroyed dataset to
  `DELETED`), and the full D-through-G crash matrix in "Failure/recovery matrix" above,
  mirroring SM-514's own stale-recovery test pattern.
- `main` protection and namespace-reuse-forbidden tests (D3, D5, D22).
