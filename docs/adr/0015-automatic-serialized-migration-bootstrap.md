# ADR-0015: Automatic Serialized Migration Bootstrap

## Status

accepted

## Context

Since the PostgreSQL foundation (B2) and reaffirmed explicitly by ADR-0011 D31/D32, Sofias Memory's schema-migration contract has been: Alembic is the sole schema authority; migrations are applied **explicitly and manually** by an operator running `alembic upgrade head`; the application **never** invokes Alembic itself. If the schema is absent, behind, or otherwise not confirmed current for the running application version, the process stays in `BOOTSTRAP_MAINTENANCE` — `/health/live` available, `/health/ready = NOT_READY` — indefinitely, until the operator migrates out-of-band. This is deliberate, reviewed architecture, not an oversight: ADR-0011 D32 freezes it as one of its own numbered decisions (decision 42 of that ADR's frozen list), and `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` enforces it structurally against both Compose files.

That contract has a real, predictable operational cost: every release that adds one or more migrations requires an operator to remember this fact, find the right release note, and run a manual out-of-band command before or immediately after redeploying — a step with no automated safeguard against being forgotten. `docs/deployment/easypanel.md`'s own upgrade history already documents one real incident of exactly this: "An earlier release advanced the image without applying its migration first, leaving the API fail-closed... until the schema caught up." The goal of this ADR is to remove that specific human-error surface — an operator should not need to remember and manually run Alembic for an ordinary upgrade — without weakening any of the safety properties the explicit-migration contract exists to protect: fail-closed startup, PostgreSQL as sole schema authority, concurrency safety, rollback safety, and portability across this project's actual supported deployment targets.

This ADR is **not** a decision to remove Alembic, hide migration failures, or make schema evolution implicit. It formalizes a **narrow, serialized, fail-closed automatic bootstrap** that does exactly what an operator does today — runs `alembic upgrade head` — at a precisely defined, safe point in the application's own existing startup sequence, under a lock, with strict state verification before and after, and with an explicit opt-out that reproduces today's contract exactly.

A read-only architecture discovery pass (this session, baseline HEAD `40062973c071bdf286705dfa15755cdfab53869c`) evaluated four candidate architectures (a Compose one-shot migration service, an image-level entrypoint bootstrap, a FastAPI-lifespan-based bootstrap, and a manual wrapper command) against this project's actual code (`sofias_memory/lifespan.py`, `sofias_memory/infrastructure/postgres/readiness.py`, `migrations/env.py`), its actual Dockerfile/Compose files, and its actually-documented deployment targets (Docker Compose, Portainer, EasyPanel; Docker Swarm is confirmed, in `docs/operations.md`, to not be used at all). This ADR freezes the outcome of that discovery.

## Decision

Sofias Memory will introduce an **automatic, serialized, fail-closed migration bootstrap**, integrated into the application's existing startup lifecycle.

### Scope of supersession: ADR-0011 D31/D32's "never automatic" clause only

This ADR supersedes **only** the specific clause of ADR-0011 D31/D32 (and the identically-stated rule repeated in `docs/operations.md`, `README.md`, `docs/deployment/easypanel.md`, and `AGENTS.md`/`CLAUDE.md`) that reads, in substance, "the application must not automatically run Alembic; the operator runs it explicitly out-of-band." Every other decision ADR-0011 froze remains fully intact and unmodified by this ADR:

- the three-state process model (`BOOTSTRAP_MAINTENANCE` → `STORAGE_CONVERGING` → `OPERATIONAL`, D31);
- "schema confirmed current" as the strict precondition that must be satisfied before storage convergence, Neo4j bootstrap, or worker/business processing may proceed (D32's ordering requirement — this ADR changes only *how* that precondition becomes true, never *when* it must already be true relative to everything else);
- fail-closed startup on any unresolved condition;
- the maintenance HTTP surface / liveness guarantee (D33) — `/health/live` remains reachable throughout bootstrap, `/health/ready` remains `NOT_READY` until `OPERATIONAL`;
- worker claim gating, Neo4j bootstrap ordering, and storage-convergence ordering, all unchanged;
- every other ADR-0011 decision (S3 object storage, CAS repointing, recovery-owned destructive work, D34–D43) — none of it is touched, referenced, or reinterpreted by this ADR.

ADR-0011 remains the historical record of the contract as originally frozen. It is not rewritten. A future documentation pass may add a short forward-reference note to ADR-0011 pointing at this ADR; that edit is out of scope for this ADR itself.

### Chosen architecture: FastAPI lifespan background bootstrap

The migration bootstrap runs as an early phase of the **existing** `lifespan()` background bootstrap task (`sofias_memory/lifespan.py`'s `_run_bootstrap`/`_attempt_bootstrap`), in the same position the existing read-only `PostgresReadinessChecker.check()` call occupies today — strictly before Neo4j bootstrap, before `worker.start()`, before storage convergence. No new orchestration layer, background task, or process-state value is introduced; the existing `BOOTSTRAP_MAINTENANCE` state, the existing fail-closed retry loop, and the existing D33 liveness contract are reused exactly as they already exist.

This is chosen over the alternative architectures for one decisive reason: **`lifespan()` already reaches `yield` — making `/health/live` reachable — before the bootstrap task does any work.** A migration that instead ran before the application process starts at all (a Dockerfile-baked entrypoint wrapper that `exec`s into `uvicorn` only after migrating, or a Compose one-shot service gating the app service's own startup) would leave `/health/live` unreachable for the entire duration of the migration — a direct regression of the D33 liveness guarantee ADR-0011 was specifically amended to establish, precisely because a long-running bootstrap phase (there, storage convergence; here, a migration) must not be mistaken for a dead container. Running migration-as-Dockerfile-build-time-step is rejected outright and separately: the image is built once and deployed against many different, not-yet-known database states — there is no database to migrate at build time at all, making that option not merely unsafe but incoherent.

### Alembic is invoked via a dedicated OS subprocess, never via its Python API in-process

The bootstrap invokes migration by spawning `alembic upgrade head` (the executable already installed in the release image's own virtualenv — `alembic` is a `[project.dependencies]` runtime dependency, not a dev-only tool) as a real OS subprocess (e.g. `asyncio.create_subprocess_exec`), awaited non-blockingly from within the existing bootstrap coroutine. It does **not** call `alembic.command.upgrade(...)` directly in-process.

This is a concrete technical requirement, not a style preference: `migrations/env.py`'s `run_migrations_online()` calls `asyncio.run(run_async_migrations_online())`. If invoked in-process from a coroutine already running under `uvicorn`'s own event loop — which every `lifespan()` background task by definition is — this raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. Subprocess invocation sidesteps this entirely (a new OS process gets its own fresh event loop) and requires **zero changes to `migrations/env.py`** — the exact same code path the operator's manual CLI invocation already exercises today runs unmodified. The subprocess runs in the same OS/container context, with the same `DATABASE_URL` and the same PostgreSQL role as the long-lived server process — this is **not** a database-privilege-separation mechanism, and this ADR does not claim it is one (see Security and privileges below). Its actual advantage is process/execution isolation and event-loop isolation: the schema-DDL execution happens in a distinct process with its own event loop, reusing the existing, unmodified Alembic CLI execution path, rather than mutating the long-lived server's own interpreter state in place.

### Migration mode: two states, `auto` default

A single configuration setting controls the bootstrap's behavior. Its exact public spelling (the Settings field alias, following this project's existing `SCREAMING_SNAKE` env-var convention — e.g. `DATABASE_MIGRATION_MODE`) must be confirmed against `sofias_memory/config.py`'s established naming conventions at implementation time; this ADR freezes only the **semantics** of the two states below, using `DATABASE_MIGRATION_MODE=auto|verify_only` as the working name.

- **`auto` (default).** The bootstrap may migrate a database whose current revision is a genuine ancestor of the application's Alembic head, including a pristine fresh database (see Schema classification below for the precise definition — never an unversioned-but-non-empty database, which always fails closed), forward to exactly that head, under the lock and verification discipline defined below.
- **`verify_only`.** Reproduces today's pre-ADR-0015 contract exactly: the bootstrap performs the same read-only schema check it already performs today, never invokes Alembic under any circumstance, and remains fail-closed in `BOOTSTRAP_MAINTENANCE` for anything other than an exact head match.

### Restore safeguard

Because `auto` mode will migrate **any** valid ancestor state forward automatically, an operator who restores or wants to inspect a historical backup at its **original** schema revision (rather than immediately transforming it forward) must start the application with `DATABASE_MIGRATION_MODE=verify_only` for that session. This is the explicit, documented way "historical restore" and "implicit forced upgrade" are kept distinct operator choices rather than becoming the same action by default. Future documentation updates (`docs/operations.md`'s backup/restore procedure) must state this explicitly; producing that documentation update is not part of this ADR.

### Advisory lock: type, key, ownership

- **Type:** a PostgreSQL **session-level** advisory lock (`pg_advisory_lock`/`pg_try_advisory_lock`/`pg_advisory_unlock`), not a transaction-level advisory lock (`pg_advisory_xact_lock`) — the lock must outlive the single short-lived Alembic subprocess invocation it protects and must be released explicitly, independent of any one transaction's commit or rollback.
- **Key:** a single, fixed, documented constant (a 63-bit-safe `bigint`) scoped to "Sofias Memory schema migration bootstrap." No key is derived from a runtime hash of a name or connection string, and no two-integer form is used — this is a single-tenant, one-database-per-instance application with no multi-tenant/shared-database key-collision concern to design around; a fixed, documented constant is simpler and equally sufficient.
- **Ownership:** the lock is acquired and held on a connection **dedicated to the bootstrap**, never the same connection or pool used for business queries or the pre-existing readiness-check connection, so returning that connection to a pool can never accidentally release the lock early. Alembic's own subprocess opens its own, entirely independent connection(s) (per `migrations/env.py`, unchanged) — this is safe: PostgreSQL advisory locks serialize by lock **key**, server-side, across all sessions, regardless of which connection any particular piece of protected work happens to run on. The only requirement is that the bootstrap's dedicated lock connection stays open for the full duration of the Alembic subprocess invocation and is not closed or released until that subprocess has fully exited.
- Explicitly not used: a `migration_locks` table, a Python-level `hash()`-derived key, or any distributed/external lock (Redis, filesystem, etc.) — a session-level advisory lock is a complete, natively-crash-safe primitive on its own (see lock semantics and crash behavior below); nothing external is needed.

### Lock semantics: cooperative serialization, always re-read after acquisition

The advisory lock serializes only **cooperative bootstrap participants** — processes that themselves attempt to acquire this specific lock key before migrating. Two bootstraps starting concurrently against the same database:

```text
bootstrap A: acquire lock -> classify DB state fresh -> migrate if required
             -> verify exact head -> release lock
bootstrap B: (blocked waiting for the same key while A holds it)
             -> acquire lock (now that A released it)
             -> RE-READ database state fresh (never reuse a snapshot taken
                before B started waiting)
             -> observes state is now already at exact head
             -> no-op
             -> release lock
```

Every bootstrap attempt re-derives its classification from PostgreSQL **after** acquiring the lock, never from an in-memory snapshot captured before waiting began — the same "always re-read authoritative state, never trust a stale snapshot" discipline `lifespan.py`'s own storage-convergence fixed-point loop (ADR-0011 D7) already applies. **No two ADR-0015 automatic bootstrap migration executions may run concurrently against the same database.** This guarantee is scoped precisely to this ADR's own cooperative bootstrap path: the advisory lock serializes only participants that acquire it, and the raw, administrative `alembic upgrade head` CLI (preserved below) does not automatically participate in it. This ADR does not claim, and cannot enforce, that no concurrent invocation of the raw CLI could ever race the automatic bootstrap — that is an operational discipline, not a locking guarantee (see the operational rule below).

### Lock acquisition timeout: bounded, non-fatal, retried by the existing outer loop

The bootstrap uses `pg_try_advisory_lock()` in a bounded retry loop against a monotonic deadline — never an unbounded blocking `pg_advisory_lock()` wait as the externally-observable behavior. The deadline value is an internal implementation constant at this stage; no new public configuration surface is introduced for it without a concretely demonstrated need. When the deadline expires without acquiring the lock:

```text
log a distinct "migration_lock_timeout" event
release the dedicated connection (never holding it open indefinitely)
remain in BOOTSTRAP_MAINTENANCE
the existing outer _run_bootstrap retry loop retries the entire attempt later
```

This is classified as a **pre-migration transient failure** (see the dedicated failure-semantics subsection below) — it does not indicate anything about migration safety and is safe to retry indefinitely by the existing mechanism, exactly like a transient PostgreSQL connectivity failure is retried today.

### Migration execution timeout: none

Once a migration subprocess has actually started, this ADR freezes **no generic automatic wall-clock timeout** that kills it. Killing DDL mid-execution because an arbitrary timer elapsed is less safe than allowing it to finish — an interrupted DDL statement can leave objects in an inconsistent state that is harder to reason about than "still running." `/health/live` remains reachable and `/health/ready` remains `NOT_READY` for the entire duration (D33, unchanged) — a long migration is observable as "still bootstrapping," not indistinguishable from a hung process, the same guarantee D33 already gives storage convergence. Explicit operator-initiated cancellation/shutdown during an in-flight migration is a distinct concern from an automatic timeout, frozen normatively in the next subsection, not assumed away by this ADR.

### Graceful shutdown during an in-flight migration: the migration critical section

Today, `lifespan()`'s shutdown path cancels `bootstrap_task` unconditionally and awaits it with `return_exceptions=True`. ADR-0015 adds a real Alembic child process to that task, so this ADR must freeze what cancellation means once that child exists — cancelling the awaiting coroutine must never silently abandon a live child process or release the advisory lock out from under it.

**Once the Alembic subprocess has been spawned, the migration subprocess and the advisory-lock ownership form one migration critical section.** For as long as this critical section is open, a **graceful application shutdown** MUST NOT:

```text
abandon the child process
release the advisory lock while the child is still alive
complete graceful shutdown while the migration child is still alive
```

If shutdown/cancellation is requested while a migration is executing, the required sequence is:

```text
shutdown becomes pending (not immediate)
migration supervision continues
the bootstrap keeps the advisory-lock connection alive
the application waits for the Alembic child to terminate naturally
the child's result (exit code) is observed and classified normally
  (success -> verify; failure -> sticky migration-execution-failure, per
  the Failure and retry semantics subsection, unchanged by shutdown being
  in progress)
only then is the advisory lock released
only then may graceful shutdown complete
```

This ADR does not freeze which specific Python primitive implements this (cancellation shielding, a supervising task awaited outside the cancelled scope, or an equivalent mechanism) — that is an implementation choice, made deliberately, not left ambiguous by omission. It does **not** introduce a generic migration timeout to bound how long graceful shutdown may wait: killing DDL merely because shutdown began would violate the same "never interrupt DDL mid-execution" reasoning the previous subsection already froze for the non-shutdown case, and would defeat the entire purpose of this critical-section rule.

**Hard termination is explicitly a different case, not a shutdown-path bug.** `SIGKILL`, a container hard kill, or a process crash are **not** graceful shutdown, and this ADR makes no attempt to give them graceful semantics:

```text
process/connections terminate immediately, including the child and the
  advisory-lock connection
PostgreSQL eventually releases the session-level advisory lock on its own
  (the lock's own crash-safety property, already frozen above)
the next application process attempt re-classifies authoritative database
  state from scratch, exactly as every other restart already does
```

No completion guarantee is made or implied for a migration interrupted by hard termination — only that the lock does not leak, and that the next attempt starts from a fresh, authoritative read of the database, never from an assumption about what the killed attempt might have finished.

### Schema classification (Alembic's own DAG is the authority)

| State | Behavior |
|---|---|
| **Pristine fresh schema** — `alembic_version` table absent **and** no user/application base tables exist in the current application schema | `auto`: migrate to head. `verify_only`: remain not-ready. |
| **Unversioned non-empty schema** — `alembic_version` table absent **and** one or more application base tables already exist | **fail closed (both modes)** |
| Exact application head | proceed / no-op (both modes) |
| Single known ancestor of head | `auto`: migrate to head. `verify_only`: remain not-ready. |
| Multiple Alembic heads in the application's own code | fail closed (both modes) |
| Multiple revisions recorded in the database | fail closed (both modes) |
| Unknown/unrecognized revision | fail closed (both modes) |
| Divergent revision (not an ancestor of head) | fail closed (both modes) |
| Database revision newer than this application's head | fail closed (both modes) — this is exactly the application-rollback scenario: an operator starting an older image against an already-migrated-forward database must never attempt `alembic downgrade`, `stamp`, or any other automatic "repair." Application rollback and schema rollback remain two different operations; only the former is ever implied by starting an older image. |

**Pristine fresh schema is a distinct, narrower state than "empty/unversioned," and the two must never be collapsed into one auto-migrable category.** Extensions, types, functions, or other objects that exist outside the application schema do not by themselves make a database non-pristine — only application base tables count. The absence of `alembic_version` alone does **not** prove a database is a genuine fresh install: it can equally mean damaged migration metadata, a partial/legacy/manually-created schema, an incomplete restore, or a foreign/unmanaged schema state that merely happens to lack this one bookkeeping table. Only when `alembic_version` is absent **and** the application schema itself is genuinely empty is auto-migration safe. An unversioned schema that already contains application tables is always **fail closed**, in both `auto` and `verify_only` — the bootstrap never runs `alembic stamp`, never infers a revision from which tables happen to exist, and never attempts any other automatic "repair" of that state. An operator facing this state must resolve it manually (inspect the schema, `stamp` deliberately if that is genuinely correct, or restore from a known-good backup) exactly as they would today.

Ancestry (whether the database's current revision is a genuine ancestor of the code's head, as opposed to diverged/unrelated/newer) is determined using Alembic's **own official revision-graph API** — `alembic.script.ScriptDirectory`'s `get_heads()`, `get_revision()`/`get_revisions()`, and `iterate_revisions()` (or the equivalent current API for the installed Alembic version) — never by manually parsing revision filenames or `down_revision` strings. This is the same graph-walking mechanism `alembic upgrade head` itself already relies on internally; reusing it directly is both more robust and less code than reimplementing DAG ancestry.

Migration never executes before classification completes.

### Ordering (unchanged surrounding sequence)

```text
FastAPI process starts (uvicorn, unchanged CMD)
  -> BOOTSTRAP_MAINTENANCE (unchanged)
  -> maintenance HTTP surface reachable (unchanged, D33)
  -> migration bootstrap (NEW, replaces today's read-only-only check):
       connect (dedicated connection)
       -> acquire advisory lock (bounded try+retry, see above)
       -> re-read authoritative database revision state
       -> classify against the Alembic DAG (table above)
       -> exact head: no-op
       -> allowed `auto` state: spawn `alembic upgrade head` subprocess,
          await completion
       -> re-run the existing read-only readiness check to VERIFY exact
          head is actually reached (never trust the subprocess exit code
          alone)
       -> release advisory lock
  -> Neo4j bootstrap (unchanged)
  -> pipeline recovery (unchanged)
  -> worker start, existing claim restrictions (unchanged)
  -> storage convergence when STORAGE_BACKEND=s3 (unchanged, ADR-0011)
  -> OPERATIONAL (unchanged)
```

### Failure and retry semantics — three explicitly distinct classes

This ADR names three failure classes precisely, so no ambiguity remains about what is safe to retry automatically and what is not.

**1. Pre-migration transient failure** (lock-wait timeout, PostgreSQL connectivity failure, Neo4j unreachable, any failure *before* a migration subprocess is actually spawned). Behavior: unchanged from today — logged, the whole bootstrap attempt is retried by the existing outer loop at its existing fixed interval. Safe to retry unconditionally; nothing has been attempted against the schema yet.

**2. Migration execution failure** (a migration subprocess was actually spawned and exited non-zero, or the post-migration verification finds the database is *not* at the expected exact head after the subprocess exited zero). This is **sticky for the remainder of that process's lifetime**:

```text
migration_failed_this_process = true   (in-memory, this bootstrap's own
                                         state — not a new database table
                                         or column)
```

For the rest of that process lifetime: **Alembic is never automatically invoked again.** The process remains in `BOOTSTRAP_MAINTENANCE`, `/health/live` stays reachable, `/health/ready` stays `NOT_READY`, and the existing read-only schema probe **continues to run** on the existing retry interval — purely to observe whether an operator has since fixed the problem manually. If that read-only probe later observes the database has reached the exact expected head (because an operator intervened out-of-band, exactly like today), the bootstrap proceeds to `OPERATIONAL` without requiring a process restart.

This must not be conflated with "safe ancestor state" from the schema-classification table. A multi-migration upgrade can partially succeed — e.g. database at `0017`, `0018` applies successfully, `0019` fails, intended head is `0020`. The database sitting at `0018` is still, by definition, a **known ancestor** of `0020`. "Known ancestor == safe initial candidate for an automatic upgrade attempt" and "a migration execution already failed in this process" are two independent, simultaneously-true facts about the same state, and only the second one gates future automatic Alembic invocation within that process's lifetime. Automatic migration is never unconditionally retried every 5 seconds once it has genuinely been attempted and failed — that would mean repeatedly re-running DDL against a database in a state a human has not yet examined, which is exactly the runaway behavior this sticky rule exists to prevent.

**No durable `migration_failure` table or column is introduced** merely to make this stickiness survive a process restart. After a process restart, the bootstrap classifies authoritative state fresh, exactly as it always does — if the database is still a valid ancestor state, a **new process lifetime** may make a new automatic attempt. This is a deliberate, explicitly accepted consequence, not an oversight: the actual requirement this ADR must satisfy is "no unbounded 5-second DDL retry loop within one process," not "eliminate every possible repeated attempt across every possible restart" — the latter would require new persistent infrastructure to solve a narrower problem than the one that actually matters operationally (a crash-looping container attempting DDL every few seconds).

**3. Schema-invalid / classification failure** (multiple heads, diverged revision, database ahead of the application, or any other state the classification table marks "fail closed"). No migration is ever attempted for these states in the first place; behavior is unchanged from today's existing `revision_mismatch`-shaped fail-closed retry.

### Multi-revision upgrades are not assumed atomic

`alembic upgrade head` applying several pending migrations is **not** assumed to be a single atomic unit — Alembic commits per-migration by default, not the whole batch as one transaction (the one deliberate exception in this project's own history, migration `0011`'s `ALTER TYPE ... ADD VALUE` inside an explicit `autocommit_block()`, is itself non-transactional by PostgreSQL's own requirement, not by Alembic batching them together). The bootstrap therefore always:

```text
read authoritative database state
-> attempt migration
-> read authoritative database state again
```

and never persists a local boolean such as `migration_done = true` as a substitute for actually re-reading the database's own revision state. The database's `alembic_version` table remains the single source of truth for "how far did this actually get," at every step.

### Migration authoring rule for automatic candidates

This ADR does **not** freeze "every migration must be additive" — that would be stricter than this project's own migration history already is (migration `0010` activates a previously-deferred constraint, and `0011` adds a native enum value with no safe downgrade; neither is purely additive, and both already shipped safely). Instead, the rule for any migration that may run under this automatic bootstrap is:

> Every automatic migration must be safely resumable from any committed Alembic revision reached before an interruption.

Concretely: a transactional migration step should rely on PostgreSQL's own transactional DDL where applicable (the default for nearly every migration in this project's history); any explicit non-transactional/autocommit step must be idempotent or otherwise safely re-runnable. Migration `0011` is the project's own existing precedent for the second case — `ALTER TYPE pipeline_type ADD VALUE IF NOT EXISTS 'dataset_delete'`, explicitly wrapped in `op.get_context().autocommit_block()`, safely re-runnable by construction (`IF NOT EXISTS`) regardless of how many times a retried bootstrap attempt might re-execute it.

### Manual migration and the Alembic CLI remain fully supported

`alembic upgrade head`, `alembic current`, and `alembic heads` remain available as administrative tools inside the release image, unchanged. Automatic bootstrap is an operational convenience and guard rail layered on top of Alembic, never a replacement for it, and never hides it. No HTTP endpoint for migration (`POST /migrate` or equivalent) is introduced; migration remains reachable only through the existing CLI/subprocess path, never through the public API.

**Operational rule — manual mutation must never race an in-flight automatic bootstrap.** A raw/manual Alembic mutation command (`alembic upgrade`, `downgrade`, `stamp`, etc.) MUST NOT be run concurrently with an automatic migration bootstrap whose own Alembic subprocess is in flight — the advisory lock protects cooperative bootstrap participants from each other, not the database from an operator manually racing it from outside that mechanism. Manual migration remains straightforwardly safe to use whenever, for example: the application process is stopped; `DATABASE_MIGRATION_MODE=verify_only` is in effect (the bootstrap never invokes Alembic at all in that mode); or the running process is already in the sticky migration-failed/read-only state (§ Failure and retry semantics) and is therefore only performing read-only probes, never spawning a new Alembic subprocess on its own. This ADR does not define a new wrapper command or a new API to enforce this rule mechanically — it remains an operator/documentation discipline, the same way it already is today for two operators who might otherwise both run `alembic upgrade head` by hand at the same time.

### Business/worker gating is unchanged

While the migration bootstrap phase is in progress, the process remains `BOOTSTRAP_MAINTENANCE` exactly as it does today for the equivalent read-only check: normal business API requests remain fail-closed, worker claims remain disabled, Neo4j bootstrap has not started, and storage convergence has not started. `/health/live` remains available; `/health/ready` remains `NOT_READY`. None of ADR-0011's existing gating logic changes shape — only the specific mechanism that decides "is the schema current" gains the ability to act, not merely observe.

### Security and privileges

The bootstrap uses the **same** `DATABASE_URL`/role the application already uses at runtime — no new migration-specific database role is introduced by this ADR. This is not a new exposure: an operator running `alembic upgrade head` manually today already does so with this exact same connection string and role, so the runtime role already implicitly has whatever DDL privileges migrations require. Separating a dedicated migration-only credential from the runtime credential is recorded here as a **future hardening possibility**, not current scope — it would add real operational complexity (two credentials to provision, rotate, and keep in sync with `.env.example`/Compose) without a concrete requirement driving it today. As already required elsewhere in this project, `DATABASE_URL`, passwords, and any other credential material are never logged by the bootstrap's observability events (below).

### Deployment portability: correctness lives in the application + PostgreSQL, not the orchestrator

This architecture's correctness must never depend on:

```text
Docker Compose's `service_completed_successfully` dependency condition
init-container support of any kind
EasyPanel-specific hooks
Portainer-specific hooks
Docker Swarm / `docker stack deploy` lifecycle semantics
```

The discovery pass found that EasyPanel's own documented migration procedure (`docs/deployment/easypanel.md` §4) already avoids relying on a Compose one-shot/init-container mechanism — it has an operator open a live console inside the already-running container instead — the strongest available evidence that this mechanism cannot be assumed reliably available across every deployment target this project documents. Because the chosen architecture (lifespan-based, inside the one existing application process) needs none of the above, it works identically under plain Compose, Portainer (which consumes `compose.yaml` directly, `docs/operations.md` §F), and EasyPanel (`deploy/easypanel/compose.yaml`), with the exact same image and the exact same code path in every case. Any of those orchestration mechanisms may later be layered on top as a complementary convenience (for example, a documented optional one-shot Compose service for operators who prefer it) — but never as the thing this ADR's safety guarantees depend on.

### No promise of rolling-upgrade compatibility

This project's actual, documented deployment model is stop-old-then-start-new (`docs/operations.md`: "this repository does not use Docker Swarm/`docker stack deploy` anywhere, so no rolling-update orchestrator sits between the operator and the container"), not a rolling update where old and new application versions run concurrently against a schema in transition. This ADR does not require, and does not attempt to guarantee, that an old and a new application version can safely operate concurrently against a just-migrated schema. If rolling-update support is ever pursued, the compatibility requirements it would impose on migration authoring belong to a separate, future architecture decision — this ADR's "safely resumable from any committed revision" rule is necessary but not sufficient for that stronger property.

### Backup/restore consequence, explicitly documented

With `auto` mode as the default, starting a current-head image against a restored **historical** database revision that is a valid ancestor of that head will **automatically migrate it forward on first boot** — a materially different outcome from today's contract, where the operator explicitly decides whether and when to advance a restored database. An operator who needs to inspect or validate a restored backup at its original schema revision before deciding to advance it must start that container with `verify_only` mode (see Restore safeguard above). This consequence must be stated explicitly in the backup/restore operational documentation once implemented; it is not a hidden side effect this ADR leaves undocumented.

### Observability

The following structured log events are frozen conceptually (exact field names/schema are an implementation detail):

```text
migration_bootstrap_started
migration_lock_waiting
migration_lock_acquired
migration_lock_timeout
migration_not_required
migration_upgrade_started
migration_upgrade_succeeded
migration_upgrade_failed
migration_schema_invalid
migration_bootstrap_verified
```

None of these events, nor any other logging this bootstrap introduces, may ever include `DATABASE_URL`, a password, or any other credential material — the same discipline already required of every other component in this codebase.

### No new database schema, no new public API

```text
new application table = 0        (the advisory lock needs no backing table)
new Alembic revision = 0         (this ADR changes bootstrap behavior, not schema)
new public HTTP endpoint = 0     (no POST /migrate, no migration admin API)
```

### Testing obligations for the future implementation

This ADR does not implement tests, but freezes the minimum gates a future implementation must satisfy:

- **Unit:** schema classification for every state in the table above, under both `auto` and `verify_only`, **including pristine-fresh vs. unversioned-non-empty as two distinct states, never collapsed into one**; code-side multi-head detection; database-side multi-revision detection; the sticky migration-execution-failure rule (a failed attempt is not retried within the same process).
- **Real PostgreSQL (Integration):** a fresh, genuinely pristine database → automatically migrates to head; an unversioned database that already contains application tables → fails closed with **zero** Alembic invocation (proven directly, not merely inferred from the end state); known ancestor → head; exact head → no-op; two concurrent bootstraps against the same database → exactly one migration execution, the other observes the already-migrated result after re-reading state; lock-wait timeout and its non-fatal retry; successful unlock; lock release on connection loss/crash; a genuine migration failure, followed by proof that it is not automatically retried on the existing 5-second interval; a subsequent manual operator fix, followed by proof that the existing read-only probe alone (no restart) carries the process to `OPERATIONAL`; **graceful shutdown requested while the Alembic subprocess is running → the advisory lock remains held until the child exits, a second bootstrap attempting to start during that window cannot begin a migration, and only after the child terminates is the lock released and shutdown allowed to complete.**
- **Deployment/static:** `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` must be **replaced**, not deleted, by an invariant with equivalent intent: Compose must never invoke raw/unserialized Alembic migration directly as a `command:`/`entrypoint:`; automatic migration is permitted only through this ADR's sanctioned, locked, verified bootstrap path.
- **CI placement:** unit/static checks run in the normal CI workflow; real-PostgreSQL concurrency/failure scenarios run in the Integration workflow, following this project's existing dedicated-disposable-database discipline; a release-image migration smoke (fresh database and a previously-released database, each migrating automatically to become ready with no manual Alembic step) runs as part of the Integration and/or Release gate, mirroring how this project already validates release images today.

## Consequences

Sofias Memory gains the ability to migrate a valid, ancestor-state PostgreSQL schema forward automatically on ordinary application startup, removing the specific "operator forgot to run Alembic" failure mode that has already caused one documented incident in this project's own deployment history — without weakening fail-closed startup, without adding a new deployment-orchestrator dependency, and without regressing the D33 maintenance-HTTP-surface guarantee.

The explicit-migration contract does not disappear: it becomes the `verify_only` mode, remains the default posture for restore/inspection scenarios, and the manual Alembic CLI remains fully available and unchanged inside the release image. ADR-0011's three-state process model, its liveness contract, and every one of its decisions besides the single "never automatic" clause remain exactly as frozen.

This ADR accepts, and explicitly documents rather than hides, one real behavioral change operators must understand: with `auto` as the default, restoring a historical backup and starting a current image against it will migrate that backup forward immediately unless the operator deliberately opts into `verify_only` first.

Future work — a durable migration-attempt audit table, a dedicated migration-only database role, formalized rolling-upgrade compatibility guarantees, or a public escape-hatch for the lock-wait/lock-timeout constants — is additive and does not require redesigning the lock, the classification model, or the sticky-failure rule defined here.

## Alternatives Rejected

- **Compose one-shot migration service, used as the correctness boundary.** Rejected as the primary mechanism because the discovery pass found no reliable evidence this pattern is supported across every deployment target this project documents — EasyPanel's own migration procedure avoids it — and because, even where it is supported (plain Compose, Portainer), it leaves `/health/live` unreachable for the full migration duration, regressing D33. May exist later as an optional convenience layered on top; never as the thing correctness depends on.

- **Image-level entrypoint bootstrap that migrates before `exec`-ing the application process.** Rejected for the same D33 regression: no HTTP surface of any kind is reachable while a container is running an entrypoint script instead of the application, unlike the chosen lifespan-based approach where the maintenance surface is already live before the bootstrap task does anything.

- **Build-time (Dockerfile) migration.** Rejected outright, not merely deprioritized: a release image is built once and deployed against arbitrarily many, not-yet-known database states; there is no database reachable at build time to migrate in the first place.

- **Calling Alembic's Python API (`alembic.command.upgrade(...)`) directly in-process from the lifespan coroutine.** Rejected because `migrations/env.py`'s existing `asyncio.run()` call is incompatible with being invoked from inside an already-running event loop, and modifying `env.py` to become loop-aware would be a larger, riskier change than simply invoking the existing, unmodified CLI as a subprocess.

- **A `migration_locks` database table instead of a PostgreSQL advisory lock.** Rejected because an advisory lock is natively crash-safe (released automatically when its owning connection/session ends) and requires no schema of its own, no migration to create it, and no cleanup logic for orphaned rows after a crash — strictly simpler for an equivalent guarantee.

- **A Python-`hash()`-derived or otherwise computed advisory-lock key.** Rejected in favor of one fixed, documented constant — this is a single-tenant, one-database-per-instance product with no multi-application key-collision scenario to design a hashing scheme around.

- **Unconditionally retrying a failed migration attempt on the existing 5-second bootstrap-retry interval.** Rejected as unsafe: repeatedly re-attempting DDL against a database in a state a human has not yet examined is exactly the runaway behavior this ADR's sticky-failure rule exists to prevent. The existing 5-second interval remains correct and unchanged for every *other* class of bootstrap failure (pre-migration transient failures, schema classified as fail-closed) — it is narrowly excluded only for "a migration execution already failed in this process."

- **A durable `migration_failure` table/column to make failure-stickiness survive a process restart.** Rejected as solving a narrower problem than the one that actually matters (an unbounded DDL retry loop *within one process*) at the cost of new persistent infrastructure; a fresh process lifetime re-classifying authoritative state from scratch is an explicitly accepted, documented trade-off, not an oversight.

- **A blanket "all automatic migrations must be additive" authoring rule.** Rejected as inconsistent with this project's own migration history (`0010`, `0011`) and stricter than necessary; replaced with the more precise "safely resumable from any committed revision" rule, which this project's actual migrations already satisfy.

- **An automatic wall-clock execution timeout that kills an in-flight migration.** Rejected because forcibly interrupting DDL is less safe than letting it finish, and because the existing D33 liveness contract already makes "still migrating" observably distinct from "hung," removing the motivation a timeout would otherwise address.

- **A separate migration-only database role/credential, introduced now.** Rejected as scope creep relative to today's already-identical-credential reality (an operator running Alembic manually today already uses the same `DATABASE_URL`); recorded as future hardening instead.

## References

- `docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md` — D31 (process state model), D32 (the clause this ADR narrowly supersedes), D33 (maintenance-HTTP-surface/liveness contract, preserved unchanged and the deciding factor in this ADR's architecture choice).
- `docs/adr/0009-worker-queue-and-pipeline-lifecycle-contract.md` — the existing fail-closed, never-crash-loop retry discipline this ADR's pre-migration-transient-failure handling reuses unmodified.
- `docs/adr/0010-administrative-dataset-deletion-contract.md` — precedent for a narrow, explicitly-scoped supersession of one specific clause of an earlier ADR without rewriting it.
- `sofias_memory/lifespan.py` — the existing bootstrap task, state machine, and retry loop this ADR's migration phase is integrated into.
- `sofias_memory/infrastructure/postgres/readiness.py` — the existing read-only schema-state check this ADR reuses for post-migration verification and for `verify_only` mode.
- `migrations/env.py` — the existing Alembic environment this ADR requires zero changes to, by construction (subprocess invocation).
- `migrations/versions/0011_add_dataset_delete_pipeline_type.py` — the project's own existing precedent for an idempotent, explicitly non-transactional (`autocommit_block`) migration step, cited by this ADR's migration-authoring rule.
- `docs/operations.md`, `docs/deployment/easypanel.md`, `README.md` — the operational documentation whose "migration is explicit, never automatic" statements this ADR's decision requires a future documentation pass to update (not part of this ADR).
- `tests/unit/test_deployment_compose.py` — the existing structural safety test this ADR requires a future implementation to replace with an equivalent-intent invariant, not delete.
