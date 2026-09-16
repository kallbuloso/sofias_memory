# Operations & Deployment

This page is the user-facing operational overview for running Sofias Memory.

It covers deployment topology, health/readiness, migrations, upgrades, backups, storage, GHCR images, and production exposure without replacing the canonical [`docs/operations.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

For release-specific or disaster-recovery work, always use the versioned Operations Guide shipped with the release you are running.

---

## 1. Runtime topology

The standard deployment contains three core services:

```text
sofias-memory
postgres + pgvector
neo4j
```

and persistent source-original storage through either:

```text
filesystem
```

or an S3-compatible backend.

The authority model is intentionally asymmetric:

```text
PostgreSQL + pgvector = authoritative durable state
source originals      = authoritative input bytes
Neo4j                  = rebuildable projection
```

Never treat Neo4j as the source of truth over PostgreSQL.

---

## 2. Published image

Stable releases are published to GHCR under:

```text
ghcr.io/kallbuloso/sofias-memory:<version>
```

For example:

```text
ghcr.io/kallbuloso/sofias-memory:0.7.0
```

Production deployments should pin an explicit release tag or digest rather than floating on an unversioned image.

The release pipeline records OCI metadata including the application version, source repository, and immutable source revision.

---

## 3. Docker Compose quick deployment

From a source checkout:

```bash
git clone https://github.com/kallbuloso/sofias_memory.git
cd sofias_memory
cp .env.example .env
```

Configure required secrets, then:

```bash
docker compose up -d
```

At minimum, configure strong values for:

```text
API_KEY
DB_PASSWORD
DB_NEO4J_PASSWORD
LLM_API_KEY
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

If your embedding provider requires a separate credential, configure:

```text
EMBEDDING_API_KEY
```

`COGNITIVE_IDEMPOTENCY_HMAC_KEY` must be a separate high-entropy secret. Do not reuse the API key or another deployment secret.

---

## 4. Health vs readiness

Sofias Memory exposes two public health endpoints:

```text
GET /health/live
GET /health/ready
```

### Liveness

`/health/live` answers whether the process is alive.

A live process is not necessarily safe for business traffic.

### Readiness

`/health/ready` answers whether the application is operationally ready.

Readiness includes the service's current bootstrap/dependency state. For example, the process may be live but not ready while an automatic database migration is running.

Deployment automation should route business traffic only after readiness succeeds.

---

## 5. Database migration modes

Alembic is the sole authority for PostgreSQL schema evolution.

Sofias Memory supports two operational modes:

```text
DATABASE_MIGRATION_MODE=auto
DATABASE_MIGRATION_MODE=verify_only
```

### `auto` — default

On startup, the application classifies the current database revision.

For a fresh database or a recognized ancestor revision, it can automatically migrate forward to the application's expected head under a serialized PostgreSQL advisory lock.

Readiness stays closed until migration/bootstrap has completed successfully.

This means ordinary installs and upgrades normally do **not** require an operator to run Alembic manually first.

### `verify_only`

The application never advances the schema automatically.

If the database is not already at the exact expected revision, the process can remain live but not ready.

Use this mode when you explicitly want to inspect or control migration timing yourself.

---

## 6. Current release migration state

For `v0.7.0`:

```text
Alembic head = 0018
```

The v0.7 migration adds the Native Cognitive Memory persistence surfaces:

```text
memory_items
memory_provenance
cognitive_memory_idempotency
```

A normal upgrade from v0.6.0 moves:

```text
0017 -> 0018
```

Under the default `DATABASE_MIGRATION_MODE=auto`, that upgrade is performed by the application's serialized migration bootstrap.

---

## 7. Fail-closed migration behavior

Automatic migration is intentionally conservative.

Sofias Memory does not guess how to repair states such as:

```text
unversioned but non-empty schema
multiple Alembic heads
foreign/diverged revision
unrecognized schema history
```

Those states require operator investigation.

The application should remain not-ready instead of stamping or mutating an ambiguous schema automatically.

---

## 8. Manual Alembic operations

Manual migration remains supported.

With Docker Compose:

```bash
docker compose run --rm sofias-memory alembic current
docker compose run --rm sofias-memory alembic heads
docker compose run --rm sofias-memory alembic upgrade head
```

`current` and `heads` are read-only diagnostics.

`upgrade head` mutates the schema and should be used deliberately, especially in production.

When operating in `verify_only`, manual migration is required before readiness can succeed on an older schema.

---

## 9. Upgrade pattern

A safe release upgrade follows this sequence:

```text
1. read release notes
2. take a backup
3. obtain/pin the target image
4. quiesce application writes
5. optionally run migration explicitly, or let auto mode handle it
6. start target image
7. wait for /health/ready
8. run a smoke check
```

Sofias Memory does not currently promise zero-downtime schema upgrades.

For a single-user/self-hosted service, the documented maintenance-window model favors recoverability over rolling-migration complexity.

---

## 10. What must be backed up

Authoritative data that must be protected:

### PostgreSQL

Contains durable application state, knowledge authority, Cognitive Memory, provenance, Sessions, Skills, Agents, Runs, idempotency evidence, and graph-outbox authority.

### Source originals

With filesystem storage, preserve the persistent source volume mounted at:

```text
/data/sources
```

With S3-compatible storage, protect the configured bucket/prefix according to your normal object-storage durability policy.

### Neo4j

Neo4j may be backed up as an operational convenience, but it is not required for authoritative recovery because the graph projection can be rebuilt from PostgreSQL.

---

## 11. Restore principle

A recovery should restore authoritative state first:

```text
PostgreSQL
source originals
```

Then verify schema/application compatibility and rebuild reconstructible projections as required.

Do not restore Neo4j and then overwrite PostgreSQL truth from it.

The packaged graph rebuild tooling exists specifically because Neo4j is a projection.

---

## 12. Filesystem vs S3-compatible source storage

Sofias Memory supports:

```text
STORAGE_BACKEND=filesystem
STORAGE_BACKEND=s3
```

### Filesystem

Simple default for local/self-hosted deployments. Durable source bytes live in the persistent source volume.

### S3-compatible

Useful when source-original durability/scale belongs in object storage.

PostgreSQL remains authoritative for metadata/state. Object bytes for finalized S3-backed Sources live in the configured bucket/prefix.

Switching an existing installation from filesystem to S3 has a dedicated convergence/migration procedure. Do not treat it as a simple config toggle on an existing corpus without reading the canonical Operations Guide.

---

## 13. Neo4j operations

Neo4j exists for knowledge-graph workloads.

Important boundary:

```text
Knowledge graph projection -> Neo4j
Cognitive Memory           -> PostgreSQL only
Sessions/Skills/Agents     -> PostgreSQL only
```

If Neo4j projection state is lost or needs reconciliation, rebuild/reconciliation tooling should derive it from PostgreSQL authority rather than the reverse.

---

## 14. Worker and durable Runs

Knowledge ingestion/maintenance uses durable `PipelineRun`s and a background worker.

Operationally this means:

- a successfully queued operation survives the originating HTTP request;
- progress can be observed through `/api/v1/runs/**`;
- failed/cancelled eligible runs can be retried as new attempts;
- readiness includes worker-operational state where required.

Cognitive Memory Create/Get/Recall/Supersede/Forget is synchronous and does not use `PipelineRun`.

---

## 15. Production exposure

Expose only the Sofias Memory HTTP service to clients.

Do not expose PostgreSQL or Neo4j publicly unless your own infrastructure has a separate, deliberate administrative requirement.

The normal public application surface is the service's HTTP port (container port `8000`) behind your reverse proxy/platform routing.

All `/api/v1/**` business endpoints require `X-API-Key`.

---

## 16. Swagger/OpenAPI in production

The interactive documentation surface is intentionally environment-gated.

With production settings, runtime:

```text
/docs
/openapi.json
```

are not registered and return `404`.

They are available only when `APP_ENV` is explicitly development-oriented (`dev` or `development`).

Use the repository documentation/Wiki as the normal production reference.

---

## 17. Easypanel deployment

The repository includes:

```text
deploy/easypanel/compose.yaml
```

The Easypanel topology follows the same core three-service model but uses a published image rather than a local Docker build and relies on the platform to route a domain to the application's internal port.

Key rules:

- keep PostgreSQL and Neo4j private;
- configure required secrets in Easypanel's environment management;
- wait for both database services and application readiness;
- route the public domain only to `sofias-memory` port `8000`;
- keep `DATABASE_MIGRATION_MODE=auto` unless you deliberately require explicit migration control.

For the platform-specific walkthrough, read:

[Easypanel Deployment Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/deployment/easypanel.md)

---

## 18. Smoke verification

After installation or upgrade, verify at least:

```bash
curl "$SOFIAS_URL/health/live"
curl "$SOFIAS_URL/health/ready"
curl -H "X-API-Key: $SOFIAS_KEY" "$SOFIAS_URL/api/v1/info"
```

Then execute a non-production functional smoke appropriate for the features your integration depends on.

The repository also contains packaged operational/testing scripts used by release validation; see `scripts/` and the canonical Operations Guide before running them against a real environment.

---

## 19. Production checklist

Before considering a deployment ready:

```text
[ ] explicit release image/tag/digest selected
[ ] API_KEY configured securely
[ ] database passwords configured securely
[ ] LLM/embedding credentials configured
[ ] COGNITIVE_IDEMPOTENCY_HMAC_KEY configured separately
[ ] persistent PostgreSQL storage configured
[ ] durable source-original storage configured
[ ] Neo4j storage available or rebuild plan understood
[ ] DATABASE_MIGRATION_MODE chosen intentionally
[ ] /health/live passes
[ ] /health/ready passes
[ ] /api/v1/info reports expected release/contracts
[ ] PostgreSQL/Neo4j are not unintentionally public
[ ] backup policy exists for authoritative data
[ ] upgrade/restore procedure has been reviewed
```

---

## 20. Canonical references

- [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
- [Easypanel Deployment](https://github.com/kallbuloso/sofias_memory/blob/main/docs/deployment/easypanel.md)
- [ADR-0015 — Automatic Serialized Migration Bootstrap](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0015-automatic-serialized-migration-bootstrap.md)
- [ADR-0002 — PostgreSQL authority / Neo4j projection](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md)
- [ADR-0011 — Durable Source Object Storage](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md)

For first-time setup, start with [Getting Started](Getting-Started). For client behavior, continue with [API Guide](API-Guide) and [Integration Guide](Integration-Guide).
