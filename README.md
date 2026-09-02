# Sofias Memory

Sofias Memory is a focused, **single-user** semantic memory and knowledge graph
service. It ingests text, files, and URLs; extracts chunks, entities, and relations;
and serves them back through retrieval modes ranging from plain vector search to
graph-grounded, LLM-generated answers with provenance back to the original source.

## Status

The MVP operational async runtime passed its functional/integration/recovery gate
(`GATE-B5`, see `docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B5.md`).
Remember, Cognify, Recall, Improve, Forget, Dataset lifecycle, and Run
retry/cancel are all implemented and durable, running through a single internal
worker with PostgreSQL as the queue and source of truth. **v0.1.0** was the
first stable MVP release; **v0.1.1** improved Swagger/OpenAPI documentation
and developer UX; **v0.1.2** is a patch release fixing a `graph_outbox`
UPSERT/DELETE ordering and cross-row claim-race defect found during
administrative Dataset deletion (see `CHANGELOG.md`), with no business API,
pipeline, or storage contract change — see
`docs/exec-plans/active/Sofias_Memory_Release_v0.1.0_Backlog.md` for the
original release discovery/backlog.

## Principal capabilities

- **Remember** — ingest text, files, or a single HTTPS URL into a dataset,
  either as raw storage (`mode=ingest`) or fully processed into chunks, embeddings,
  entities, and relations (`mode=full`).
- **Cognify** — process pending or explicitly selected sources into semantic
  memory, including full dataset rebuilds onto a new generation.
- **Recall** — retrieve ranked context via vector chunks, lexical, summaries,
  graph traversal, hybrid rank fusion, or graph-grounded RAG with a generated
  answer and provenance references.
- **Improve** — background hygiene: feedback-weighted ranking, entity
  deduplication, relation embeddings, summaries, graph reconciliation.
- **Forget** — remove memory by source, dataset, or everything, with explicit
  confirmation for the destructive "everything" scope.
- **Dataset management** — create/list/rename/inspect datasets, and
  administratively and durably delete a dataset namespace (distinct from Forget).
- **Runs** — every write is a durable, observable `PipelineRun` you can list,
  inspect, retry, or cancel.

## Architecture

- One FastAPI application; the pipeline worker runs **inside the same process**
  (no separate worker deployment, no external queue broker).
- **PostgreSQL + pgvector is the authoritative source of truth** for all
  datasets, sources, chunks, entities, relations, pipeline run/step state, and the
  outbox that drives graph projection.
- **Neo4j is a reconstructible projection**, never authoritative — it can always
  be rebuilt from PostgreSQL (`scripts/rebuild_graph.py`).
- Source originals are stored on a local filesystem volume by default, or
  optionally in S3/an S3-compatible bucket (`STORAGE_BACKEND=s3`) — see
  `docs/operations.md` §13. S3-compatible source storage has been validated
  against real MinIO and Wasabi endpoints with no provider-specific code —
  see `docs/operations.md` §13.16 for scope and evidence.
- LLM and embedding calls go through any **OpenAI-compatible** endpoint.
- All private routes require a static `X-API-Key` header.

See the accepted architecture decisions in `docs/adr/` and the canonical product
specification in `docs/product/Sofias_Memory_PRD_SPECS.md` for the full rationale.

## Requirements

- Docker (for PostgreSQL + pgvector and Neo4j; `compose.yaml` pins
  `pgvector/pgvector:0.8.1-pg17` and `neo4j:5.26-community`), or your own instances
  of both.
- An OpenAI-compatible API key for both LLM and embedding calls.
- Python `>=3.12,<3.13` and [`uv`](https://docs.astral.sh/uv/) if running the
  application from source (see [Quick start](#quick-start) below).

## Quick start

The release image contains its own Alembic migration assets and operational
scripts, so a full first start needs no source checkout at all. See
[`docs/operations.md`](docs/operations.md) for the complete, verified
first-start/migration/upgrade/rollback/backup/restore contract — every command
there was run for real against disposable infrastructure, including a
complete non-empty backup, destroy, restore, and Neo4j-rebuild drill.

> **`alembic upgrade head` below is a FIRST INSTALL / EMPTY DATABASE step.**
> It is required once, against a brand-new, empty PostgreSQL database/volume.
> It is **not** part of an ordinary redeploy, restart, or Stack recreation
> that reuses an already-migrated PostgreSQL volume, and it is only needed
> again on a later upgrade if that specific release adds new migrations. See
> [When do I run migrations?](#when-do-i-run-migrations) below.

Minimal Compose-based flow (see `docs/operations.md` §2 for the full version):

```bash
cp .env.example .env   # set API_KEY, DB_PASSWORD, DB_NEO4J_PASSWORD, LLM_API_KEY
docker compose up -d postgres neo4j
docker compose run --rm sofias-memory alembic upgrade head
docker compose up -d sofias-memory
curl http://127.0.0.1:8000/health/ready
```

**Production deployment** (Portainer, EasyPanel, a published GHCR image, the
security checklist, and the production smoke test) is covered in
[`docs/operations.md`](docs/operations.md#12-production-deployment) — this
section and the rest of this Quick start are the source-build/local path.

Prefer running from a source checkout (e.g. for development)? See
[`docs/development.md`](docs/development.md). That flow still works and looks
like this:

```bash
# 1. Start PostgreSQL + pgvector and Neo4j, reachable from the host.
docker run -d --name sofias-postgres \
  -e POSTGRES_DB=sofias_memory \
  -e POSTGRES_USER=sofias_memory \
  -e POSTGRES_PASSWORD=change-me \
  -p 5432:5432 \
  pgvector/pgvector:0.8.1-pg17

docker run -d --name sofias-neo4j \
  -e NEO4J_AUTH=neo4j/change-me-too \
  -p 7687:7687 \
  neo4j:5.26-community

# 2. Configure the application.
cp .env.example .env
uv run python scripts/generate_api_key.py   # paste the result into API_KEY in .env
# Also set in .env: DATABASE_URL, NEO4J_URI/NEO4J_PASSWORD (pointing at the
# containers above), LLM_API_KEY, EMBEDDING_API_KEY.

# 3. Install dependencies and migrate the schema (first install only --
#    against the brand-new, empty databases started in step 1).
uv sync --dev
uv run alembic upgrade head

# 4. Run the application.
uv run uvicorn sofias_memory.app:create_app --factory --host 127.0.0.1 --port 8000

# 5. Confirm it's ready.
curl http://127.0.0.1:8000/health/ready
```

`compose.yaml` (`sofias-memory` + `postgres` + `neo4j`, application-only
published by default) is the canonical, portable local/dev stack definition
— see [Development](docs/development.md) for more on its internal-network
layout, and Portainer deployments consume it directly today (§F of
`docs/operations.md`). **EasyPanel deployments use a dedicated variant**,
[`deploy/easypanel/compose.yaml`](deploy/easypanel/compose.yaml) — see
[`docs/deployment/easypanel.md`](docs/deployment/easypanel.md) for the
verified, step-by-step guide.

### When do I run migrations?

`alembic upgrade head` is a **schema-migration** step, not a startup step —
the application never migrates itself automatically. It is required in
exactly two situations:

| Situation | Run `alembic upgrade head`? |
|---|---|
| Fresh install — brand-new, empty PostgreSQL database/volume | **Yes**, once, before first starting the application. |
| Ordinary redeploy or restart of an already-migrated deployment | No. |
| Recreating a Compose Stack/service while reusing the same, already-migrated PostgreSQL volume | No — recreating the Stack does not reset the schema. |
| Upgrading to a release with **no** new migration | No — verify readiness/smoke as usual, but there is nothing to migrate. |
| Upgrading to a release **with** one or more new migrations | **Yes**, once, using the target release's image, following the backup/quiesce upgrade procedure (`docs/operations.md` §4). |

`alembic current` and `alembic heads` are read-only inspection/verification
commands — they never mutate the schema and are safe (though not required)
to run at any time, including on every redeploy, to confirm what revision is
currently applied.

## Configuration

All settings are environment variables, validated at startup (see
`sofias_memory/config.py`). The full list with defaults is in `.env.example`;
grouped by area:

| Area | Examples |
|---|---|
| Application/API | `API_KEY`, `HTTP_HOST`, `HTTP_PORT`, `LOG_LEVEL`, `CORS_ALLOWED_ORIGINS`, `MAX_REQUEST_BODY_MB` |
| PostgreSQL | `DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` |
| Neo4j | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` |
| Storage | `DATA_DIRECTORY`, `TEMP_DIRECTORY`, `MAX_SOURCE_SIZE_MB`, `STORAGE_BACKEND`, `STORAGE_S3_*` (optional — see `docs/operations.md` §13) |
| LLM | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT_SECONDS` |
| Embeddings | `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` |
| Chunking/retrieval | `CHUNK_MAX_TOKENS`, `RECALL_DEFAULT_TOP_K`, `RECALL_RRF_K` |
| Graph/provenance | `GRAPH_SUBGRAPH_MAX_DEPTH`, `PROVENANCE_MAX_EVIDENCE` |
| Improve | `ENTITY_DEDUP_SIMILARITY_THRESHOLD`, `ENTITY_MERGE_SIMILARITY_THRESHOLD` |
| Worker | `WORKER_ENABLED`, `WORKER_POLL_INTERVAL_MS`, `WORKER_MAX_CONCURRENT_DATASETS` |
| Privacy/logging | `STORE_QUERY_CONTENT`, `LOG_DOCUMENT_CONTENT`, `LOG_LLM_PAYLOADS` |

`compose.yaml` passes every one of these through as `${VAR:-default}`
interpolations in its `sofias-memory` service, so every row above (including
the Graph/Improve settings) is configurable via the operator's own `.env`
without editing `compose.yaml` itself.

`compose.yaml` also uses two **infrastructure-only** interpolation variables that
are never read by the application itself: `DB_PASSWORD` and `DB_NEO4J_PASSWORD`,
used only to compose `DATABASE_URL`/`NEO4J_PASSWORD`/`NEO4J_AUTH` for the
containers. This keeps `extra="forbid"` meaningful on the application's own
`.env` loading — the app only ever sees its own declared Settings.

## Health and readiness

- `GET /health/live` — process liveness only. No dependency checks. Never
  requires `X-API-Key`.
- `GET /health/ready` — checks PostgreSQL (reachable, correct schema),
  Neo4j (reachable), and the internal worker (operational); with
  `STORAGE_BACKEND=s3` it also checks that startup storage convergence has
  completed (`docs/operations.md` §13). Never requires `X-API-Key`.

If `WORKER_ENABLED=false`, or the worker's background tasks are unexpectedly
dead, `/health/live` can stay `ok` while `/health/ready` reports `not_ready`:
existing reads keep working, but any request that would need to create a new
durable run is rejected (`503 WORKER_DISABLED`).

## Authentication

Every route under `/api/v1` requires an `X-API-Key` header, compared in constant
time against the configured `API_KEY`. `/health/live` and `/health/ready` are the
only exceptions. A valid key has the form `sf-` followed by at least 32 URL-safe
characters; generate one with:

```bash
uv run python scripts/generate_api_key.py
```

## API overview

See [`docs/api.md`](docs/api.md) for the full semantic guide — response envelope,
`ErrorCode` values, `Idempotency-Key` semantics, `wait=true/false`, the
`PipelineRun` lifecycle, and worked examples for every endpoint family (Remember,
Recall, Cognify, Improve, Forget, Dataset management, Dataset delete, Runs).
The formal schema is served by the running application at `/openapi.json`,
browsable via Swagger UI at `/docs` — but **only when `APP_ENV=dev` or
`APP_ENV=development`**; in every other environment (including the
`production` default) neither route is registered at all (`404`, not an
auth error) and `/redoc` is never registered in any environment. When
enabled, both `/docs` and `/openapi.json` are public (alongside `/health/*`)
so Swagger UI works in a plain browser tab; every `/api/v1/**` route
underneath still requires `X-API-Key`, entered via the UI's "Authorize"
button. See `docs/api.md` for details.

## Async runs

Every write (Remember, Cognify, Improve, Forget, Dataset delete) creates a durable
`PipelineRun`. With `wait=false` you get `202 Accepted` as soon as the run is
durably queued — the run already exists and is observable via
`GET /api/v1/runs/{run_id}` before you receive the response. `wait=true` polls the
same run to a terminal state on your behalf; it is convenience only, never a
separate synchronous code path. A run can be manually retried
(`POST /api/v1/runs/{run_id}/retry`, creating a new run with `retry_of_run_id` set)
or cancelled (`POST /api/v1/runs/{run_id}/cancel`, cooperative — an in-flight
external effect is never interrupted mid-call). See `docs/api.md` for the full
lifecycle and `Idempotency-Key` contract.

## Persistence

Three named volumes back the canonical `compose.yaml` stack:
PostgreSQL data, Neo4j data, and original source files. PostgreSQL and the source
volume are authoritative; Neo4j is always reconstructible from PostgreSQL. The
source-files volume (`DATA_DIRECTORY`) remains mandatory even with
`STORAGE_BACKEND=s3` — it holds durable ingress staging and in-transit
migration state regardless of where finalized Source originals ultimately
live (`docs/operations.md` §13.2).

## Backup and restore

**Must be backed up:** PostgreSQL and the source files volume — both are
irreplaceable. **Reconstructible:** Neo4j — it can always be rebuilt from
PostgreSQL via `scripts/rebuild_graph.py --all`, so backing it up is a
convenience, not a requirement. The full, verified backup/restore procedure —
including a real, non-empty backup → destroy → restore → Neo4j-rebuild drill —
is in [`docs/operations.md`](docs/operations.md#6-backup).

## Upgrade and migrations

Alembic is the sole authority for schema evolution; migrations are applied
**explicitly**, never automatically at application startup. `/health/ready`
detects a schema that doesn't match the application's expected revision and
reports `not_ready` rather than guessing. Downgrades are not guaranteed in
general — migration `0011`, for example, adds a native PostgreSQL enum value
and has no safe destructive downgrade (`ALTER TYPE ... DROP VALUE` does not
exist in PostgreSQL). The full migration/upgrade/rollback procedure,
including that limitation's exact operational consequence, is in
[`docs/operations.md`](docs/operations.md#3-migration-policy).

## Security notes

- Put a TLS-terminating reverse proxy in front of the application; never expose
  it directly to the internet without one.
- Only the application port needs to be reachable externally — PostgreSQL and
  Neo4j should stay on an internal network (this is already the default in
  `compose.yaml`).
- Keep all secrets (`API_KEY`, provider keys, database passwords) out of the
  repository; `.env.example` never contains real values.
- Rotate `API_KEY` or provider keys by changing the environment variable and
  restarting — there is no runtime key-management endpoint by design.
- The application container already runs as a non-root user with a read-only
  root filesystem and no extra Linux capabilities in `compose.yaml`.
- No external telemetry is sent beyond the LLM/embedding provider calls you
  configure.

## Limitations / out of scope

By design, not because they were forgotten: multi-user accounts, ACL/roles/
tenancy, a frontend, MCP integration, arbitrary Cypher execution, a runtime
settings API, cloud sync, external message queues, and a multi-worker/HA
cluster. S3-compatible remote object storage for Source originals **is**
supported (`STORAGE_BACKEND=s3`, `docs/operations.md` §13) as an explicit,
closed, first-party backend — not a generic storage-provider plugin system;
see `docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md`.
See `docs/product/Sofias_Memory_PRD_SPECS.md` for the full, authoritative
scope.

## API documentation

- Schema: `/openapi.json` (Swagger UI at `/docs`) — **development-only**
  (`APP_ENV=dev`/`development`), public and browser-usable when enabled,
  `404`/not registered otherwise; `/api/v1/**` always requires `X-API-Key`
  regardless (see `docs/api.md`).
- Human-readable semantics guide: [`docs/api.md`](docs/api.md).

## Development

See [`docs/development.md`](docs/development.md) for local toolchain setup, running
checks/tests, running the application on the host, the current local development
database stack, and operational scripts.

## License and upstream acknowledgement

Sofias Memory is licensed under the Apache License 2.0 (see `LICENSE`). It is an
independent reimplementation referencing concepts from
[`topoteretes/cognee`](https://github.com/topoteretes/cognee); see `NOTICE.md` for
the exact upstream baseline and attribution terms.
