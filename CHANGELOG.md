# Changelog

All notable, user-facing changes to Sofias Memory are documented in this file.

## [0.6.0]

Minor release adding an **automatic, serialized, fail-closed migration
bootstrap** (ADR-0015): the application can now migrate an eligible
PostgreSQL schema forward to head automatically, on ordinary startup,
removing the operational burden of remembering to run `alembic upgrade
head` manually before or after every redeploy. This is a narrower,
carefully-scoped guard rail layered on top of Alembic, never a replacement
for it — Alembic remains the sole schema-evolution authority, the manual
CLI remains fully supported, and any ambiguous schema state still fails
closed rather than being guessed, stamped, or repaired automatically.

### Migration mode

- New `DATABASE_MIGRATION_MODE=auto|verify_only` setting, default `auto`.
- `auto`: a schema that is either pristine/fresh or a known ancestor of the
  application's Alembic head migrates forward to head automatically at
  startup, under a session-level PostgreSQL advisory lock, with
  pre- and post-migration verification. Any other schema state (an
  unversioned-but-non-empty schema, multiple code heads, multiple database
  revisions, a diverged/foreign/unrecognized revision) fails closed —
  never guessed, stamped, or repaired.
- `verify_only`: reproduces the pre-v0.6.0 contract exactly — the
  application never invokes Alembic itself under any circumstance; a
  schema not already at exact head simply stays `not_ready`.
- Restoring a historical backup under the `auto` default migrates it
  forward automatically on first boot; start with `verify_only` to inspect
  a restored backup at its original revision first (see
  `docs/operations.md`).

### Sticky failure and manual recovery

- A genuine migration-execution failure is sticky for the remainder of
  that process's lifetime — Alembic is never automatically re-invoked
  within the same process after a failed attempt, avoiding a runaway
  DDL-retry loop against a database state a human has not yet examined.
- If an operator repairs the database manually while that same process is
  still running, a continuing read-only probe detects it and the process
  proceeds to ready **without requiring a restart**. A process restart
  always re-classifies the schema from scratch.

### Graceful shutdown and hard-termination safety

- Once the Alembic child process has been spawned, it and the advisory
  lock ownership form one critical section: a graceful application
  shutdown never abandons a live migration child or releases the lock
  while it is still running — the shutdown waits for the child to finish
  naturally, classifies its result, and only then completes.
- A hard-killed supervisor (`SIGKILL`, crash, container kill) can never
  leave an unprotected migration child able to keep mutating the database
  after its serialization protection has disappeared — on this project's
  Linux release runtime, the migration child cannot outlive the supervisor
  that owns its advisory lock (`PR_SET_PDEATHSIG`); this guarantee is
  Linux-specific and is documented as such, not silently promised
  cross-platform. The advisory lock itself never leaks, and the next
  legitimate participant always re-reads authoritative PostgreSQL state
  from scratch before acting.

### Documentation

- `README.md`, `docs/operations.md`, and `docs/deployment/easypanel.md`
  updated for the `auto`/`verify_only` contract — the previous
  "always run migrations manually" instructions no longer describe the
  default path. `AGENTS.md`/`CLAUDE.md` synchronized with the same
  semantics.
- The deployment static invariant that used to assert Compose never runs
  Alembic automatically now asserts the equivalent, updated property:
  Compose never invokes Alembic directly (`command:`/`entrypoint:`);
  automatic migration is permitted only through this release's sanctioned,
  locked, verified application-startup bootstrap.

### Database/upgrade

- **Introduces zero new database migrations.** This release changes how
  and when `alembic upgrade head` gets invoked, not the schema itself —
  the Alembic head remains `0017`. Upgrading from `0.5.0` requires no
  migration step at all; starting the `0.6.0` image against an
  already-migrated `0.5.0` database performs a no-op automatic bootstrap
  and reaches ready immediately.

### Configuration

- New setting: `DATABASE_MIGRATION_MODE` (`auto`/`verify_only`, default
  `auto`). No other new runtime configuration surface.

## [0.5.0]

Minor release adding first-class durable **Agent** profiles (ADR-0014):
Sofias Memory stores and manages Agent identity/configuration and two
explicit associations. Sofias Memory never executes an Agent, selects a
provider/model on its behalf, or manages a provider session. Entirely
additive to the existing public API.

### Agent management

- New `Agent` resource: `POST /agents`, `GET /agents`,
  `GET /agents/{agent_uuid}`, `PATCH /agents/{agent_uuid}`.
- `name` (portable, lowercase `a-z0-9-`, 1..64 chars) is the immutable
  logical identity; `agent_uuid` is the structural identity. `name` already
  existing, in any status, is always `409 INVALID_REQUEST` — creation never
  upserts.
- Profile fields: `display_name`, `description`, `instructions`,
  `metadata` — `PATCH` replaces each field actually sent, and `metadata` is
  a wholesale replacement, never a deep merge.
- Lifecycle: `active <-> archived` via `POST /agents/{agent_uuid}/archive`
  and `.../restore`, idempotent with no timestamp churn on replay. Like
  Skill archive, this is a **discovery filter, not a management admission
  barrier** — every management and association operation remains available
  on an archived Agent; only the default `GET /agents` listing (no
  `status` filter) excludes it, defaulting to `active` only.

### Agent ↔ Skill associations

- `GET /agents/{agent_uuid}/skills`,
  `PUT /agents/{agent_uuid}/skills/{skill_uuid}`,
  `DELETE /agents/{agent_uuid}/skills/{skill_uuid}` — an explicit,
  idempotent association between an Agent and a Skill.
- `pinned_revision` omitted/`null` follows the Skill's `current_revision`
  live; an explicit integer pins the association to exactly that revision,
  and a later Skill rollback does not move the pin. `GET` discloses
  `current_revision`, `pinned_revision`, and `effective_revision` side by
  side, plus the effective revision's own `description`/`tags`/
  `declared_tools`/`compatibility` — never `procedure`,
  `resolution_embedding`, or any internal `SkillRevision.id`.
- An archived Skill's association remains fully visible and manageable —
  `GET`/`PUT`/`DELETE` all keep working, pin state and
  `association_created_at` unchanged.
- Pin integrity is protected structurally by PostgreSQL: a composite
  foreign key ties the pin to `(skill_id, pinned_revision_id)`, so a pin
  can never reference another Skill's revision, and deleting a pinned,
  non-current revision is rejected outright rather than silently clearing
  the pin.

### Agent ↔ Session associations

- `GET /agents/{agent_uuid}/sessions`,
  `PUT /agents/{agent_uuid}/sessions/{session_uuid}` (no request body),
  `DELETE /agents/{agent_uuid}/sessions/{session_uuid}` — an explicit,
  **M:N** association between an Agent and a Session.
- Records the **current explicit management association only** — **not
  historical Agent provenance**. `DELETE` removes the fact outright;
  re-associating afterward creates a new row with a new
  `association_created_at`, strictly later than the deleted one. A Session
  associated with more than one Agent has no per-operation Agent
  discriminator: a `Query`/`PipelineRun` linked to that Session can never
  be attributed to one specific Agent from this association alone.
- Associating with an archived Session is permitted (`200`) and is
  management metadata only — it never creates a `SessionEntry`/`Query`/
  `PipelineRun` and never touches `Session.updated_at`/`archived_at`.

### Compatibility

- Forget (source/dataset/everything scope) and administrative Dataset
  Delete never affect `Agent`, `agent_skills`, or `agent_sessions` — proven
  against real PostgreSQL, including the most sensitive case,
  `DELETE EVERYTHING`.
- Agent operations create zero `graph_outbox` events and are never
  projected to Neo4j — no Agent node, relationship, or property, proven
  against real PostgreSQL and real Neo4j.
- `AgentSkill`'s `declared_tools` disclosure does not authorize tool use —
  the same non-guarantee Skills already document; Sofias Memory has no
  Agent/Skill execution path to gate in the first place.
- `AgentSession` association creates no Session activity of any kind —
  proven with real before/after row-count deltas on `SessionEntry`/`Query`/
  `PipelineRun`.
- `queries`, `pipeline_runs`, and `session_entries` gain no `agent_id`
  column — per-operation Agent attribution is structurally impossible by
  design, not merely unimplemented.

### Lifecycle/concurrency

- `PATCH` racing `archive`/`restore` on the same Agent is linearizable via
  single-row `FOR UPDATE` locking, with no lost update, under real
  concurrent load — proven both ways (whichever operation is serialized
  first, the composed final state is always correct).
- Duplicate `agent_skills`/`agent_sessions` association attempts converge
  to exactly one row via composite-primary-key upsert; concurrent
  different-pin `PUT`s on the same Agent↔Skill pair converge to the last
  serialized winner, never a duplicate.
- Different Agents never serialize against each other — per-Agent, not
  global, locking.

### Database/upgrade

- Adds migrations `0015` (`agents`), `0016` (`agent_skills`), `0017`
  (`agent_sessions`). Upgrading from `0.4.0` requires `alembic upgrade
  head` (current head: `0017`) before starting the new version — applies
  `0014 -> 0015 -> 0016 -> 0017` in one pass — see `docs/operations.md`.

### Configuration

- No new required settings. Agent Management introduces no new runtime
  configuration surface.

## [0.4.0]

Minor release adding first-class durable procedural **Skills**: Sofias
Memory stores, versions, and semantically resolves Skills; the caller
decides what to do with a resolved Skill and executes it themselves —
Sofias Memory never invokes a Skill, selects one automatically, or
authorizes tool use. Entirely additive to the existing public API.

### Skill management and immutable revisions

- New `Skill`/`SkillRevision` resources: `POST /skills`, `GET /skills`,
  `GET /skills/{skill_uuid}`, `PATCH /skills/{skill_uuid}` (strictly
  `current_revision` rollback — never content, never `name`).
- `name` (portable, lowercase `a-z0-9-`, 1..64 chars) is the immutable
  logical identity; `skill_uuid` is the structural identity. `name` already
  existing, in any status, is always `409 INVALID_REQUEST` — creation never
  upserts and never resolves by content hash.
- `POST/GET /skills/{skill_uuid}/revisions`,
  `GET /skills/{skill_uuid}/revisions/{revision}`: a `SkillRevision` is
  immutable once created (no `PATCH`/`DELETE`), addressed by its per-Skill
  integer `revision`, never an internal id. Creating a revision with content
  semantically identical (`content_sha256`) to an existing revision of that
  Skill is a safe replay (`200`, existing revision, `current_revision`
  unchanged) instead of a duplicate (`201` for genuinely new content).
- Lifecycle: `active <-> archived` via `POST /skills/{skill_uuid}/archive`
  and `.../restore`. Unlike Session archive, this is a **discovery filter,
  not a write admission barrier** — every management operation (`GET`,
  create revision, `PATCH current_revision`, export) remains available on an
  archived Skill; only `POST /skills/resolve` excludes it. `restore`
  preserves whatever `current_revision` the Skill has *at the moment of
  restore*, never reconstructing the pointer from before the archive.

### Standalone `SKILL.md` interoperability

- `POST /skills/import` (new Skill from a standalone `SKILL.md` document;
  an existing `name` is always `409`, never safe replay, never upsert),
  `POST /skills/{skill_uuid}/revisions/import` (new revision on an
  already-identified Skill, following the same safe-replay rule as the
  structured revision-create route), and
  `GET /skills/{skill_uuid}/revisions/{revision}/export`.
- Covers the portable frontmatter subset only (`name`, `description`,
  `license`, `compatibility`, `metadata`, `allowed-tools`) plus the Markdown
  body as `procedure`; bundled packages (`scripts/`/`references/`/
  `assets/`), zip import, a filesystem watcher, and a remote registry are
  explicitly out of scope.
- `tags` is a Sofias Memory extension with no portable frontmatter field: it
  round-trips through the reserved transport key
  `metadata["sofias-memory.tags"]`, consumed and removed on import (never
  persisted as semantic metadata) and re-synthesized on export.
- `content_sha256` is the SHA-256 of the revision's canonical semantic JSON
  representation, never a digest of the original or exported `SKILL.md`
  bytes — export is deterministic over persisted content, not a promise of
  byte-identity with whatever was originally imported.

### Semantic resolve

- `POST /skills/resolve` (`query`, `top_k`, default 5, max 20) ranks
  `active`-status Skills' *current* revision by cosine similarity over
  pgvector (HNSW `halfvec_cosine_ops`, ADR-0006), returning up to `top_k`
  matches ordered by `score` descending (higher = more similar) with no
  hidden similarity threshold; `matches: []` (never `404`) when none are
  eligible.
- Progressive disclosure: `resolve`, `GET /skills`, `GET /skills/{skill_uuid}`,
  and the revision list never include `procedure` — only `skill_uuid`,
  `name`, `description`, `current_revision`, `tags`, `declared_tools`,
  `compatibility`, and (resolve only) `score`. `resolve` never selects a
  Skill on the caller's behalf, never fetches `procedure`, and never
  executes a tool.
- `declared_tools`/`allowed-tools` is descriptive metadata only — never an
  authorization grant, a permission, or an installed-tool guarantee.

### Compatibility

- Forget (source/dataset/everything scope) and administrative Dataset
  Delete never affect any Skill or SkillRevision, including their
  `current_revision` pointer and content hashes — proven against real
  PostgreSQL, including the most sensitive case, `DELETE EVERYTHING`.
- Session and SessionEntry carry no column or reference to Skills, and
  neither feature's lifecycle affects the other.
- Skills are global (no Dataset/Session ownership), exist only in
  PostgreSQL/pgvector, and are never projected to Neo4j — no Skill node,
  relationship, or `graph_outbox` event is ever created by any Skill
  operation.
- Concurrency hardened under real load: colliding `name` creation converges
  to exactly one winner; concurrent revision creation with different
  content gets distinct monotonic ordinals; concurrent identical content
  converges to one persisted revision; `current_revision` rollback and new
  revision creation are linearized per-Skill with no lost update; different
  Skills never serialize against each other.

### Database/upgrade

- Adds migration `0014` (`skills`, `skill_revisions`, including the pgvector
  HNSW expression index used by semantic resolve). Upgrading from `0.3.0`
  requires `alembic upgrade head` (current head: `0014`) before starting the
  new version — see `docs/operations.md`.

### Configuration

- No new required settings. Skills reuse the existing OpenAI-compatible
  embedding provider configuration (`EMBEDDING_*`) already used elsewhere.

## [0.3.0]

Minor release adding first-class durable Sessions: a persistent temporal
context boundary that Recall and Remember can associate with, entirely
additive to the existing public API.

### First-class Sessions

- New `Session` resource: `POST /sessions`, `GET /sessions`,
  `GET /sessions/{session_uuid}`, `PATCH /sessions/{session_uuid}`.
- Caller-facing `session_id` (external key, case-sensitive, immutable) and
  structural `session_uuid` (internal/public UUID identity).
- Lifecycle: `active <-> archived` via `POST /sessions/{session_uuid}/archive`
  and `POST /sessions/{session_uuid}/restore`. Archive is an admission
  barrier — it blocks new SessionEntry/Recall/Remember activity but never
  blocks safe replay, manual retry, reads, or administrative `PATCH`.

### SessionEntries

- Append-only contextual history: `POST` / `GET /sessions/{session_uuid}/entries`.
- Optional caller-supplied `external_id`, unique per Session: the same id
  with the same semantic payload safely replays (`201`, no duplicate); the
  same id with a different payload is `409 IDEMPOTENCY_CONFLICT`. Replay
  remains observable after archive.

### Recall

- Recall accepts `session_id`; the resulting Query is associated with the
  resolved/lazily-created Session (`RecallResult.session_uuid`).
- New opt-in `include_session_context` (default `false`): injects a bounded,
  deterministic window of recent SessionEntries into RAG generation only —
  retrieval itself is unchanged, and no query rewriting occurs.
- `GET /provenance/query/{query_id}` now exposes `session_uuid` and
  `session_context`, the exact SessionEntries used, independent of knowledge
  provenance.

### Remember / Runs

- Remember (text/URL/file) accepts `session_id`; `PipelineRun.session_id` is
  the authoritative association. `RememberTextResult.session_uuid` and
  `RunSummaryResult`/`RunDetailResult.session_uuid` expose it.
- `GET /runs` accepts an optional `session_uuid` filter.
- Manual retry preserves the original run's Session association verbatim —
  it is never re-resolved by external key, and remains permitted even if
  that Session is now archived.

### Compatibility

- No backfill: pre-v0.3.0 textual `session_id` carriers (`MemoryEntry`,
  `Document.metadata`, historical `PipelineRun.input`) are never converted
  into a first-class Session association.
- Forget and administrative Dataset Delete preserve Sessions, SessionEntries,
  and Query/Feedback audit history unchanged; `DELETE EVERYTHING` removes
  semantic memory, never Session/history state.
- A Session may span multiple Datasets; it never acquires Dataset ownership.
- Sessions and SessionEntries exist only in PostgreSQL — no Neo4j projection,
  no new `graph_outbox` event category.

### Database/upgrade

- Adds migrations `0012` and `0013`. Upgrading from `0.2.0` requires
  `alembic upgrade head` (current head: `0013`) before starting the new
  version — see `docs/operations.md`.

### Configuration

- `SESSION_CONTEXT_MAX_ENTRIES` (default `20`)
- `SESSION_CONTEXT_MAX_CHARS` (default `16000`)

## [0.2.0]

Minor release adding durable S3-compatible storage for Source originals while
preserving the existing filesystem backend and public API contract.

### Durable S3-compatible Source storage

- Added `filesystem` and `s3` as the two supported first-party storage
  backends for finalized Source originals; `filesystem` remains the default.
- Added deterministic `s3://bucket/key` storage through the existing
  `SourceObjectStorage`/`SourceStorageRouter` boundary, with SHA-256 and byte
  size verification, idempotent finalize, and typed conflict detection.
- Reads and deletes follow each persisted Source URI scheme, so historical
  `file://` and `s3://` Sources can coexist safely regardless of the current
  write backend.
- Added explicit destructive-storage outcomes:
  `NOT_REQUESTED`, `DELETED_NOW`, `ALREADY_ABSENT`, and `UNRESOLVED`, including
  version-aware deletion semantics when bucket versioning is enabled.

### Startup convergence and recovery

- Added fail-closed process states
  `BOOTSTRAP_MAINTENANCE` → `STORAGE_CONVERGING` → `OPERATIONAL`.
- Enabling `STORAGE_BACKEND=s3` on an existing filesystem deployment
  automatically converges eligible `file://` Source originals to S3 at startup
  using verify-before-CAS repointing and post-CAS exact local cleanup.
- `/health/live` remains available during convergence; `/health/ready` and
  business routes remain unavailable until convergence reaches a clean fixed
  point.
- Added durable crash/restart handling for Remember finalization and
  filesystem→S3 convergence without holding PostgreSQL locks across external
  storage I/O.
- Added D43 lifecycle exclusion so supported single-process deployments cannot
  start new destructive authoritative transitions for live Case-A Sources
  during storage convergence; an observed `DELETING` transition at the CAS
  boundary now fails closed as a named integrity violation.

### Provider validation

- Completed production-shaped Gate-G validation against a real MinIO
  S3-compatible endpoint, including migration, restart/idempotency,
  conflict/absence semantics, and version-aware destructive deletion.
- Completed a separate provider-compatibility smoke against real Wasabi
  (`us-east-1`) with the same adapter and no provider-specific code changes:
  probe, finalize/PUT, HEAD metadata verification, GET/hash/size,
  idempotency, conflict fail-closed, and delete/positive-absence all passed.
- The validated providers are compatibility evidence, not an allowlist; the
  application remains provider-neutral at the S3 API boundary.

### Deployment and operations

- Added complete S3 configuration, IAM/least-privilege guidance,
  filesystem→S3 upgrade/convergence, backup/restore, rollback, outage, and
  troubleshooting documentation.
- `DATA_DIRECTORY` remains mandatory and persistent in both storage modes
  because it still owns durable ingress and in-transit recovery/migration
  state.
- Documented the single-process `stop old -> start new` deployment invariant
  required while S3 convergence is in use.
- Added and validated the dedicated EasyPanel deployment artifact and clarified
  when Alembic migrations are required versus ordinary redeploys.

### Security and CI

- Raised the `pypdf` runtime dependency floor to `>=6.16.1,<7`; the lock now
  resolves to a release with the known runtime advisories fixed.
- Kept runtime `pip-audit` as a blocking CI gate and HIGH-severity Bandit
  findings blocking while retaining the full Bandit report as informational.
- Strengthened Settings / `.env.example` / Compose parity validation,
  including intentionally-commented optional S3 placeholders.

### Compatibility and upgrade notes

- No public API path or request/response business contract is intentionally
  changed by this release.
- No new database schema migration is introduced by `0.2.0`.
- Existing filesystem deployments continue to work without any S3
  configuration.
- Switching an existing deployment from `filesystem` to `s3` performs
  automatic forward convergence at startup; there is intentionally no
  automatic S3→filesystem reverse migration.
- After any Source has been durably repointed to `s3://`, rolling the
  application back to a pre-S3 release is not a safe ordinary image rollback;
  follow the backup/restore procedure documented in `docs/operations.md`.
- PostgreSQL remains authoritative and Neo4j remains a reconstructible
  projection.

## [0.1.2]

Patch release for a production defect found by the EASYPANEL-001 production
smoke's Dataset-delete cleanup phase.

### Fixes

- Fixed a `graph_outbox` drain ordering defect where a mixed snapshot of
  entity/chunk UPSERT and DELETE commands for the same Dataset could apply
  DELETEs before older, still-unconverged UPSERTs, causing an administrative
  Dataset delete's `converge_projection` step to fail once a relationship
  UPSERT's endpoint node had already been removed.
- Added a PostgreSQL-authoritative fence so a DELETE `graph_outbox` row for a
  Dataset cannot be claimed -- by either the autonomous consumer or an
  explicit dataset drain -- while that Dataset still has an UPSERT row
  PENDING, PROCESSING under a live lease, or FAILED with retries remaining.
  This closes a cross-row race that ordering alone did not: two different
  outbox rows can no longer be applied out of dependency order by two
  different workers, which previously risked stale projection work
  resurrecting or otherwise breaking Neo4j graph state for a Dataset already
  (or concurrently) being deleted.
- Improved `DATASET_DELETE` error classification for its projection
  convergence step: genuine transient Neo4j/transport failures are still
  reported as a retryable dependency outage, but an unexpected or
  programming-defect failure is no longer relabeled as retryable and masked
  behind indefinite retries.

### Compatibility

- No public API contract change.
- No request or response shape change.
- No database schema migration.
- No storage format change.

### Validation

`v0.1.2-rc.1` was validated against a real Easypanel deployment:

- `/health/live` and `/health/ready` PASS, with PostgreSQL, Neo4j, and the
  worker all reported ready.
- `/api/v1/info` reported `0.1.2-rc.1` running under `production`.
- Provider-backed Remember and a Recall marker check both PASS.
- A fresh administrative Dataset DELETE PASS, and the full
  `production_smoke.py` suite PASS end to end.
- The Dataset left `failed`/`deleting` by the `v0.1.1` production incident
  was recovered to `Dataset.status=DELETED` through the supported run retry
  flow, with no manual PostgreSQL, Neo4j, `graph_outbox`, or filesystem
  repair.

EASYPANEL-001's own documentation artifacts (`deploy/easypanel/compose.yaml`,
`docs/deployment/easypanel.md`) are not yet published by this entry.

## [0.1.1]

### API documentation and Swagger

- Swagger UI (`/docs`) is now available only when `APP_ENV=dev` or
  `development`; production and every other environment continue to have no
  documentation surface at all (`404`, not an auth error) for `/docs`,
  `/openapi.json`, and `/redoc` (`/redoc` is never available in any
  environment).
- Every API operation now has a meaningful summary and description, request
  fields have clear descriptions, and destructive operations (Forget,
  administrative Dataset delete) are explicitly called out as such.
- `401`, `403`, `422`, and route-specific error responses (for example
  dataset-not-found, idempotency conflicts, worker-unavailable) are now
  documented accurately against the real `ErrorEnvelope` shape this API
  actually returns, replacing FastAPI's generic default validation-error
  documentation.
- The `X-API-Key` Authorize flow in Swagger UI is unchanged and documented.
- The human-facing Swagger UI now omits the `Idempotency-Key` header input
  for readability; the canonical `/openapi.json` schema continues to fully
  document `Idempotency-Key` on every operation that accepts it.

### Compatibility

- No business API paths were removed or renamed.
- No request or response business contract was intentionally changed.
- `Idempotency-Key` runtime support and semantics are unchanged.
- Pipeline, storage, and database behavior are unchanged.
- No database migration is required to upgrade to 0.1.1.

## [0.1.0]

Sofias Memory is a focused, single-user semantic memory and knowledge graph
service: it ingests text, files, and URLs; extracts chunks, entities, and
relations; and serves them back through retrieval ranging from plain vector
search to graph-grounded, LLM-generated answers with provenance back to the
original source.

### Core memory and recall

- Ingest content via text, file upload, or a single HTTPS URL (SSRF-guarded),
  either stored as-is (`mode=ingest`) or fully processed into chunks,
  embeddings, entities, and relations (`mode=full`).
- Process pending or explicitly selected sources into semantic memory
  (Cognify), including full dataset rebuilds onto a new generation without
  ever exposing a partially-rebuilt state to readers.
- Retrieve context via six modes: vector chunks, summaries, graph traversal,
  authoritative entity/relation triplets, hybrid rank fusion, and
  graph-grounded RAG with a generated, provenance-backed answer.
- Background hygiene (Improve): feedback-weighted ranking, entity
  deduplication, relation embedding refresh, summary maintenance, and graph
  reconciliation — always explicit, never triggered implicitly.
- Every retrieved chunk, entity, and relation carries a provenance chain back
  to its originating source and document.

### Asynchronous pipelines

- Every write (Remember, Cognify, Improve, Forget, Dataset delete) is a
  durable, observable `PipelineRun`, backed by PostgreSQL as the queue and
  authority — no external broker.
- `wait=true`/`wait=false` share the exact same underlying run; a client can
  poll `GET /runs/{run_id}` for status/progress at any time.
- `Idempotency-Key` support prevents duplicate work from retried requests.
- Manual retry and cooperative cancellation for any run.
- Automatic recovery of abandoned work after a process restart or crash,
  including real OS-process-kill recovery proof.

### Dataset and deletion lifecycle

- Full dataset management: create, list, rename, inspect sources/stats.
- Forget by source, dataset, or everything (destructive "everything" scope
  requires an exact confirmation phrase).
- Administrative dataset deletion — distinct from Forget — permanently
  retires a dataset namespace with a durable tombstone; the `main` dataset
  can never be deleted this way.

### Graph projection and recovery

- Neo4j is a reconstructible projection, never authoritative; PostgreSQL is
  the sole source of truth for all knowledge, provenance, and pipeline state.
- The graph projection can be rebuilt from PostgreSQL at any time, per
  dataset or globally, via a packaged operational script.
- A transactional outbox drives projection updates with autonomous,
  crash-safe recovery.

### Operations and observability

- A single release image contains its own Alembic migration assets and
  operational scripts (`rebuild_graph.py`, `verify_installation.py`,
  `generate_api_key.py`) — no source checkout required to operate it.
- Documented, verified first-start, migration, upgrade, rollback, backup, and
  restore procedures (`docs/operations.md`), including a real non-empty
  backup → destroy → restore → rebuild drill.
- `/health/live` and `/health/ready` distinguish process liveness from full
  dependency/worker readiness.
- Structured JSON logging and PostgreSQL-derived operational metrics, with no
  external telemetry.

### Security and release packaging

- Every private route requires a static `X-API-Key`, compared in constant
  time; only `/health/*` is exempt.
- SSRF-guarded URL ingestion (loopback, link-local, private-network, and
  cloud-metadata-endpoint protections), request body size limits, and
  storage path-traversal guards.
- The container runs as a non-root user, with a read-only root filesystem,
  no extra Linux capabilities, and no privilege escalation in the reference
  Compose deployment.
- A pinned, non-floating base image and reproducible dependency lock; CI
  enforces lint/type/security checks, a runtime dependency vulnerability
  audit, and Settings/configuration parity on every change.

### Known limitations

- Single-user only: no multi-tenant support, accounts, roles, or ACLs.
- No frontend; API-only, documented in [`docs/api.md`](docs/api.md).
- Neo4j is a projection, not a database you should treat as authoritative or
  back up as a requirement.
- Schema migrations are explicit and manual — never applied automatically at
  startup.
- No guaranteed arbitrary schema downgrade; migration `0011` in particular
  cannot be reversed (PostgreSQL has no `DROP VALUE` for a native enum), so
  recovery past that point relies on a pre-upgrade backup, not `alembic
  downgrade`.
- Backup and upgrade both use a maintenance-window (quiesced) model, not
  zero-downtime hot operations.
- No external message queue, multi-worker cluster, or high-availability
  deployment model — a single application instance is the supported
  topology.
