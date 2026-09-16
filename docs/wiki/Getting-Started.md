# Getting Started

This guide takes you from a fresh checkout to a running Sofias Memory instance and your first persisted Cognitive Memory.

For production operations, backup/restore, migration policy, S3 storage, and recovery, use the versioned [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

---

## 1. What you need

The easiest path uses Docker Compose.

You need:

- Docker with Compose support;
- an OpenAI-compatible LLM endpoint and API key;
- an OpenAI-compatible embeddings endpoint and API key;
- local ports required by your deployment available;
- a few application secrets configured in `.env`.

The canonical stack runs:

- Sofias Memory;
- PostgreSQL + pgvector;
- Neo4j.

PostgreSQL + pgvector is authoritative. Neo4j is a rebuildable knowledge-graph projection.

---

## 2. Clone the repository

```bash
git clone https://github.com/kallbuloso/sofias_memory.git
cd sofias_memory
cp .env.example .env
```

The current stable release is `v0.7.0`. For reproducible production deployments, prefer the published release/tag or GHCR image instead of an arbitrary development commit.

Published image:

```text
ghcr.io/kallbuloso/sofias-memory:0.7.0
```

---

## 3. Configure the required secrets

Open `.env` and provide, at minimum, valid values for the secrets used by the application and infrastructure.

The important ones for a normal first start are:

```dotenv
API_KEY=sf-<your-generated-api-key>
DB_PASSWORD=<postgres-password>
DB_NEO4J_PASSWORD=<neo4j-password>
LLM_API_KEY=<provider-key>
EMBEDDING_API_KEY=<provider-key>
COGNITIVE_IDEMPOTENCY_HMAC_KEY=<separate-high-entropy-secret-at-least-32-characters>
```

`COGNITIVE_IDEMPOTENCY_HMAC_KEY` must be a separate high-entropy secret. Do not reuse `API_KEY`. It keys the non-reversible request digest used by Cognitive Memory idempotency.

For the complete configuration reference, use `.env.example` in the repository and the [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

---

## 4. Start Sofias Memory

The default migration mode is:

```dotenv
DATABASE_MIGRATION_MODE=auto
```

With `auto`, a fresh database or a known older Sofias Memory schema is migrated automatically during application startup under the project's serialized migration bootstrap.

Start the stack:

```bash
docker compose up -d
```

You do **not** need to run `alembic upgrade head` manually for the normal first-start path when `DATABASE_MIGRATION_MODE=auto` is enabled.

If you deliberately use `DATABASE_MIGRATION_MODE=verify_only`, the application never applies migrations automatically. In that mode, the schema must already be at the exact expected revision before readiness can become healthy.

---

## 5. Check liveness and readiness

Process liveness:

```bash
curl http://127.0.0.1:8000/health/live
```

Dependency and schema readiness:

```bash
curl http://127.0.0.1:8000/health/ready
```

`/health/live` answers whether the process is alive.

`/health/ready` is stricter: it verifies that the application is operational against its required dependencies and current schema before reporting ready.

---

## 6. Check the API contract

All `/api/v1/**` routes require `X-API-Key`.

```bash
export SOFIAS_URL=http://127.0.0.1:8000
export SOFIAS_KEY='sf-...'

curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/info"
```

For v0.7.0, `/api/v1/info` advertises Cognitive Memory contract version `1` and the five Cognitive Memory capabilities:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

Callers should use this machine-readable contract instead of guessing capabilities from the application SemVer.

---

## 7. Create your first Cognitive Memory

This example stores a durable profile memory in global scope.

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Idempotency-Key: getting-started-profile-001" \
  -H "Content-Type: application/json" \
  -d '{
    "memory_type": "profile",
    "scope": "global",
    "content": "Prefers concise technical explanations.",
    "provenance": {
      "origin_kind": "user_asserted",
      "source_system": "getting-started",
      "source_ref": "example:profile"
    }
  }'
```

A successful create returns a first-class `MemoryItem`.

Important concepts already visible in this request:

- `memory_type` is explicit;
- `scope` is explicit;
- provenance is first-class, not an unstructured metadata bag;
- `Idempotency-Key` protects retries of the same mutation;
- the generated embedding is authoritative internally but is never returned in the public API.

---

## 8. Recall relevant Cognitive Memory

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories/recall" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How should technical explanations be presented?",
    "scopes": ["global"],
    "top_k": 5
  }'
```

Typed Cognitive Recall returns:

- the matching `MemoryItem`;
- cosine `relevance`;
- an explicit `is_current_truth` value.

Cognitive Recall is different from the legacy Knowledge Memory `/api/v1/recall` endpoint. The two memory planes are intentionally separate.

---

## 9. Read a memory directly

Use the `memory_id` returned by Create:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/memories/<memory_id>"
```

Get is an identity lookup. It does not perform semantic search.

---

## 10. What to learn next

Read [Core Concepts](Core-Concepts) before designing a production integration. In particular, understand:

- Knowledge Memory vs. Cognitive Memory;
- Dataset / Source / Document / Chunk;
- `MemoryItem` types and scopes;
- provenance;
- current truth and historical `as_of` recall;
- Supersede vs. Forget;
- Sessions, Skills, Agent profiles, and Runs.

For complete request/response schemas and error semantics, use the versioned [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## Development Swagger UI

When `APP_ENV=dev` or `APP_ENV=development`, the application exposes:

```text
/docs
/openapi.json
```

Those routes are intentionally not registered in production mode. The private API itself still requires `X-API-Key` when Swagger/OpenAPI is enabled.

---

## Troubleshooting the first start

If `/health/live` works but `/health/ready` does not, do not bypass readiness. Check the application logs and the [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

Common categories include database connectivity, Neo4j connectivity, schema readiness, provider configuration, or storage convergence.

Sofias Memory intentionally fails closed on ambiguous or unsafe schema states rather than guessing, stamping, or silently repairing them.

---

## Documentation authority

This page is a user-facing guide. The versioned repository remains authoritative for exact operational and API behavior:

- [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
- [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [Architecture decisions](https://github.com/kallbuloso/sofias_memory/tree/main/docs/adr)
- [Product contracts](https://github.com/kallbuloso/sofias_memory/tree/main/docs/product)
