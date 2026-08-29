# Changelog

All notable, user-facing changes to Sofias Memory are documented in this file.

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
