# Configuration Reference

Sofias Memory is configured through environment variables and an optional UTF-8 `.env` file. Settings are loaded once at process startup and validated strictly.

This page is the friendly configuration reference for operators. The authoritative implementation is `sofias_memory/config.py`, and `.env.example` is the canonical deployment template.

---

## Configuration rules

- environment variable names are case-sensitive;
- unknown keys in the project `.env` file are rejected;
- settings are frozen after startup;
- secrets use secret-aware types internally and must never be logged;
- production values should be supplied through your platform's secret manager or environment injection;
- do not commit real credentials.

A minimal production deployment normally needs valid values for:

```text
API_KEY
DATABASE_URL
NEO4J_PASSWORD
COGNITIVE_IDEMPOTENCY_HMAC_KEY
LLM_API_KEY
```

`EMBEDDING_API_KEY` may also be supplied explicitly when the embedding provider uses a different credential.

---

## Application and HTTP

| Variable | Default | Purpose |
|---|---:|---|
| `APP_NAME` | `Sofias Memory` | Application name reported by the API. |
| `APP_ENV` | `production` | Runtime environment. Swagger/OpenAPI UI is only exposed for `dev`/`development`. |
| `APP_VERSION` | canonical package version | Optional deployment metadata override. |
| `API_KEY` | required | Static credential for protected `/api/v1/**` routes. Must begin with `sf-` and contain at least 32 URL-safe random characters after the prefix. |
| `HTTP_HOST` | `0.0.0.0` | Bind address. |
| `HTTP_PORT` | `8000` | HTTP port. |
| `LOG_LEVEL` | `INFO` | Application log level. |
| `CORS_ALLOWED_ORIGINS` | empty | Explicit CORS origin allowlist. |
| `MAX_REQUEST_BODY_MB` | `50` | Maximum HTTP request body size. |
| `REQUEST_WAIT_TIMEOUT_SECONDS` | `30` | Maximum request-side wait for `wait=true` pipeline operations. |

Generate a strong API key rather than editing the example placeholder literally.

---

## PostgreSQL + pgvector

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_URL` | required | Async SQLAlchemy URL. Must use `postgresql+asyncpg://`. |
| `DATABASE_POOL_SIZE` | `10` | Base PostgreSQL connection-pool size. |
| `DATABASE_MAX_OVERFLOW` | `10` | Additional temporary pool capacity. |
| `DATABASE_MIGRATION_MODE` | `auto` | `auto` or `verify_only`. |

`auto` is the normal deployment mode. It runs the serialized migration bootstrap for a pristine database or a known older schema.

`verify_only` never invokes Alembic. It is intended for environments where schema migration is controlled externally; readiness remains false when the schema is behind.

PostgreSQL + pgvector is the authoritative store for durable knowledge, provenance, pipeline state, Sessions, Skills, Agent Profiles, and Cognitive Memory.

---

## Neo4j

| Variable | Default | Purpose |
|---|---:|---|
| `NEO4J_URI` | `bolt://neo4j:7687` | Bolt/Neo4j connection URI. |
| `NEO4J_USERNAME` | `neo4j` | Database username. |
| `NEO4J_PASSWORD` | required | Database password. |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database name. |

Neo4j is a rebuildable projection used by Knowledge Memory graph workloads. It is not the source of truth and is not used by Native Cognitive Memory.

See [Graph & Neo4j](Graph-and-Neo4j).

---

## Native Cognitive Memory

| Variable | Default | Purpose |
|---|---:|---|
| `COGNITIVE_IDEMPOTENCY_HMAC_KEY` | required | Independent HMAC-SHA-256 secret used to fingerprint Cognitive mutation requests. |

Requirements:

- at least 32 characters;
- high entropy;
- never reuse `API_KEY`;
- never reuse across environments;
- do not rotate casually if historical idempotency replay behavior must remain valid.

A suitable test/development value can be generated with:

```bash
openssl rand -hex 32
```

---

## Local source storage

| Variable | Default | Purpose |
|---|---:|---|
| `DATA_DIRECTORY` | `/data/sources` | Durable source ingress, recovery data, and filesystem-backed finalized originals. Remains required even when S3 is enabled. |
| `TEMP_DIRECTORY` | `/data/tmp` | Temporary processing workspace. |
| `MAX_SOURCE_SIZE_MB` | `50` | Maximum accepted source size. |
| `STORAGE_BACKEND` | `filesystem` | Write backend for newly finalized source originals: `filesystem` or `s3`. |

Changing `STORAGE_BACKEND` controls where **new** finalized originals are written. Existing objects continue to be read/deleted according to their persisted `storage_uri`.

See [Storage & S3](Storage-and-S3).

---

## S3-compatible object storage

These settings are used when `STORAGE_BACKEND=s3`.

| Variable | Default | Purpose |
|---|---:|---|
| `STORAGE_S3_BUCKET` | required for S3 | Bucket name. |
| `STORAGE_S3_PREFIX` | empty | Application-owned key prefix. |
| `STORAGE_S3_REGION` | provider default | S3 region. |
| `STORAGE_S3_ENDPOINT_URL` | unset | Custom S3-compatible endpoint, e.g. MinIO. |
| `STORAGE_S3_ACCESS_KEY_ID` | unset | Optional static credential. |
| `STORAGE_S3_SECRET_ACCESS_KEY` | unset | Optional static credential. |
| `STORAGE_S3_SESSION_TOKEN` | unset | Optional temporary session credential. |
| `STORAGE_S3_MAX_CONCURRENCY` | `4` | Maximum concurrent S3 operations. |

Static S3 credentials are optional. When omitted, the standard AWS credential provider chain can be used.

The S3 prefix must not contain ambiguous path segments such as `.` or `..`, and Sofias Memory must be the exclusive writer to the configured prefix.

---

## LLM provider

| Variable | Default | Purpose |
|---|---:|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL. |
| `LLM_API_KEY` | required | Provider credential. |
| `LLM_MODEL` | `gpt-5-mini` | Default LLM model. |
| `LLM_TIMEOUT_SECONDS` | `120` | Request timeout. |
| `LLM_MAX_RETRIES` | `3` | Provider retry count. |
| `LLM_MAX_CONCURRENCY` | `4` | Concurrent LLM requests. |

The provider boundary is OpenAI-compatible; a compatible self-hosted or third-party endpoint can be configured through `LLM_BASE_URL`.

---

## Embeddings

| Variable | Default | Purpose |
|---|---:|---|
| `EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible embedding endpoint. |
| `EMBEDDING_API_KEY` | unset | Optional embedding-specific credential. |
| `EMBEDDING_MODEL` | `text-embedding-3-large` | Embedding model. |
| `EMBEDDING_DIMENSIONS` | `3072` | Required vector dimensions for the current schema. |
| `EMBEDDING_BATCH_SIZE` | `64` | Embedding batch size. |
| `EMBEDDING_MAX_CONCURRENCY` | `4` | Concurrent embedding requests. |

The current v0.7 schema expects 3072-dimensional embeddings. Do not change this value independently of the database/schema contract.

---

## Chunking

| Variable | Default |
|---|---:|
| `CHUNK_MAX_TOKENS` | `900` |
| `CHUNK_OVERLAP_TOKENS` | `120` |
| `CHUNK_MIN_TOKENS` | `40` |

These values influence how newly cognified documents are split. Treat changes as retrieval-quality changes rather than cosmetic configuration.

---

## Recall

| Variable | Default |
|---|---:|
| `RECALL_VECTOR_TOP_K` | `50` |
| `RECALL_LEXICAL_TOP_K` | `50` |
| `RECALL_GRAPH_SEED_TOP_K` | `10` |
| `RECALL_GRAPH_DEPTH` | `2` |
| `RECALL_GRAPH_MAX_NODES` | `100` |
| `RECALL_DEFAULT_TOP_K` | `12` |
| `RECALL_MAX_TOP_K` | `100` |
| `RECALL_RRF_K` | `60` |

These settings control Knowledge Memory retrieval defaults and bounds. Native Cognitive Memory has its own typed recall request contract.

---

## Session context

| Variable | Default |
|---|---:|
| `SESSION_CONTEXT_MAX_ENTRIES` | `20` |
| `SESSION_CONTEXT_MAX_CHARS` | `16000` |

These limits apply when Knowledge Recall explicitly includes Session context for RAG generation.

---

## Graph and provenance

| Variable | Default |
|---|---:|
| `GRAPH_SUBGRAPH_MAX_DEPTH` | `3` |
| `GRAPH_SUBGRAPH_MAX_RELATIONS` | `200` |
| `GRAPH_PATH_MAX_DEPTH` | `4` |
| `PROVENANCE_MAX_EVIDENCE` | `50` |

These are safety/response bounds for graph traversal and evidence hydration.

---

## Improve

| Variable | Default |
|---|---:|
| `ENTITY_DEDUP_SIMILARITY_THRESHOLD` | `0.90` |
| `ENTITY_MERGE_SIMILARITY_THRESHOLD` | `0.95` |

These thresholds affect maintenance behavior. Changes should be validated against real knowledge data before being promoted to production.

---

## Worker

| Variable | Default |
|---|---:|
| `WORKER_ENABLED` | `true` |
| `WORKER_POLL_INTERVAL_MS` | `500` |
| `WORKER_STALE_AFTER_SECONDS` | `300` |
| `WORKER_MAX_CONCURRENT_DATASETS` | `1` |
| `WORKER_MAX_CONCURRENT_READS` | `8` |

Disabling the worker prevents new durable pipeline work from being executed and may cause write endpoints that require it to return `WORKER_DISABLED`/`503`.

See [Runs & Reliability](Runs-and-Reliability).

---

## Privacy and logging

| Variable | Default | Purpose |
|---|---:|---|
| `STORE_QUERY_CONTENT` | `true` | Whether Recall query text is durably persisted where the query model permits it. |
| `LOG_DOCUMENT_CONTENT` | `false` | Opt-in document-content logging. Keep false in normal deployments. |
| `LOG_LLM_PAYLOADS` | `false` | Opt-in provider-payload logging. Keep false unless deliberately debugging in a safe environment. |

Never enable content/payload logging casually in production.

---

## Recommended production baseline

```text
APP_ENV=production
DATABASE_MIGRATION_MODE=auto
STORAGE_BACKEND=filesystem  # or s3 after configuring it deliberately
WORKER_ENABLED=true
LOG_DOCUMENT_CONTENT=false
LOG_LLM_PAYLOADS=false
```

Also:

- use high-entropy independent secrets;
- terminate TLS at your reverse proxy/load balancer;
- restrict PostgreSQL and Neo4j to private networks;
- persist `DATA_DIRECTORY` even when S3 is used;
- monitor `/health/ready` rather than liveness alone;
- back up PostgreSQL and source-object storage together.

See [Security Model](Security-Model), [Operations & Deployment](Operations-and-Deployment), and the canonical [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).
