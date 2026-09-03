# Changelog

All notable, user-facing changes to Sofias Memory are documented in this file.

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
