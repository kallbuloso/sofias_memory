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
worker with PostgreSQL as the queue and source of truth. **v0.1.0 is currently in
release preparation** (release-candidate quality; not yet tagged/published) — see
`docs/exec-plans/active/Sofias_Memory_Release_v0.1.0_Backlog.md` for exactly what
remains before the tag.

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
- Source originals are stored on a local filesystem volume.
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

The release image now contains its own Alembic migration assets and operational
scripts (no source checkout required to run `alembic upgrade head` or
`scripts/rebuild_graph.py` from inside it) — but the finalized, image-based
first-start/upgrade/backup procedure is still being written; see
`docs/exec-plans/active/Sofias_Memory_Release_v0.1.0_Backlog.md` (REL-003) for
what remains before the v0.1.0 tag. The flow below reflects what is documented
and verified today, running the application from a source checkout against
PostgreSQL and Neo4j reachable from the host.

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

# 3. Install dependencies and migrate the schema.
uv sync --dev
uv run alembic upgrade head

# 4. Run the application.
uv run uvicorn sofias_memory.app:create_app --factory --host 127.0.0.1 --port 8000

# 5. Confirm it's ready.
curl http://127.0.0.1:8000/health/ready
```

`compose.yaml` is the canonical, portable stack definition
(`sofias-memory` + `postgres` + `neo4j`, application-only published by default,
hardened with `read_only`/`cap_drop: ALL`/`no-new-privileges`) and is what
Portainer/EasyPanel consume directly — see [Development](docs/development.md) for
more on its internal-network layout. The application image can already run its
own migration and operational scripts standalone (verified against a real,
disposable PostgreSQL and Neo4j, with no source bind-mounted in); documenting
the exact end-to-end first-start/upgrade sequence built entirely on top of
`compose.yaml` is REL-003's remaining work before the v0.1.0 tag — until then,
the source-checkout flow above is the documented quick start.

## Configuration

All settings are environment variables, validated at startup (see
`sofias_memory/config.py`). The full list with defaults is in `.env.example`;
grouped by area:

| Area | Examples |
|---|---|
| Application/API | `API_KEY`, `HTTP_HOST`, `HTTP_PORT`, `LOG_LEVEL`, `CORS_ALLOWED_ORIGINS`, `MAX_REQUEST_BODY_MB` |
| PostgreSQL | `DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` |
| Neo4j | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` |
| Storage | `DATA_DIRECTORY`, `TEMP_DIRECTORY`, `MAX_SOURCE_SIZE_MB` |
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
  Neo4j (reachable), and the internal worker (operational). Never requires
  `X-API-Key`.

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
The formal, always-current schema is served directly by the running application at
`/openapi.json` (and Swagger UI at `/docs`) — both require `X-API-Key` like every
other route, so open them with `curl -H "X-API-Key: ..."` rather than a plain
browser tab; see `docs/api.md` for why.

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
volume are authoritative; Neo4j is always reconstructible from PostgreSQL.

## Backup and restore (overview)

**Must be backed up:** PostgreSQL and the source files volume — both are
irreplaceable. **Reconstructible:** Neo4j — it can always be rebuilt from
PostgreSQL via `scripts/rebuild_graph.py --all` (now packaged in the release
image itself, no source checkout needed), so backing it up is a convenience,
not a requirement. A concrete, step-by-step backup/restore procedure is being
finalized as release work — see
`docs/exec-plans/active/Sofias_Memory_Release_v0.1.0_Backlog.md` (REL-003).

## Upgrade and migrations

Alembic is the sole authority for schema evolution; migrations are applied
**explicitly**, never automatically at application startup. `/health/ready`
detects a schema that doesn't match the application's expected revision and
reports `not_ready` rather than guessing. Downgrades are not guaranteed in
general — migration `0011`, for example, adds a native PostgreSQL enum value and
has no safe destructive downgrade (`ALTER TYPE ... DROP VALUE` does not exist in
PostgreSQL). The release image now contains its own migration assets
(`alembic upgrade head` runs from inside it, no source checkout needed); the
finalized, documented upgrade procedure built on top of that is release work in
progress (REL-003).

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
settings API, cloud sync, external message queues, a multi-worker/HA cluster, or
remote object storage. See `docs/product/Sofias_Memory_PRD_SPECS.md` for the full,
authoritative scope.

## API documentation

- Schema: `/openapi.json` (Swagger UI at `/docs`), served directly by the running
  application — **both require `X-API-Key`** like every other route, so retrieve
  them with `curl -H "X-API-Key: ..."`, not a plain browser tab (see
  `docs/api.md`).
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
