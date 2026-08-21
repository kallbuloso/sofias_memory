# ADR-0009: Worker, Queue Claiming and Pipeline Lifecycle Contract

## Status

Accepted

## Context

B4 proved the Core Memory functional synchronous path (`GATE-B4 PASSED`). It reused
`pipeline_runs`, `pipeline_steps`, and `graph_outbox` as bookkeeping for synchronous
`wait=true` execution, but none of the operational machinery exists yet:

- no polling worker;
- no `FOR UPDATE SKIP LOCKED` queue claim;
- no pipeline engine (`sofias_memory/pipelines/` contains only a B4 chunking helper);
- no worker lifecycle in `lifespan.py`;
- no heartbeat, stale recovery, retry scheduling, or cancellation;
- `graph_outbox.mark_processing` flips `status`/`attempt` but records no lease
  timestamp or owner, so a crash mid-projection leaves a `processing` row with no way
  to detect it is abandoned;
- `PipelineRun` has no durable retry schedule or manual-retry lineage column;
- `PipelineType` has only `remember`, `cognify`, `improve`, `forget`.

B5's job is to turn this bookkeeping into a durable, restart-safe runtime without
introducing an external queue, a second worker process, or a second synchronous
engine living alongside the async one. This ADR is the architecture gate required by
`docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B5.md` (SM-501) before any
B5 code is written. It freezes the 19 decisions already approved in that backlog
(section 5) and gives them concrete, testable mechanisms.

Identity boundary (no users/tenant/ACL/roles/permissions), PostgreSQL authority, and
Neo4j-as-rebuildable-projection are not reopened here — they are inherited from
`AGENTS.md`, the PRD, ADR-0001, ADR-0002, and ADR-0008. This ADR states only the
operational consequence for the worker/queue/lifecycle layer, not a restatement of
those documents.

Administrative `DELETE /api/v1/datasets/{dataset_id}` is explicitly **not** resolved
here. It gets its own `ADR-0010` before SM-515, per backlog section 5.15.

## Decision

Sofias Memory adds one internal worker, in the same FastAPI process, that claims
`pipeline_runs` rows from PostgreSQL with `FOR UPDATE SKIP LOCKED` and executes them
through a versioned, code-registered pipeline engine. PostgreSQL is the queue, the
lifecycle authority, and the durability boundary. There is no in-memory durability
boundary: no `asyncio.create_task` fire-and-forget, no in-memory queue, no
process-local lock as the source of exclusion truth. `wait=true` and `wait=false`
submit to and observe the exact same run through the exact same engine; there is no
second synchronous implementation.

The sections below freeze each mechanism required by backlog section 5 (A–X in the
SM-501 task description).

---

## A. PipelineRun lifecycle

States (unchanged, no new enum value added):

```text
queued
running
succeeded
failed
cancelling
cancelled
```

### Transition matrix

| From | To | Trigger | Who |
|---|---|---|---|
| — | `queued` | durable submission commit | API/service, in the same transaction that resolves/creates the Dataset |
| `queued` | `running` | claim (`FOR UPDATE SKIP LOCKED`) | worker |
| `queued` | `queued` | automatic retry re-eligibility (`next_attempt_at` reached) is a no-op transition; run never leaves `queued` for this | worker |
| `queued` | `cancelled` | cancel request on a run not yet claimed | cancel endpoint (SM-514) |
| `running` | `succeeded` | engine finishes final step with no error | worker |
| `running` | `queued` | step failed with a retryable error and attempts remain | worker (sets `next_attempt_at`, increments `PipelineRun.attempt`) |
| `running` | `failed` | permanent error, retries exhausted, or `config_fingerprint` mismatch on recovery | worker |
| `running` | `cancelling` | cancel request while claimed | cancel endpoint, observed by worker at next safe checkpoint |
| `cancelling` | `cancelled` | worker reaches a safe checkpoint | worker |
| `cancelling` | `failed` | safe checkpoint reached but the in-flight step could not be rolled back cleanly and left no committed partial state requiring compensation | worker (rare; see section N) |
| any non-terminal | `failed` | stale recovery classifies the run as unrecoverable (§I) | startup recovery |

Explicitly proibited:

- `succeeded`/`cancelled` → anything (terminal, immutable);
- `failed` → anything except via a **new** `PipelineRun` created by manual retry (§M); the failed run itself never reopens;
- `queued` → `running` performed by anything other than the claim query (no service-layer shortcut);
- `cancelling` → `running` (cancellation intent is never silently dropped; see §I for the stale-`cancelling` recovery case, which still converges to a terminal state, never back to `running`).

### Fields

- `created_at`: set at durable submission commit (§C), never rewritten.
- `started_at`: set the first time the run transitions `queued → running` (i.e. on
  first claim). Not reset on subsequent automatic-retry reclaims of the same run.
- `finished_at`: set exactly once, on the transition into `succeeded`, `failed`, or
  `cancelled`. Never set on `cancelling`.
- `current_step`: the `PipelineStep.name` currently executing or last attempted;
  cleared only conceptually (kept as the last known step) once terminal — it is
  observability, not control state.
- `progress`: `REAL` in `[0.0, 1.0]`, computed as `succeeded_steps / total_registered_steps`
  for the pipeline's registered step list (§O); monotonic within one run's lifetime
  except it may reset on retryable failure only if steps before the failed one are
  re-marked (they are not — see §L, resume skips succeeded steps).
- `worker_id`: the opaque id (§G) of the worker that currently owns or most recently
  owned the run; retained after terminal states for audit, not cleared.
- `heartbeat_at`: updated only while `running`/`cancelling` and owned by a live
  worker (§H); left untouched (not nulled) after a terminal transition, since it is
  historical, not a liveness signal once terminal.
- `error_code`/`error_message`: set only on `failed`; stable, public-safe values
  (§X); never a raw provider payload or traceback.
- `metrics`: JSONB, additive, safe for public API (SM-508); never chunk text,
  embeddings, or prompts.
- `attempt`: see §L.
- `next_attempt_at` (new column, §K): `NULL` unless the run is `queued` awaiting a
  durable automatic retry.
- `retry_of_run_id` (new column, §M): `NULL` unless this run was created by manual
  retry.

---

## B. PipelineStep lifecycle

States mirror `PipelineRunStatus` (`queued`, `running`, `succeeded`, `failed`,
`cancelling`, `cancelled`), scoped to one row per `(run_id, ordinal)`.

- **Creation:** all `PipelineStep` rows for a run are materialized in the **same
  submission transaction** as the `PipelineRun` row (§C), all initially `queued`,
  derived from the registry's step list for that `pipeline_type` at submission
  time. Materialization is not deferred to first claim. This is a deliberate
  correction from an earlier draft of this ADR, for four reasons:
  1. **durable execution plan** — the full plan a run will execute is visible in
     PostgreSQL the moment the run is accepted, not invented lazily by whichever
     worker process happens to claim it first;
  2. **Runs API observability before claim** — `GET /api/v1/runs/{run_id}` (SM-508)
     can show the complete step plan for a `queued` run, not just an opaque
     top-level status;
  3. **auditable cancellation of `queued` runs** — cancelling a run that was never
     claimed (`queued → cancelled`, §N) has a concrete, already-persisted step plan
     to reference in audit/history, instead of "a plan that would have existed";
  4. **drift detection** — a deploy that changes the registry's step list between
     submission and claim cannot silently redefine what a `queued` run executes;
     the persisted step plan is compared against the registry at claim time (see
     `input_hash`/drift below), so a mismatch fails loudly (`STEP_INPUT_DRIFT`)
     instead of silently running a different pipeline than what was accepted.

  The registry itself remains closed and versioned in code (§O) — this change does
  not add a `pipeline_version` column; it only moves *when* the registry's step
  list for a given `pipeline_type` is read and persisted as concrete rows, from
  "first claim" to "submission".
- **Ordinal:** stable, zero-based, assigned from the registered step order for that
  `pipeline_type`. Ordinals are never renumbered across retries or resumes.
- **Transitions — the unambiguous, complete matrix (corrects an earlier draft of
  this ADR, where §B's summary and §I's recovery rules disagreed):**

  | From | To | When |
  |---|---|---|
  | — | `queued` | materialization (§C) |
  | `queued` | `running` | engine starts executing this step |
  | `queued` | `cancelled` | owning run is cancelled before this step ever started |
  | `running` | `succeeded` | step completes with no error |
  | `running` | `failed` | permanent error, or a retryable error with the run's attempt ceiling already exhausted |
  | `running` | `queued` | **only** a retryable error with attempts remaining (§K), or `RUNNING`-stale recovery reconciling an abandoned claim (§I) |
  | `running` | `cancelled` | **only** `CANCELLING`-stale recovery classifying the orphaned step as case A or B (safe/reconciled, §I) |

  `succeeded`, `failed`, and `cancelled` are terminal for that `PipelineStep` row.
  Manual retry (§M) never reopens a `failed` step in the same run — it creates a
  new `PipelineRun` with fresh `PipelineStep` rows instead.

  **`PipelineStepStatus.CANCELLING` is not part of this matrix.** The enum value
  is preserved for schema compatibility (it mirrors `PipelineRunStatus`, §A), but
  the B5 engine's normal cancellation path never routes a step through it: because
  cancellation is observed only at the safe checkpoint between steps (§N), the
  step actively executing when a run enters `cancelling` simply keeps running to
  its own `succeeded`/`failed` outcome under the normal matrix above, while the
  run itself stays `cancelling` until that checkpoint; steps that were `queued`
  and never got to start go directly `queued → cancelled` once the run finalizes.
  A future story must not introduce step-level `cancelling` usage "because the
  enum exists" without a documented reason and an ADR update.
- **`attempt`:** increments each time this specific step is executed (§L). Does not
  reset when the owning run is reclaimed for a later `PipelineRun.attempt`.
- **`input_hash`:** `CHAR(64)` sha256 hex of everything that semantically determines
  what the step will do: its canonical input snapshot (derived from prior step
  output or the top-level request) **and**, when the step's own definition/version
  in the registry can change its behavior for the same input, a stable identifier
  of that step definition/version. This is computed and persisted once, at
  materialization time (submission, per the Creation rule above) for steps whose
  input is already fully known then; for steps whose input depends on a prior
  step's output, `input_hash` is computed and persisted when that prior output
  first becomes available (still before the step itself executes). No new
  `pipeline_version` column is introduced — the registry's existing step-definition
  identity (already a code-level constant per §O) is reused as an input to the hash
  when relevant, not stored as a separate column.
- **`output`/`metrics`:** JSONB, additive, public-safe subset only — never raw
  provider payload, document text, or embeddings (same redaction rule as PRD
  section 23 / SM-508's "não expor" list).
- **`error`:** JSONB with a stable `code` and safe `message`; classified per §X.
- **`started_at`/`finished_at`:** set exactly once per attempt's terminal outcome;
  re-executing the step (new attempt) does not retroactively rewrite the previous
  attempt's timestamps — the row holds only the latest attempt's timestamps, while
  `PipelineStep.attempt` is the durable count of how many times it ran. Per-attempt
  history beyond the counter is not persisted (no separate attempt-history table);
  this is a deliberate simplicity trade-off, not an oversight.

### Skip / resume rule

On every claim (initial or reclaim), the engine first re-validates the persisted
step plan against the current registry definition for that `pipeline_type`
(ordinal count, names, and — where already computable — `input_hash` for steps
whose input was fully known at materialization time); a mismatch here fails the
run immediately with `STEP_INPUT_DRIFT` before executing anything, covering the
case where the registry itself changed between submission and claim (e.g. a
deploy landed mid-flight). Then, walking the step list in ordinal order, for each
step:

- if a `PipelineStep` row exists with `status = succeeded` **and** (no `input_hash`
  is defined for that step, or the recomputed input hash matches the stored one) →
  **skip**, do not re-execute;
- if `input_hash` is defined and does not match → **fail the run** with
  `STEP_INPUT_DRIFT` (permanent, §X); this must never happen for correctly composed
  pipelines and indicates a code/config change mid-flight, not a transient error;
- otherwise → **execute** (first time, or re-attempt after a prior `failed`/`queued`
  retry cycle for that step).

Cancellation checkpoints exist only **between** steps (§N); a step, once started,
runs to its own terminal outcome (`succeeded` or `failed`) before the engine
re-evaluates cancellation.

---

## C. Queue submission

A durable submission is one PostgreSQL transaction:

```text
1. validate request (schema, dataset existence rules per pipeline type);
2. resolve or create the Dataset row when the pipeline type requires one
   (e.g. Remember on a not-yet-existing dataset slug);
3. reject a client-supplied `Idempotency-Key` in the reserved `sys:` namespace
   (§M) with `400 RESERVED_IDEMPOTENCY_KEY_NAMESPACE`; otherwise resolve/create the
   Idempotency-Key row or short-circuit to an existing PipelineRun for the same
   canonical work identity (§S);
4. INSERT PipelineRun with status='queued', attempt=0, next_attempt_at=NULL,
   dataset_id resolved (or NULL only for a true global operation, §F);
5. INSERT one PipelineStep row per step in the registry's step list for this
   pipeline_type (§B), all status='queued', ordinal per registry order,
   input_hash populated for any step whose input is already fully known at this
   point;
6. COMMIT.
```

Only after this commit may the API respond (`202` for `wait=false`, or begin
waiting for `wait=true`, §Q/§R). No public endpoint may report a run as accepted,
return a `run_id`, or start polling/waiting before this transaction is committed.
`PipelineStep` rows **are** created at submission time, in the same transaction as
the `PipelineRun` row (§B) — this is a correction from an earlier draft of this
ADR, which deferred step materialization to first claim.

This is the concrete mechanism behind the invariant "no essential recovery state may
exist only in process memory" (backlog section 4): the row exists in PostgreSQL
before any other actor observes the request as accepted.

---

## D. Claim

### The race `FOR UPDATE SKIP LOCKED` alone does not close

`FOR UPDATE SKIP LOCKED` gives exclusivity over **the candidate row itself**: two
claimers cannot both walk away with the *same* `PipelineRun` row. It gives no
exclusivity across **different rows** that happen to share a `dataset_id`, and a
`NOT EXISTS` subquery over other rows' current `status` is a plain `SELECT` with no
locking semantics of its own — it only sees whatever the other transaction has
already committed at the time it runs.

Concretely, this race is real and this ADR's first draft understated it:

```text
Tx A: claims Run A (dataset X)
  -> SELECT ... WHERE NOT EXISTS (running/cancelling for dataset X) -- sees none
Tx B: claims Run B (dataset X), concurrently, before Tx A commits
  -> SELECT ... WHERE NOT EXISTS (running/cancelling for dataset X) -- also sees none
Tx A: UPDATE Run A SET status='running'; COMMIT
Tx B: UPDATE Run B SET status='running'; COMMIT
-- both committed: dataset X now has two RUNNING writes.
```

The same window exists between a global run (`dataset_id IS NULL`) and a
dataset-scoped run: both can observe "no conflicting run" before either commits.
`NOT EXISTS` inside the same statement is not sufficient on its own to prevent
this, because the two candidate rows are different rows locked by different
`FOR UPDATE SKIP LOCKED` invocations — SKIP LOCKED never makes one transaction wait
on, or become visible to, the other's uncommitted state. The phrase "double claim
impossible by construction" from this ADR's earlier draft is retracted; it was
true only for the same row, never for two distinct rows of the same dataset (or a
global vs. dataset-scoped pair).

### Layered defense

This ADR freezes four **distinct** mechanisms, each closing a different gap; none
of them alone is claimed sufficient:

1. **`FOR UPDATE SKIP LOCKED`** — exclusivity over the queue row itself; a claimer
   never processes a row another claimer already has locked. Prevents the same
   `PipelineRun` from being claimed twice.
2. **PostgreSQL transaction-level advisory lock arbitration** (new in this
   revision) — exclusivity **across rows** sharing the same `dataset_id`, or across
   a global run vs. any dataset-scoped run, enforced *before* the claiming
   transaction commits. This is what closes the race above.
3. **Persisted `RUNNING`/`CANCELLING` state** — after commit, the row itself is the
   durable proof of ownership; any later claimer's read of current state reflects
   reality once committed.
4. **Partial unique index (defense in depth)** — a second, independent guarantee at
   the schema level that no two `pipeline_runs` rows can simultaneously carry
   `dataset_id = X AND status IN ('running','cancelling')`, so that even a future
   bug in the claim query's transaction logic cannot silently produce two
   concurrent writers for one dataset; the write would fail loudly instead.
   **Physical activation of this mechanism is deferred** past SM-502 and SM-503,
   to after the last direct-`RUNNING` B4 writer is migrated (see the rollout
   amendment below) — the contract remains required before `GATE-B5`.

### Advisory lock arbitration

Dataset-scoped claim:

```text
1. select a queued candidate row for dataset X (ordinary SELECT, no lock yet, to
   pick a target without blocking on the lock);
1.5. fairness precheck (starvation guard, see below): if an eligible queued
   global run (dataset_id IS NULL, status='queued', next_attempt_at unset/due)
   exists with created_at strictly earlier than this candidate's created_at,
   skip this candidate entirely -- do not attempt the locks below, try the next
   candidate instead;
2. acquire pg_try_advisory_xact_lock_shared(GLOBAL_BARRIER_KEY);
   -- shared: many dataset-scoped claimers may hold this simultaneously;
   -- fails (false) only while a global claimer holds the exclusive form (step 2
   --   of the global claim path below), which means a global op is currently
   --   being claimed or is already running/cancelling;
3. acquire pg_try_advisory_xact_lock(dataset_lock_key(dataset_id));
   -- exclusive, scoped to this one dataset_id;
   -- fails (false) if another transaction is concurrently claiming (or already
   --   owns, for the duration of its transaction) the same dataset_id;
4. if either try-lock failed: release what was acquired (automatic at
   ROLLBACK/end of the failed attempt), skip this candidate, try the next one;
5. re-validate eligibility now that both locks are held: re-read the candidate's
   status ('queued'), re-check NOT EXISTS running/cancelling for this dataset_id
   AND NOT EXISTS running/cancelling for dataset_id IS NULL -- both checks are now
   race-free, because no other dataset-X claimer and no global claimer can be
   concurrently mid-claim while these locks are held;
6. UPDATE the candidate row to 'running', worker_id, heartbeat_at, started_at,
   attempt + 1, next_attempt_at = NULL, exactly as before;
7. COMMIT. Both advisory xact locks are released automatically at COMMIT
   (transaction-scoped locks, not session-scoped -- no explicit unlock call, and
   no risk of a leaked lock surviving past this transaction).
```

Global claim (`dataset_id IS NULL`, i.e. `Forget Everything`):

```text
1. select a queued candidate global row;
2. acquire pg_try_advisory_xact_lock(GLOBAL_BARRIER_KEY);   -- exclusive form
   -- fails (false) if any dataset-scoped claimer currently holds the shared form,
   --   or another global claimer already holds the exclusive form;
3. re-validate: NOT EXISTS any dataset-scoped run running/cancelling, AND NOT
   EXISTS another global run running/cancelling;
4. UPDATE to 'running', same fields as above;
5. COMMIT, locks released automatically.
```

`pg_try_advisory_xact_lock*` (not the blocking `pg_advisory_xact_lock*` form) is
used deliberately: a candidate blocked by a concurrent claim is **skipped**, not
waited on, keeping claim latency bounded and avoiding a worker thread parked on a
lock while other eligible candidates for other datasets sit unclaimed.

### Global barrier fairness (starvation guard)

Shared/exclusive advisory-lock arbitration alone closes correctness (no
simultaneous execution) but not **fairness**: without the precheck above, a
continuous stream of newly-submitted dataset-scoped writes could keep winning the
shared lock indefinitely, so a pending eligible global run (`Forget Everything`)
could in principle never see a moment where no dataset-scoped claimer is
concurrently attempting the shared lock, and starve.

Frozen rule: **once a global run is queued and eligible, no dataset-scoped run
submitted after it may be claimed ahead of it.** Concretely, via the "fairness
precheck" step above:

```text
1. global G becomes queued and eligible (next_attempt_at unset/due);
2. dataset-scoped runs already RUNNING/CANCELLING at that moment are unaffected
   and keep executing to their own terminal state normally -- fairness never
   preempts in-flight work, it only withholds new claims;
3. any dataset-scoped candidate with created_at > G.created_at fails the
   fairness precheck and is skipped by every claimer, for every poll tick, for
   as long as G remains queued and eligible;
4. dataset-scoped candidates with created_at < G.created_at (already ahead of G
   in FIFO order before G existed) are unaffected -- they already had
   precedence and are claimed normally;
5. once the currently-running dataset-scoped runs from step 2 all reach a
   terminal state, no claimer is acquiring the shared GLOBAL_BARRIER_KEY lock
   anymore (no eligible dataset-scoped candidate remains claimable while G is
   pending), so G's exclusive pg_try_advisory_xact_lock(GLOBAL_BARRIER_KEY)
   succeeds on a subsequent poll tick and G is claimed;
6. after G reaches a terminal state, normal claiming resumes for all
   dataset-scoped candidates, including any that accumulated
   created_at > G.created_at while G was pending.
```

**Fairness only applies while G is queued AND eligible** (the same eligibility
predicate as claim itself, §D: `next_attempt_at IS NULL OR next_attempt_at <=
now()`) — the fairness precheck (step 1.5 of the dataset-scoped claim algorithm
above) is defined in terms of "an eligible queued global run", not merely "a
queued global run". This has a direct consequence for automatic retry (§K) that
this ADR states explicitly to avoid ambiguity:

- while G sits `queued` with `next_attempt_at > now()` (durable retry backoff,
  §K), G is **not** eligible, the fairness precheck's `NOT EXISTS` over eligible
  global candidates finds none, and dataset-scoped claims proceed completely
  normally for the entire backoff window — G does **not** hold the barrier
  during its own backoff;
- once `next_attempt_at <= now()`, G becomes eligible again, its `created_at`
  precedence is honored again exactly as in step 3 above, and any
  dataset-scoped run newer than G is withheld until G is claimed or requeued
  again.

This is a pure PostgreSQL predicate added to the existing per-candidate claim
loop -- no in-memory mutex, no separate scheduler, no priority queue beyond the
single `created_at`-ordering rule already frozen above. It only ever *withholds*
a claim attempt while an eligible global candidate is pending; it never revokes,
preempts, or cancels an already-`running` dataset-scoped run, and it never holds
the barrier during a global run's own retry backoff. A pending *eligible* global
run therefore bounds new dataset-scoped throughput to zero only for the
(expected to be short, single-replica MVP) duration between "global becomes the
oldest eligible candidate" and "currently running dataset-scoped work drains" --
not indefinitely, not during its own backoff, and not retroactively against work
already in flight.

**Key derivation:** advisory locks use a 64-bit (or two-32-bit-int) key space with
no built-in namespacing, so this ADR freezes a deterministic, collision-safe-enough
derivation for the MVP rather than a literal magic integer:

```text
GLOBAL_BARRIER_KEY = a single fixed constant, reserved exclusively for this
                      purpose (never reused for any other advisory lock in the
                      codebase);

dataset_lock_key(dataset_id) = hash the dataset_id UUID (16 bytes) down to the
                      advisory-lock key space using a stable, documented hash
                      (e.g. the low 64 bits of the UUID's own bytes, or a fixed
                      non-cryptographic hash such as PostgreSQL's own hashtext()
                      applied to the UUID's text form) combined with a distinct
                      "namespace" high bits/second-key value so it can never
                      collide with GLOBAL_BARRIER_KEY or with advisory locks used
                      for an unrelated purpose elsewhere in the codebase.
```

The exact hash function and the two-int vs. single-bigint key encoding are an
SM-503 implementation choice; this ADR only requires that the function be
(a) deterministic per `dataset_id`, (b) namespaced so it cannot collide with
`GLOBAL_BARRIER_KEY` or any unrelated advisory lock, and (c) documented in code
where SM-503 implements it. A theoretical hash collision between two different
`dataset_id`s is an accepted, extremely low-probability MVP trade-off (single
64-bit hash space, single-replica worker) — not a correctness gap this ADR treats
as open, but one SM-503 must note in its own PR description if the chosen hash
width is smaller than 64 bits.

### Partial unique index (defense in depth)

The final, required backstop is a partial unique constraint equivalent to:

```text
UNIQUE (dataset_id) WHERE status IN ('running', 'cancelling') AND dataset_id IS NOT NULL
```

so that even if the advisory-lock arbitration above were ever bypassed by a future
code path, the database itself refuses to let a second dataset-scoped run become
`running`/`cancelling` for the same `dataset_id` — an `UPDATE`/`INSERT` producing a
second such row would fail with a constraint violation instead of silently
committing a second writer. This is a correctness backstop, not a replacement for
the advisory-lock arbitration (which is still required, because a partial unique
index alone offers no equivalent protection for the global-vs-dataset-scoped
barrier, only for dataset-vs-dataset).

> **Rollout amendment (recorded during SM-502 implementation, ADR remains
> Accepted):** this constraint's *contract* is frozen and required before
> `GATE-B5` — that has not changed. Its *physical activation*, however, is
> deliberately **deferred** past both SM-502 and SM-503. SM-502 implemented the
> persistence layer and empirically confirmed, against real PostgreSQL
> integration tests, that B4's still-synchronous public write pipelines
> (`forget.py` in particular, plus `remember.py`/`cognify.py`/`improve.py`)
> create `PipelineRun` rows directly as `RUNNING` and resolve concurrency
> conflicts via an application-level check that runs *after* that insert —
> activating the constraint before those writers are migrated rejects that
> insert before the application's own conflict handling ever runs, breaking
> real, already-tested B4 behavior (see `test_forget_postgres_integration.py`'s
> reentrant/conflict scenarios). SM-503 builds the claimant and advisory-lock
> arbitration described in this section, but **still must not** activate this
> index while any direct-`RUNNING` B4 writer exists — SM-503's claimant and
> this constraint govern the *same* `pipeline_runs` table, and the constraint
> would reject B4's legacy inserts exactly as it did when first attempted in
> SM-502. Physical activation happens only after SM-510 (Cognify),
> SM-511 (Improve), SM-512 (Forget), and SM-513 (Remember) have each moved
> their public write path onto the B5 claimant, at which point a follow-up
> migration adds this index and a real PostgreSQL test proves it, verified as
> a `GATE-B5` requirement. This is a rollout-sequencing decision, not a
> reduction of the invariant: no `RUNNING`/`CANCELLING` same-dataset collision
> is ever considered acceptable in the final B5 runtime.

### Frozen properties

```sql
-- Illustrative shape only; SM-503 owns the literal query, advisory key
-- derivation, and index.
SELECT r.id FROM pipeline_runs r
WHERE r.status = 'queued'
  AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= now())
ORDER BY r.created_at ASC
LIMIT :candidate_batch
FOR UPDATE SKIP LOCKED;
-- for each candidate, in a loop, inside its own subtransaction/savepoint:
--   try advisory locks per the dataset-scoped or global path above;
--   on success: re-validate, UPDATE to running, commit;
--   on failure: skip, try next candidate.
```

- **Ordering:** `created_at ASC` (FIFO by submission time) is the minimum
  determinism required; no priority queue in the MVP.
- **Eligibility:** `queued` **and** (`next_attempt_at` unset or due). Same-dataset
  and global-barrier exclusion is **not** part of the eligibility `WHERE` clause
  anymore (that was the flawed approach) — it is enforced by the advisory-lock
  arbitration and re-validation step, which happens per-candidate, inside the
  window where the relevant lock(s) are held.
- **Commit before I/O:** the claim transaction commits (`status='running'`,
  `worker_id`, `heartbeat_at`, `started_at`, `attempt`) before any step execution,
  provider call, or Neo4j call begins. No row lock and no advisory lock is held
  across an LLM, embedding, HTTP, or Neo4j call — advisory xact locks are released
  at COMMIT, before execution starts.
- **Double claim of the same row is impossible** by `FOR UPDATE SKIP LOCKED`
  (mechanism 1). **Double claim across rows of the same dataset, or a global vs.
  dataset-scoped pair, is prevented by advisory lock arbitration** (mechanism 2),
  **backstopped by the partial unique index** (mechanism 4). These are four
  distinct guarantees, not one restated four times.
- **Fairness:** sufficient for a single-replica worker with
  `WORKER_MAX_CONCURRENT_DATASETS` concurrent claims per poll tick; no fairness
  guarantee is made across datasets beyond FIFO order within eligible candidates,
  **except** the global-barrier starvation guard above, which is a required
  fairness guarantee, not a best-effort one: a pending eligible global run is
  never overtaken by a younger dataset-scoped submission.

---

## E. Same-dataset serialization

Rule: at most one write `PipelineRun` with a non-`NULL` `dataset_id` may be
`running` or `cancelling` for a given `dataset_id` at any time. Reads are unaffected
and always read the current `active_generation` regardless of a concurrent write.

Mechanism: the per-dataset PostgreSQL transaction-level advisory lock acquired
during claim (§D), re-validated against current row state while the lock is held,
backstopped by the partial unique index on `(dataset_id) WHERE status IN
('running','cancelling')` (§D). A same-dataset `NOT EXISTS` check with no lock
arbitration is **not** sufficient by itself — see §D's documented race and why it
is retracted as a standalone mechanism.

`dataset_id` used for serialization is always the **authoritative** UUID already
resolved onto the `PipelineRun` row at submission (§C) — never a slug, never a
value inferred by the worker at claim time. A write pipeline for a not-yet-existing
Dataset must resolve/create that Dataset row (and therefore its authoritative id)
inside the same submission transaction before the run becomes `queued`.

---

## F. Global barrier

Only `Forget Everything` uses `dataset_id = NULL`. A global run and a dataset-scoped
run are mutually exclusive by construction of the advisory-lock arbitration in §D
(shared lock for dataset-scoped claimers, exclusive lock for the global claimer, on
the same `GLOBAL_BARRIER_KEY`):

- while any run with `dataset_id IS NULL` is `running`/`cancelling`, no
  dataset-scoped claim can acquire the shared barrier lock and complete its
  re-validation, so no dataset-scoped `queued` run can be claimed;
- while any dataset-scoped run is `running`/`cancelling`, a global claim attempting
  to acquire the barrier lock in exclusive mode still succeeds at the lock itself
  (shared and exclusive holders are only mutually exclusive with each other while
  actively held during a claim transaction, not with committed row state), so the
  global claim's **re-validation step** (§D step 3 of the global path) is what
  rejects it by observing the dataset-scoped run's committed `running`/`cancelling`
  status — the barrier lock prevents concurrent *claiming*, and the re-validation
  read of committed state prevents claiming *while an already-committed conflicting
  run exists*. Both are required together.

This is enforced entirely inside PostgreSQL via advisory lock arbitration plus
re-validation against committed row state — never via a process-local mutex, never
via an application-level semaphore. A second application instance (even though only
one replica is officially supported, §Explicit Non-Goals) claiming against the same
database would observe the same exclusion, because the guarantee lives in
PostgreSQL transaction-level advisory locks and committed row state, not in worker
memory.

`Forget source` / `Forget dataset` / `Forget dataset memory-only` are dataset-scoped
(`dataset_id` set); only `Forget everything` is global (`dataset_id = NULL`).

**Fairness:** exclusion alone does not prevent a continuous stream of newer
dataset-scoped submissions from starving a pending global run forever — §D's
"Global barrier fairness (starvation guard)" freezes the additional, required
precedence rule (`created_at`-ordered, no dataset-scoped run newer than a pending
eligible global run may be claimed ahead of it) that closes this gap without a
mutex or a scheduler.

---

## G. Worker identity

`worker_id` is an opaque token generated once per process boot:

```text
worker_id = f"wk-{uuid4()}"
```

No hostname, PID, file path, or secret is embedded. It is safe to return in
`GET /api/v1/runs` responses (SM-508) as an opaque correlation token. It is held in
process memory for the process's lifetime and written to every row the worker
claims or heartbeats; it is never used as a lock name or exclusion key by itself —
exclusion is by `pipeline_runs.status` + `dataset_id`/`NULL` (§E, §F), not by
`worker_id` identity.

---

## H. Heartbeat

- **Interval:** the worker updates `heartbeat_at = now()` for every run it currently
  owns and is actively processing, at a fixed cadence independent of
  `WORKER_POLL_INTERVAL_MS` (poll interval governs claim frequency; heartbeat
  interval governs liveness signaling for already-claimed runs). Heartbeat interval
  MUST be materially shorter than `WORKER_STALE_AFTER_SECONDS` — the implementation
  (SM-505) picks a fixed fraction (illustratively, `WORKER_STALE_AFTER_SECONDS / 3`,
  bounded to a sane floor) so that a single missed tick cannot falsely trigger stale
  classification.
- **Update mechanism:** a short, standalone `UPDATE ... WHERE id = :run_id AND
  worker_id = :worker_id` statement, committed immediately — never inside the same
  transaction as a long-running step's provider call, and never holding a
  `SELECT ... FOR UPDATE` open across it.
- **Which states receive heartbeat:** `running` and `cancelling` only. `queued`
  (including `queued` awaiting `next_attempt_at`) never receives heartbeat updates —
  a `queued` run with no active owner is expected to have a stale/absent heartbeat.
- **Relation to `WORKER_STALE_AFTER_SECONDS`:** a run is a stale-recovery candidate
  when `status IN ('running', 'cancelling') AND heartbeat_at < now() -
  WORKER_STALE_AFTER_SECONDS` (§I). This is a read-time classification, never a
  persisted status value.

---

## I. Stale recovery

`stale` is never a persisted enum value (`PipelineRunStatus` keeps exactly the six
existing values). It is always the derived predicate above.

### Recovery reconciles state; it never executes business logic inline

Stale recovery (startup pass or periodic in-process pass, §Startup behavior below)
is a **state-reconciliation** operation only. It never calls into pipeline step
`execute()` code, never talks to an LLM/embedding provider, never writes
`graph_outbox` events, and never touches Neo4j. Its entire job is reading and
writing `pipeline_runs`/`pipeline_steps` rows to move an abandoned run back into a
state the **ordinary** claim path (§D) and the **ordinary** engine (§O) can pick up
normally. Any actual re-execution of business logic happens later, only through a
normal claim, by whichever worker (possibly a different one) next runs the claim
query.

### `RUNNING` stale

Preferred, frozen path: `RUNNING (stale) → QUEUED → normal claim → RUNNING`.
Recovery never transitions a stale run directly back to `running` itself — it only
ever produces `queued` (for the ordinary claim path to pick up) or a terminal
state.

1. Detected by the predicate in §H.
2. `config_fingerprint` is compared against the current process configuration
   fingerprint (§J). Mismatch → transition straight to `failed` with
   `CONFIG_FINGERPRINT_MISMATCH`, `finished_at` set. This is the only
   business-adjacent decision recovery makes, and it is a pure comparison of two
   already-persisted/computed strings — no step code runs.
3. Fingerprint match → reconcile only, no execution:
   - set `status='queued'`;
   - set `next_attempt_at` per the same backoff schedule computed from `attempt`
     (§K) — recovery reuses the automatic-retry scheduling rule, it does not
     invent a separate one;
   - `worker_id`/`heartbeat_at` are left as historical (overwritten by the next
     real claim, per §D/§G) — recovery does not clear them to `NULL`, since a
     `NULL` heartbeat on a `queued` row carries no special meaning (§H already
     defines `queued` as never heartbeating);
   - any `PipelineStep` row left `running` under that run is reset to `queued`
     (never `failed`) so the resumed run's engine — on its *next normal claim* —
     re-executes it per the skip/resume rule (§B); recovery itself does not decide
     success/failure of that step, it only un-sticks it from an ownerless
     `running` state that can never complete on its own;
   - if attempts are already exhausted (§K's ceiling), skip the `queued`
     transition and go straight to `failed` with `WORKER_LOST` instead — recovery
     still does not execute anything, it only recognizes that no further attempt
     is allowed.
4. The row now sits `queued` (or terminal). The **next** worker to run the
   ordinary claim query (§D) claims it exactly like any other `queued` row —
   advisory lock arbitration, re-validation, `attempt` increment, all apply
   identically. Recovery's job ends at `queued`; claim's job (§D) is what actually
   produces the next `running` state.

### `CANCELLING` stale

The cancellation **intent** must never be lost, and recovery must reach a terminal
state (`cancelled`, or in the rare unrecoverable case `failed`) **without
resuming any business work** — it must not silently let the run's steps continue
executing as if cancellation had never been requested.

Frozen mechanism:

1. Recovery **never** transitions a stale `cancelling` run to `queued` or
   `running`. Unlike `RUNNING` stale, there is no "let the ordinary claim path pick
   it up again for further step execution" option — that would mean resuming
   business work under a still-active cancellation intent, which is exactly what
   must never happen.
2. Recovery must also never mark the run `cancelled` **blindly**. A crash can
   catch the orphaned in-flight `PipelineStep` at any point relative to its own
   external effects, and cancellation recovery must not assume "nothing happened"
   without evidence. The orphaned step is classified into exactly one of three
   cases, using only already-persisted PostgreSQL state (never by re-executing the
   step, never by calling out to a provider, Neo4j, or the filesystem to "check" —
   consistent with the step replay/commit boundary contract in §O):

   **A. Provably not committed, or effect+completion were one transaction.** If
   the step's authoritative PostgreSQL mutation and its own
   `PipelineStep.status='succeeded'` transition were designed to commit together
   (the commit-boundary preference in §O), then an orphaned step with no
   `succeeded` row is proof, by that same transactional guarantee, that no
   authoritative effect was committed either — a step that never reached
   `succeeded` under that design never partially applied its business mutation.
   → reset the step to `cancelled`, finalize the run to `cancelled`. This is the
   common case and matches the original draft's behavior, but now stated as a
   *proven* case rather than an assumed default.

   **B. Effect is idempotent/reconcilable.** If the step's own contract (§O) is
   the idempotent-or-reconcilable kind — e.g. it re-derives "did this already
   happen" from durable state (a `graph_outbox` row already written, a storage
   file already present at the expected path, an authoritative row already in the
   expected end state) — recovery invokes **only that step's own minimal,
   already-defined reconciliation check** (not the full pipeline, not the step's
   normal forward-progress `execute()`), to bring durable state to a known-
   consistent point. No new business logic is introduced by this ADR; the step
   implementation must already expose this reconciliation per §O's replay-safety
   contract. → after reconciliation confirms a safe, known state, reset the step
   to `cancelled`, finalize the run to `cancelled`.

   **C. Ambiguous / not reconcilable from persisted state.** If neither A nor B
   can be established — the step is not provably uncommitted, is not of the
   idempotent/reconcilable kind, or its reconciliation check itself cannot
   determine a safe end state — recovery must not guess. → finalize the run to
   `failed` with a stable operational error code (e.g. `CANCEL_RECOVERY_AMBIGUOUS`
   or equivalent), **never** report `cancelled`. A `failed` run in this state is
   still eligible for manual retry (§M), which redoes the step from a clean slate
   rather than trusting an unproven "it was fine to cancel" label.

   This classification applies with particular weight to **filesystem/storage**
   steps (was the delete/write actually applied before the crash?) and to
   **graph projection** steps — for graph projection specifically, the step's own
   job is only to write the `graph_outbox` row durably (§O, §V); once that row is
   committed, ADR-0008's outbox lease/recovery (§V) is the sole mechanism that
   converges Neo4j, and cancellation recovery never writes to Neo4j directly or
   invents a second convergence path — case A or B for a projection step is
   determined entirely by whether the `graph_outbox` row itself was committed
   (A: not committed → nothing to reconcile; B: committed → it is already
   idempotent/reconcilable by ADR-0008's own guarantee, so case C should not
   normally arise for this specific kind of step). Any future step with an
   external, observable effect must be classified the same way when it is
   introduced — this ADR does not enumerate every future case, only the
   principle and today's two concrete kinds (filesystem, graph projection).
3. Steps other than the orphaned in-flight one are unaffected by this
   classification — any step already `succeeded` stays `succeeded` (audit-safe,
   per §B); this section concerns only the one step that was `running` at the
   moment of the crash.

### `PipelineStep RUNNING` stale

A step's own staleness is implied by its owning run's staleness — steps do not
carry an independent heartbeat. Recovery of the run (above) is what re-evaluates
any `running` step under it.

### Startup behavior

On process startup, before the worker begins claiming new work (PRD section 21
startup order: PostgreSQL healthy → Neo4j healthy → app → migrations → Neo4j
constraints → config check → **worker** → readiness true), a startup recovery pass
scans for `RUNNING`/`CANCELLING` runs whose heartbeat already satisfies the stale
predicate (this catches runs abandoned by a crash of the *previous* process, which
by definition cannot still be heartbeating) and applies the same reclassification
above. This is the same code path as periodic in-process stale detection (SM-505
may run it periodically too), not a separate one-off startup-only algorithm.

`created_at` alone is never used as stale evidence (explicitly rejected by backlog
§5.1 and SM-507's "Não fazer") — only `status` + `heartbeat_at`.

---

## J. `config_fingerprint`

- Computed once per process boot from the resolved `Settings` (excluding secret
  values themselves, matching `/api/v1/info`'s existing fingerprint approach) and
  attached to every `PipelineRun` at submission time as the fingerprint **active at
  submission**.
- The check point is exactly the stale-`RUNNING` recovery path (§I step 2): when a
  run is found stale, its stored `config_fingerprint` is compared to the **current**
  process's fingerprint. It is not re-checked on every heartbeat and not re-checked
  on a normal (non-stale) claim, because a run that is actively heartbeating under
  the same process generation was already claimed under a consistent fingerprint.
- Mismatch is unconditional: there is no partial-compatibility exception in the
  MVP. Any difference fails the run safely as `failed` /
  `CONFIG_FINGERPRINT_MISMATCH`.
- New processing under new configuration always happens via a **new** run — either
  a fresh top-level submission or manual retry (§M) — never by mutating the old
  run's fingerprint or resuming it under new config.

---

## K. Automatic retry

- **Retryable** (§X) errors: transient dependency failures (LLM/embedding
  timeout/5xx, Neo4j unavailability, transient PostgreSQL contention surfaced as a
  retryable driver error). Retryable classification is decided by the step
  implementation raising a typed retryable error, not by string-matching messages.
- **Permanent** (§X): validation errors, data errors (e.g. malformed source content
  that will never parse), `STEP_INPUT_DRIFT`, `CONFIG_FINGERPRINT_MISMATCH`,
  anti-hallucination contract violations already enforced by B4 services (e.g.
  cognify's structured-output repair budget exhausted) — these must fail the run,
  not loop.
- **Max attempts:** a fixed per-pipeline-type ceiling on `PipelineRun.attempt`
  (illustratively 5; the exact number is an implementation constant set in SM-504,
  not re-opened by this ADR beyond requiring it exists, is finite, and is the same
  concept driving `next_attempt_at` scheduling below). Exhausting it on a retryable
  error transitions the run to `failed` with the last retryable error's code
  surfaced.
- **Backoff + jitter:** exponential backoff seeded by `PipelineRun.attempt`
  (`base * 2^(attempt-1)`, capped) plus random jitter, written into
  `next_attempt_at = now() + backoff_with_jitter` when the run is set back to
  `queued` (§A). This computation happens once, at the moment of the `running →
  queued` transition, and is itself durable (persisted column) — a restart does not
  lose or reset the scheduled eligibility.
- **Durability:** because eligibility lives in `next_attempt_at` (a persisted
  column checked by the claim query, §D), retry scheduling survives process
  restart with no separate in-memory timer/scheduler.
- **Migration:** `pipeline_runs.next_attempt_at TIMESTAMPTZ NULL`, plus a
  supporting index for the claim query (§5. Migrations, below).

---

## L. Run attempt vs Step attempt

These are deliberately **not** the same counter:

- **`PipelineRun.attempt`**: the number of times this run has been *claimed*
  (`queued → running` transitions). Every reclaim after an automatic retry
  increments it, whether the run failed at step 1 or step 5 of its pipeline.
- **`PipelineStep.attempt`**: the number of times *that specific step* has actually
  executed. A run's second claim (`PipelineRun.attempt = 2`) does not re-execute
  already-`succeeded` steps (§B skip rule), so most steps in a multi-step pipeline
  will show `PipelineStep.attempt = 1` even when `PipelineRun.attempt` is higher —
  only the step that failed (and any step after it that never ran) accumulates
  additional attempts.

Backoff/jitter (§K) is computed from `PipelineRun.attempt`, because the unit of
scheduling and retry-ceiling enforcement is the run, not an individual step —
this keeps the eligibility computation in one place (the claim query) instead of
requiring per-step scheduling state.

---

## M. Manual retry

`POST /api/v1/runs/{run_id}/retry` (SM-514) creates a **new** `PipelineRun`;
the original run is never mutated back to `queued`/`running`.

- **Allowed source states:** `failed` and `cancelled` only. `queued`, `running`,
  `cancelling` return a stable conflict error (`RUN_NOT_RETRYABLE`, `409`) — there
  is nothing to retry while work is still outstanding or already succeeded.
  `succeeded` also returns `RUN_NOT_RETRYABLE`; retrying success is not a supported
  operation (a fresh submission is the correct action if genuinely new work is
  wanted).
- **Vínculo persisted:** new run's `retry_of_run_id = original_run.id`. The original
  row is untouched (immutable history) — no field on it is rewritten to point
  forward; the forward link only exists from the new row backward, which is
  sufficient for audit and keeps the original row's history free of retroactive
  edits.
- **New run's identity:** same `pipeline_type`, `dataset_id`, `input`, and
  `payload_hash` as the original (retry does not let the caller redefine the work);
  a fresh `idempotency_key` is used **internally** for the new row's own
  idempotency slot, independent of any client-supplied `Idempotency-Key` header on
  the retry request itself.
- **Reserved internal namespace:** internal keys generated by this ADR's own
  mechanisms (manual retry, and any future internal use) always use the prefix
  `sys:` — e.g. `f"sys:retry:{original_run.id}"` — which is a **reserved
  namespace**: submission (§C) must reject any client-supplied `Idempotency-Key`
  header that starts with `sys:` with a stable `400`
  (`RESERVED_IDEMPOTENCY_KEY_NAMESPACE` or equivalent) before it ever reaches the
  lookup/insert step, so an internal key can never collide with a caller-supplied
  one and a caller can never forge or hijack an internal retry slot. Public
  `Idempotency-Key` values are therefore, by construction, always disjoint from
  every internal key this ADR generates.
- **Concurrent `retry(A)` calls converge to the same new run:** two concurrent
  `POST /api/v1/runs/A/retry` requests both attempt to `INSERT` a `PipelineRun`
  with `idempotency_key = "sys:retry:A"`. The existing
  `uq_pipeline_runs_idempotency_key` partial unique index (already present in the
  current schema, no new constraint required for this specific guarantee) makes
  the second `INSERT` fail with a unique violation; the losing request catches that
  violation and `SELECT`s the winner's row instead of creating a second run. This
  is the same pattern already required for ordinary submission's Idempotency-Key
  handling (§C step 3) — retry reuses it rather than inventing a second mechanism.
  Both concurrent callers observe the same `retry_of_run_id = A` run, call it `B`.
- **Retrying a retry:** if `B` later also ends in `failed`/`cancelled`, a new
  `POST /api/v1/runs/B/retry` creates `C` with `retry_of_run_id = B`, using
  `idempotency_key = "sys:retry:B"`. History is a simple backward-linked chain
  (`C.retry_of_run_id = B.id`, `B.retry_of_run_id = A.id`), each link immutable
  once written; there is no cap on chain length and no collapsing of the chain —
  full lineage stays auditable by following `retry_of_run_id` backward from any
  run.
- **Steps of the new run:** materialized fresh at the new run's own submission
  (§C/§B — the manual-retry endpoint performs the same durable submission
  transaction as any other run creation); the new run does not inherit the old
  run's `PipelineStep` rows. It re-executes from step 1 — manual retry is a full
  redo, not a resume, because the operator explicitly asked to retry after the
  automatic retry budget/behavior was exhausted or the run was cancelled; resuming
  from a possibly-inconsistent partial state without re-validation is not assumed
  safe.
- **`attempt` on the new run:** starts at `0`, identical to any fresh submission.
- **Side effects already committed** by the original run (e.g. a source already
  marked `deleted` by a Forget run that later failed on a later step) are not
  undone and not reapplied by the new run beyond what its own steps' skip/resume
  logic would naturally do if it inherited state — since manual retry does *not*
  inherit `PipelineStep` rows, each of its steps must itself be idempotent against
  already-applied PostgreSQL state (an existing B4 invariant carried forward, not a
  new one).

---

## N. Cancellation

- `queued → cancelled`: immediate, no worker involvement required — a `queued` run
  with no owner can be finalized directly by the cancel endpoint via a guarded
  `UPDATE ... WHERE status='queued'`.
- `running → cancelling`: the cancel endpoint sets `status='cancelling'`
  unconditionally for a currently `running` owned run; it does not wait for the
  worker.
- `cancelling → cancelled`: only the worker performs this transition, at the next
  **safe checkpoint** — defined as the boundary between two `PipelineStep`
  executions (§B), never mid-step.
- **Never** interrupted mid-transaction: a step that is inside a critical
  PostgreSQL write transaction (e.g. persisting Forget's authoritative deletes plus
  its `graph_outbox` events in one transaction) always completes that transaction
  before the engine re-checks cancellation. Cancellation is checked *before starting*
  the next step, never injected into an in-flight one.
- **During a provider call** (LLM/embedding/HTTP): the in-flight call is allowed to
  finish (or fail on its own timeout); cancellation is observed at the next
  checkpoint after the call returns, not by aborting the HTTP call itself. This
  avoids leaving an ambiguous partial provider interaction.
- **During retry backoff:** a `queued` run awaiting `next_attempt_at` can be
  cancelled directly via the `queued → cancelled` path above (it has no active
  worker to interrupt).
- **During graph projection:** projection (an outbox-driven step or the autonomous
  processor, §V) is not interrupted mid-command; a `cancelling` run still lets
  in-flight `graph_outbox` rows reach `done`/`failed` normally — outbox rows are
  independent lifecycle from the run that created them once written (§V), so
  cancelling the run does not roll back or orphan outbox events already committed.
- **During storage deletion** (Forget): a storage delete is treated as its own
  step-scoped unit; cancellation is observed before or after that step, never
  mid-delete.
- **Recovery of a crashed `cancelling` run:** covered by §I (`CANCELLING` stale).

---

## O. Pipeline engine

```text
registry:   closed, code-defined map of PipelineType (+ implicit version via code)
            -> ordered list of step definitions. No client-supplied pipeline.
context:    PipelineContext carries run_id, dataset_id, resolved Settings-derived
            handles (session factory, Neo4j resource, LLM/embedding clients),
            and the current step's persisted input/output — not raw secrets.
steps:      PipelineStep Protocol per PRD section 14.3:
              async def execute(context) -> StepResult
              async def compensate(context, result) -> None
```

- Only pipelines internal to `sofias_memory/pipelines/` are executable; no dynamic
  import driven by request data (existing AGENTS.md/PRD invariant, restated here
  only as its operational consequence: the registry key space is fixed at process
  start).
- **One top-level public request = one top-level `PipelineRun`.** Composition of a
  multi-phase product operation (e.g. Remember/full) happens as multiple steps
  inside that single run, never as a nested top-level run of a different
  `pipeline_type` (§P).
- `compensate` exists per the PRD's engine contract but is used only when a step
  cannot leave a safely-abandonable partial artifact; the preferred pattern (per PRD
  14.3) remains versioned artifacts + later cleanup (e.g. an unreferenced new
  `generation` simply stays inactive forever rather than requiring compensating
  deletes). `compensate` is not invoked by cancellation (§N) — cancellation stops
  *before* the next step, it does not roll back the step that already committed.
- Step execution never holds a PostgreSQL transaction open across a provider or
  Neo4j call (same rule as claim, §D) — a step that needs to persist results opens
  a short transaction after the external call returns.

### Step replay and commit boundary contract

Every step subject to automatic retry/resume (i.e. every step, since §K/§B make
resume the default recovery path for a retryable failure) must be **replay-safe**:
it may only be re-executed automatically when at least one of the following holds:

1. **its effects are naturally idempotent** — re-running it with the same input
   produces the same authoritative state as running it once (e.g. an upsert keyed
   on a stable id); or
2. **durable state exists to prove/reconcile the prior attempt's effect** before
   redoing it — the step's own re-execution first checks PostgreSQL for evidence of
   what the previous attempt already committed, and reconciles instead of blindly
   repeating a side effect that would otherwise duplicate.

A step that meets neither condition must not be marked retryable; it must fail
permanently (§X, "Permanent request/data") rather than risk a silent duplicate
effect on retry.

**Commit boundary preference:** whenever a step mutates authoritative PostgreSQL
state, the business mutation and that step's own `PipelineStep` row transition to
`succeeded` (with its `output`/`metrics`) should be committed in the **same**
PostgreSQL transaction whenever architecturally possible. This closes the gap
where a crash between "business state committed" and "step marked succeeded"
would otherwise make the skip/resume rule (§B) re-execute a step whose effect
already landed — the two facts land together, so resume's `status = succeeded`
check is trustworthy evidence that the business effect is also already
committed. As with every other rule in this ADR, this never extends to holding
that transaction open across a provider or Neo4j call (§D, §O) — it applies only
to the step's own PostgreSQL write, after any external call has already returned.

**External effects** (effects outside a single PostgreSQL transaction) need their
own idempotent/recovery strategy per kind, not a generic saga framework:

- **filesystem** (source storage writes/deletes): idempotent by construction —
  writing the same bytes to the same path, or deleting an already-absent path, is
  a safe no-op/overwrite; the authoritative record of what *should* exist is
  always PostgreSQL (existing B4 invariant, unchanged).
- **Neo4j**: never mutated directly by a pipeline step; all graph-affecting change
  goes through `graph_outbox` (ADR-0008), whose own idempotent upsert/delete
  semantics and lease/recovery (§V) are the recovery strategy — a step's job is
  only to write the outbox row durably alongside its business mutation, per the
  commit-boundary preference above.
- **provider calls with an observable side effect** (rare in this codebase's
  current LLM/embedding usage, which is read-only against the provider): a step
  making such a call must record enough durable evidence of "did I already do
  this" before retrying, following the same idempotent-or-reconcilable rule above;
  no such step exists in B4's inherited pipelines today, so this is a constraint
  on any future step, not a retrofit.

This ADR does not introduce a generic saga/compensation framework. `compensate()`
(above) remains an exceptional tool for a step whose partial artifact truly cannot
be left abandoned safely — it is not a requirement for every step, and it is not
the mechanism replay-safety relies on; replay-safety relies on idempotency or
reconciliation, described above, which is a property of *re-running forward*, not
of rolling back.

---

## P. Remember/full

```text
REMEMBER (top-level PipelineRun, PipelineType.REMEMBER)
  step: validate_request
  step: resolve_or_create_dataset
  step: persist_source
  step: extract_text
  step: normalize_document
  step: chunk_document
  step: embed_chunks
  step: summarize_chunks
  step: extract_graph
  step: resolve_entities
  step: persist_graph_records
  step: project_to_neo4j        (writes graph_outbox; §V owns delivery after this)
  step: summarize_document
  step: activate_generation
  step: finalize_run
```

This is the PRD section 15.1 sequence, executed as steps of one `REMEMBER` run —
never as a `REMEMBER` run that enqueues and awaits a second top-level `COGNIFY`
run. `POST /api/v1/cognify` standalone continues to create its own independent
top-level `PipelineType.COGNIFY` run reusing the same cognify-related step
implementations (shared step code, not a shared run).

---

## Q. `wait=false`

1. Submission (§C) commits durably.
2. Response: HTTP `202 Accepted` with `{run_id, status}` (`status` reflects the row
   at response time — `queued`, or `running` if the worker already claimed it
   before the response was built, which is a benign race with no incorrect
   observable state either way).
3. No blocking wait of any kind on the API request path.

---

## R. `wait=true`

1. Submission (§C) commits durably — identical to `wait=false`; `wait` is decided
   purely by the caller after the same durable enqueue.
2. **Observation authority:** the persisted `PipelineRun` row in PostgreSQL is the
   only source of truth for whether the run has reached a terminal state.
   `wait=true` must never depend on an in-memory `asyncio.Queue`/`asyncio.Event`/
   process-local signal as its **sole** source of completion — the run may have
   been claimed and finished by a different code path (a different worker
   iteration, or, in a future multi-process scenario, a different process
   entirely) than whatever object is holding a local signal. A concrete
   implementation (SM-509's) may use a local signal purely as a **latency
   optimization** (wake up a poll early instead of waiting for the next tick), but
   losing that signal — a missed notification, or the handler's own process being
   different from the one that flips the run terminal — must never change the
   observed outcome: the handler must always be able to fall back to polling
   `pipeline_runs` directly and get the correct answer. Concretely: short interval
   polling against `pipeline_runs` (optionally short-circuited by a local signal
   as an optimization) is required as the baseline mechanism; a pure in-memory
   signal with no PostgreSQL fallback is not an acceptable implementation of this
   contract.
3. Bounded by `REQUEST_WAIT_TIMEOUT_SECONDS`.
4. If the run reaches a terminal state before timeout: return the terminal result
   inline (200-class response carrying the same public result shape the run would
   expose via `GET /api/v1/runs/{run_id}`, per each endpoint's contract).
5. If the run has not reached a terminal state at timeout: return `202 Accepted`
   with the same `run_id` and current status — never an error, never a fabricated
   partial success.
6. The run keeps executing regardless of the timeout; HTTP client disconnect or
   handler timeout **never** cancels the run. Only an explicit
   `POST /api/v1/runs/{run_id}/cancel` (§N) cancels it.

---

## S. Idempotency

### Key vs. hash — two different jobs

`Idempotency-Key` (caller-supplied, optional) is the **lookup identity**. When
present, it is what submission (§C step 3) uses to find an existing
`PipelineRun` for this exact key, via the existing
`uq_pipeline_runs_idempotency_key` partial unique index.

`payload_hash` (`CHAR(64)` sha256 of the canonical request payload, always
computed, never optional) is **not** a second identity namespace layered on top of
the key. It never causes two requests with the *same* `Idempotency-Key` to resolve
to two different runs. Its job is narrower: it is a **mismatch guard** — proof that
a given `Idempotency-Key` is always being reused for the *same* logical work, never
silently reused for different work.

`wait` is excluded from both the lookup identity and the hash — it is a
response-shape preference, not part of the work (unchanged from the earlier
draft).

### Contract

```text
Idempotency-Key K + payload_hash A            -> creates Run X (payload_hash A stored on X)
Idempotency-Key K + payload_hash A (again)     -> resolves the SAME Run X (hash matches)
Idempotency-Key K + payload_hash B (different) -> 409 IDEMPOTENCY_CONFLICT;
                                                    Run X is NOT mutated, NOT
                                                    re-resolved, NOT superseded;
                                                    caller must use a different
                                                    Idempotency-Key for genuinely
                                                    different work.
```

```text
request A: same operation + wait=false + Idempotency-Key K -> creates/resolves Run X
request B: same operation + wait=true  + Idempotency-Key K -> resolves the same Run X,
                                                                 only chooses to wait on it
```

Without an `Idempotency-Key`, `payload_hash` alone never deduplicates a write —
each such request creates a new `PipelineRun`, unchanged from existing B4
behavior. `payload_hash` only becomes a guard once paired with a caller-supplied
key; it is not a generic content-addressed dedup mechanism on its own.

Behavior when an existing run is found for the same `Idempotency-Key` (hash
matching, per the contract above — a hash mismatch always short-circuits to `409`
before any of the following applies):

| Existing run status | Behavior |
|---|---|
| `queued` | resolve to it; `wait=false` responds `202` immediately; `wait=true` waits on it |
| `running` | same as `queued` |
| `succeeded` | resolve to it; return its terminal result again (safe replay), no new run |
| `failed` | resolve to it; return its terminal failure again; caller must use manual retry (§M) to get a new attempt — resubmitting with the same key does not silently create a second run |
| `cancelling` | resolve to it; behaves like `running` (still in flight) |
| `cancelled` | resolve to it; return the cancelled terminal state again; manual retry (§M) is the path to a new run |

A request without an `Idempotency-Key` always creates a new `PipelineRun` (no
implicit dedup by payload alone) — this matches existing B4 behavior and is not
changed by B5.

---

## T. Worker lifecycle

### Startup

Per PRD section 21.1, worker start is step 7 of 8, after migrations and Neo4j
constraints are confirmed and before readiness flips true:

```text
PostgreSQL healthy -> Neo4j healthy -> app -> migrations -> Neo4j constraints
  -> config check -> worker start (+ startup stale recovery, §I) -> readiness true
```

The worker does not start (and readiness never becomes true for the worker
component) if `WORKER_ENABLED=false` (§U) or if any prerequisite above failed.

### Shutdown

Per PRD section 21.1:

```text
stop accepting new claims -> let in-flight step(s) reach their safe checkpoint
  -> persist final heartbeat/status -> close DB/Neo4j connections -> stop worker
  -> API shuts down
```

Concretely: the worker's poll loop stops issuing new claim queries immediately on
shutdown signal; runs already `running` are given a bounded grace period to reach
their next safe checkpoint (§N's checkpoint definition — between steps); a run
still mid-step when the grace period elapses is left as-is (its `heartbeat_at`
simply stops advancing) and is picked up by stale recovery (§I) on the next
process's startup — shutdown never force-fails a run just to make shutdown appear
clean, because that would misrepresent unknown-outcome work as a definite failure.
No `asyncio.Task` is abandoned unawaited: every claimed run's execution task is
tracked and awaited (with the grace-period bound) before process exit.

---

## U. `WORKER_ENABLED`

`WORKER_ENABLED=false`:

- the worker component does not start at all (no polling, no claiming);
- reads (Recall, Graph/Provenance, Runs list/get) remain available whenever their
  own dependencies (PostgreSQL, Neo4j) are healthy — reads never depend on the
  worker;
- any write that requires the B5 runtime (i.e. any write migrated off the B4
  synchronous path by SM-510..SM-513/SM-515) returns `503` with a stable error code
  (`WORKER_DISABLED` or equivalent), submitting **nothing** — no `PipelineRun` row
  is created for a request that cannot be serviced, so a later
  `WORKER_ENABLED=true` restart never finds phantom queued work from a disabled
  period;
- `/health/live` is unaffected (process-alive only, per ADR-0008's `/health/live`
  rule extended here to the worker);
- `/health/ready` is `false` while any write-capable pipeline type is expected to
  run through the disabled worker — i.e. readiness reflects that the operational
  runtime required by the product is not available, per PRD FR-120.
- There is no synchronous fallback engine. `WORKER_ENABLED=false` is an explicit
  degraded/read-only mode, not a second code path that reimplements the writes
  inline.

---

## V. Graph outbox recovery

ADR-0008 remains the sole authority for `graph_outbox` payload shape, aggregate
identity, upsert/delete semantics, and idempotent replay. This ADR only adds the
operational lifecycle needed to recover a `processing` row that was never marked
`done`/`failed` because its worker died mid-projection.

### Migration

```text
graph_outbox.processing_started_at TIMESTAMPTZ NULL
graph_outbox.worker_id             TEXT NULL
```

### Claim

An autonomous processor (SM-506, running inside the same internal worker
coordinator, not a separate process) claims eligible `graph_outbox` rows with the
same `FOR UPDATE SKIP LOCKED` pattern used for `pipeline_runs`:

```sql
SELECT id FROM graph_outbox
WHERE status = 'pending'
   OR (status = 'processing' AND processing_started_at < now() - :outbox_stale_after)
   OR (status = 'failed' AND attempt < :max_outbox_attempts)
ORDER BY id ASC
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
-- then UPDATE ... SET status='processing', processing_started_at=now(), worker_id=:worker_id, attempt=attempt+1
-- committed before the Neo4j apply call.
```

- **Lease/ownership:** `processing_started_at` + `worker_id` together are the lease;
  a row is stale-processing when
  `status='processing' AND processing_started_at < now() - outbox_stale_after`
  (an operational threshold, not necessarily identical in value to
  `WORKER_STALE_AFTER_SECONDS`, but the same *kind* of derived-staleness pattern as
  §H/§I — no new persisted `stale` status).
- **Crash after Neo4j apply, before `mark_done`:** the row is still `processing`
  (stale-eligible) or already reclaimed; reprocessing it replays the same
  ADR-0008 idempotent upsert/delete, which converges to the same Neo4j state
  (`MERGE`-equivalent semantics, ADR-0008) — safe by construction, not by luck.
- **Dependency order:** unchanged from ADR-0008's rebuild order
  (`Entity → Chunk → MENTIONED_IN → RELATES_TO → NEXT`); the autonomous processor
  processes outbox rows in `id` order (creation order), which already respects
  emission order from the originating transaction and therefore respects the same
  dependency order the originating pipeline wrote them in.
- **Idempotency:** identical guarantee as ADR-0008 §"Idempotency and Replay" —
  duplicate delivery, retry after transient Neo4j failure, and rebuild-after-loss
  are all safe. This ADR does not weaken or duplicate that guarantee, only gives it
  an autonomous trigger instead of requiring an explicit drain call.
- **Relation to explicit drain:** `GraphOutboxBatchProcessor`'s explicit
  per-dataset drain (used today by B4 pipelines at the end of a write) continues to
  exist and continues to be called at the natural end of a pipeline's
  `project_to_neo4j` step, for low-latency projection. The autonomous processor is
  the safety net for rows an explicit drain never got to reach (crash before drain,
  or a row left behind by a partially-completed run) — the two are complementary,
  not competing; a row processed by one is simply `done`/`failed` and invisible to
  the other.

---

## W. Worker concurrency

`WORKER_MAX_CONCURRENT_DATASETS` bounds how many distinct-dataset writes the single
worker process may have claimed and executing at once. It is a concurrency limit,
not a durability mechanism. Even with exactly one application replica:

- runs for different datasets may execute concurrently, up to the configured limit;
- the same dataset can never have two writes `running` simultaneously (§E) —
  this is a correctness invariant, not merely a scheduling preference, and is
  enforced at the PostgreSQL claim level regardless of the concurrency limit's
  value.

`WORKER_MAX_CONCURRENT_READS` is unrelated to this ADR's write lifecycle; it
continues to bound read-path concurrency and is out of scope here.

---

## X. Failures and classification

Minimal operational categories (not a general-purpose taxonomy):

| Category | Examples | Run outcome |
|---|---|---|
| Retryable dependency/transient | LLM/embedding timeout or 5xx, Neo4j connection failure, transient PostgreSQL contention | `running → queued` with backoff (§K), until attempts exhausted |
| Permanent request/data | invalid source content that will never parse, anti-hallucination contract violation after repair budget exhausted, `STEP_INPUT_DRIFT` | `running → failed`, no retry scheduled |
| Cancellation | explicit cancel request observed at checkpoint | `running/cancelling → cancelled` |
| Config mismatch | `config_fingerprint` differs at stale recovery | `→ failed` (`CONFIG_FINGERPRINT_MISMATCH`), no auto-resume |
| Worker shutdown/recovery | process death, graceful shutdown grace period elapsed | `running → queued` (if fingerprint matches and attempts remain) or `→ failed` (`WORKER_LOST`, attempts exhausted) via stale recovery, never silently "succeeded" |

`error_code` values are stable strings suitable for public API responses (SM-508);
`error_message` is a safe, non-sensitive summary. Neither ever contains a raw
provider payload, stack trace, secret, or internal path, consistent with the
existing AGENTS.md/PRD redaction rules — restated here only as the specific set of
codes this ADR introduces (`CONFIG_FINGERPRINT_MISMATCH`, `STEP_INPUT_DRIFT`,
`WORKER_LOST`, `RUN_NOT_RETRYABLE`, `WORKER_DISABLED`), not a full public error
catalog (that belongs to SM-508/SM-516's OpenAPI work).

---

## Migrations expected

This ADR does not create a migration. It freezes what SM-502/SM-506 must add:

### `pipeline_runs`

- `next_attempt_at TIMESTAMPTZ NULL` — durable automatic-retry eligibility (§K).
- `retry_of_run_id UUID NULL REFERENCES pipeline_runs(id)` — manual-retry lineage
  (§M).
- **`UNIQUE (dataset_id) WHERE status IN ('running', 'cancelling') AND dataset_id
  IS NOT NULL`** (partial unique index) — the defense-in-depth backstop for
  same-dataset serialization (§D, §E); required before `GATE-B5`, but **not**
  part of SM-502's migration (rollout amendment, §D) — added by a follow-up
  migration after SM-513, once no direct-`RUNNING` B4 writer remains.
- Index supporting the claim query's eligibility predicate, e.g.
  `(status, next_attempt_at)` and confirmation that the existing
  `ix_pipeline_runs_dataset_id_status` and `ix_pipeline_runs_status` indexes remain
  sufficient for the re-validation reads inside the advisory-lock arbitration
  (§D); further index tuning may be justified by SM-503's real query-plan testing
  rather than assumed here.
- No new `PipelineRunStatus`/`PipelineStepStatus` enum values (`stale` is derived,
  never persisted).
- No migration required for advisory lock keys (§D) — PostgreSQL advisory locks
  are not schema objects; the key derivation is a code-level constant/function in
  SM-503.
- No migration required for the manual-retry concurrency guarantee (§M) — it
  reuses the existing `uq_pipeline_runs_idempotency_key` partial unique index,
  already present in the current schema.

### `graph_outbox`

- `processing_started_at TIMESTAMPTZ NULL` — lease timestamp (§V).
- `worker_id TEXT NULL` — lease owner (§V).
- Index supporting stale-processing recovery, e.g.
  `(status, processing_started_at)`.

### `pipeline_type`

- **Not** modified by this ADR. `DATASET_DELETE` is explicitly deferred to
  ADR-0010/SM-515 (backlog §5.15, §5.17); adding it here would be scope creep this
  ADR must not introduce.

No other column, table, or enum change is justified by the decisions above. If
SM-502/SM-503 implementation discovers a genuine additional need (e.g. a
composite index proving necessary under real query-plan testing), that is recorded
against the relevant story, not retrofitted into this ADR after acceptance without
a revision.

---

## Testability contract

The following behaviors must be provable by real PostgreSQL/Neo4j integration
tests across SM-502 through SM-514 before `GATE-B5` can be attempted:

- double claim of the same `pipeline_runs` row by two concurrent claimers is
  impossible (§D, mechanism 1);
- **cross-row same-dataset claim race:** two concurrent claimers targeting
  *different* `queued` rows of the *same* `dataset_id` never both reach `running`
  — this must be tested as its own scenario (two distinct rows, not one), since it
  is exactly the race a naive `NOT EXISTS`-only claim query fails to close (§D);
- same-dataset serialization: two writes for one dataset never observe `running`
  simultaneously (§E), including under the partial unique index backstop (§D) —
  a test should also confirm the constraint itself rejects a second concurrent
  `running`/`cancelling` row if the advisory-lock path were bypassed;
- cross-dataset concurrency: writes for distinct datasets can execute concurrently
  up to `WORKER_MAX_CONCURRENT_DATASETS` (§W);
- global barrier: a global run and a dataset-scoped write are never both
  `running`/`cancelling` at once (§F);
- global barrier fairness: under a continuous stream of new dataset-scoped
  submissions, a pending eligible global run is claimed once currently-running
  dataset-scoped work drains, and is never starved indefinitely by younger
  dataset-scoped writes jumping ahead of it (§D);
- durable retry: a killed worker process does not lose a scheduled
  `next_attempt_at`, and the retry still fires correctly after restart (§K);
- stale `RUNNING` recovery: a killed process's claimed run is correctly
  reclassified (resumed or failed) by a fresh process, without a healthy run being
  touched (§I);
- stale `CANCELLING` recovery: cancellation intent survives a crash and still
  converges to `cancelled` (never silently reverts to `running`) (§I);
- `config_fingerprint` mismatch: a stale run recovered under different
  configuration fails safely and is never silently resumed (§J);
- graceful shutdown: no orphan `asyncio.Task`, in-flight step reaches its safe
  checkpoint or is left for the next process's stale recovery — never marked
  falsely successful (§T);
- `wait=true` timeout returns `202` with the same `run_id` and does not cancel or
  duplicate the run (§R);
- idempotency: `wait=false` and `wait=true` against the same `Idempotency-Key`
  resolve to the exact same `PipelineRun` (§S);
- manual retry creates a new `PipelineRun` linked via `retry_of_run_id`, and the
  original run's row is never mutated (§M);
- concurrent manual retry: two simultaneous `POST /runs/{id}/retry` calls for the
  same run converge to the same new run, never creating two (§M);
- `PipelineStep` rows exist for a `queued` run immediately after submission,
  before any claim (§B/§C);
- stale `RUNNING` recovery never leaves a run directly `running` — it always
  produces `queued` (for a subsequent normal claim) or a terminal state, and never
  itself executes step business logic (§I);
- stale `CANCELLING` recovery classifies the orphaned in-flight step correctly
  into provably-uncommitted (A), reconcilable (B), or ambiguous (C) per §I, never
  reports `cancelled` for case C (must be `failed`/`CANCEL_RECOVERY_AMBIGUOUS`
  instead), and never resumes normal step execution in any case;
- `graph_outbox` stale `processing` recovery: an abandoned lease is reclaimed and
  reprocessed idempotently without duplicating or corrupting the Neo4j projection
  (§V).

---

## Explicit Non-Goals

This ADR does not design, and SM-502..SM-516 must not implement ahead of their own
story, or ever for items reserved beyond B5:

- Administrative Dataset DELETE semantics (tombstone, reuse of slug, response
  shape) — `ADR-0010`, before SM-515;
- frontend;
- MCP;
- multi-replica coordination, leader election, or distributed locking;
- high availability;
- an external queue (Redis/Celery/RabbitMQ/Kafka/SQS or any other);
- a product-facing cron/scheduler (auto-Improve or otherwise);
- distributed tracing;
- arbitrary client-supplied pipelines;
- users/tenant/ACL/roles/permissions (inherited invariant, not reopened);
- a database provider registry or alternative PostgreSQL/Neo4j providers.

## Consequences

Implementing SM-502 onward becomes mechanical against this contract instead of
ad hoc: claim, serialization, heartbeat, stale recovery, retry, cancellation, and
`wait` semantics are all specified before any worker code exists. The two new
`pipeline_runs` columns and two new `graph_outbox` columns are the schema surface
SM-502/SM-506 commit B5 to immediately; the `pipeline_runs` partial unique index
is committed to the same degree architecturally, but its physical activation is
deferred to a follow-up migration after SM-513 (rollout amendment, §D) — everything
else is behavior on the existing schema. Same-dataset and global-barrier exclusion
require SM-503 to implement PostgreSQL transaction-level advisory lock arbitration
correctly (§D) — this is more implementation surface than a `NOT EXISTS`-only
approach would have been, which is the direct cost of closing the race that
approach left open; SM-503's advisory-lock logic must therefore stand on its own
correctness during the interval before the partial unique index is activated,
without a schema-enforced backstop yet in place. `ADR-0010` remains a deliberately
separate, smaller decision closer to its own implementation (SM-515), keeping this
ADR focused on the shared runtime every other B5 story depends on.

## Alternatives Rejected

- **`NOT EXISTS` subquery inside the claim statement, with no additional
  arbitration, as the sole same-dataset/global-barrier mechanism:** this was this
  ADR's own first-draft approach and is rejected/retracted (§D) — it only
  prevents two claimers from taking the *same* row (via `FOR UPDATE SKIP LOCKED`),
  it does not prevent two claimers from taking *different* rows of the same
  dataset concurrently, because both can observe "no conflict" before either
  commits. Replaced by PostgreSQL transaction-level advisory lock arbitration
  (§D), backstopped by a partial unique index (§D).
- **Deferring `PipelineStep` materialization to first claim:** this was also this
  ADR's own first-draft approach and is rejected/retracted (§B/§C) in favor of
  materializing all steps, `queued`, in the same transaction as the `PipelineRun`
  — see §B for the four reasons (durable plan, pre-claim observability, auditable
  `queued` cancellation, drift detection).
- **`payload_hash` as a second identity namespace alongside `Idempotency-Key`:**
  rejected; conflating "lookup identity" and "mismatch guard" into one concept
  would make it ambiguous whether a hash difference should create a new run
  (breaking the key's promise of resolving to one run) or be silently ignored
  (defeating the guard's purpose). Keeping them as two named, separately-defined
  concepts (§S) resolves that ambiguity by construction.
- **External queue (Redis/Celery/RabbitMQ/Kafka/SQS):** rejected per B5 invariants
  and ADR-0001's modular-monolith direction; PostgreSQL already has the durability,
  transactional co-location with authoritative writes, and `FOR UPDATE SKIP LOCKED`
  needed for a single-replica MVP worker.
- **`asyncio.create_task` + in-memory queue as the durability boundary:** rejected;
  it does not survive process restart, directly violating "no essential recovery
  state may exist only in process memory" and the explicit Cognee-upstream audit
  finding in the backlog (§3.4) that this pattern is a UX convenience, not a
  durability mechanism.
- **Process-local `asyncio.Lock`/semaphore as the source of dataset-exclusion
  truth:** rejected for the same reason the Cognee upstream audit flags it — it
  does not protect across process restart or (even though only one replica is
  officially supported) across more than one process observing the same database.
- **New `PipelineRunStatus.STALE` enum value:** rejected; `stale` is a derived
  read-time classification (`status` + `heartbeat_at`), keeping the state machine
  in §A small and avoiding a status that itself would need its own transition
  rules into and out of.
- **Per-step independent retry scheduling (a `next_attempt_at` on
  `PipelineStep` instead of `PipelineRun`):** rejected in favor of run-level
  scheduling (§K/§L); it keeps the claim query's eligibility check anchored to one
  table (`pipeline_runs`) and avoids a second retry-scheduling authority that could
  drift from the run-level attempt ceiling.
- **Manual retry mutating/reopening the original run:** rejected; it would break
  the "terminal states are immutable" property relied on by idempotency (§S) and
  audit history, and was already explicitly decided against in backlog §5.8.
