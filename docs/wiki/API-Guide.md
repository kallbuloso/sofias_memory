# API Guide

Sofias Memory exposes a versioned HTTP API for applications, automations, agent runtimes, internal tools, and other software that needs durable memory infrastructure.

This page is a **user-facing map of the API**. It explains the public conventions and endpoint families without duplicating every request/response schema.

For the canonical, versioned API contract, use [`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 1. Base URL

A typical deployment might expose Sofias Memory at:

```text
https://memory.example.com
```

Versioned application routes live under:

```text
/api/v1/**
```

Health checks are intentionally outside that prefix:

```text
GET /health/live
GET /health/ready
```

---

## 2. Authentication

Every private `/api/v1/**` operation requires the static API key header:

```http
X-API-Key: <your-api-key>
```

Example:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/info"
```

The only public operational routes are:

```text
GET /health/live
GET /health/ready
```

Treat the API key as a secret. Store it in your application or automation platform's credential store rather than embedding it in source code or workflow definitions.

---

## 3. Response envelopes

Successful API responses use a common envelope:

```json
{
  "data": {},
  "meta": {
    "request_id": "...",
    "timestamp": "2026-09-16T00:00:00Z"
  }
}
```

`request_id` is the correlation identity to keep when troubleshooting a request.

Errors use a stable error envelope:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Safe public message",
    "details": {},
    "request_id": "..."
  }
}
```

Integrations should branch on the stable `error.code` and HTTP status, not on human-readable message text.

Common stable codes include:

```text
INVALID_REQUEST
MISSING_API_KEY
INVALID_API_KEY
DEPENDENCY_UNAVAILABLE
REQUEST_TOO_LARGE
IDEMPOTENCY_CONFLICT
RESERVED_IDEMPOTENCY_KEY_NAMESPACE
MEMORY_NOT_FOUND
MEMORY_STATE_CONFLICT
SESSION_ARCHIVED
WORKER_DISABLED
RUN_NOT_RETRYABLE
```

The canonical list evolves with the API contract; consult `docs/api.md` for operation-specific responses.

---

## 4. Capability negotiation

Before relying on an optional contract, call:

```text
GET /api/v1/info
```

For Native Cognitive Memory v1, the service advertises:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

Use these machine-readable capabilities instead of inferring support from the application SemVer alone.

---

## 5. API families

Sofias Memory deliberately exposes multiple domain families rather than one generic "memory" endpoint.

| Area | Primary public surface | Purpose |
|---|---|---|
| Health | `/health/live`, `/health/ready` | Process liveness and operational readiness |
| Info | `/api/v1/info` | Version, contract and capability negotiation |
| Knowledge ingestion | `/api/v1/remember` | Store text, files and HTTPS URL sources |
| Knowledge processing | `/api/v1/cognify` | Turn durable sources into chunks, embeddings and semantic knowledge |
| Knowledge retrieval | `/api/v1/recall` | Vector, lexical, summary, graph, hybrid and RAG-oriented retrieval |
| Knowledge maintenance | `/api/v1/improve` | Explicit ranking/graph maintenance and reconciliation |
| Knowledge Forget | `/api/v1/forget` | Forget source-backed memory by its supported scope |
| Datasets | `/api/v1/datasets/**` | Manage logical knowledge namespaces and inspect source/statistics state |
| Runs | `/api/v1/runs/**` | Observe, cancel and retry durable knowledge-pipeline runs |
| Graph | graph routes under `/api/v1` | Read projected entities/relationships |
| Provenance | provenance routes under `/api/v1` | Trace retrieved evidence to sources/documents/chunks |
| Feedback | feedback routes under `/api/v1` | Record relevance feedback used by maintenance flows |
| Sessions | `/api/v1/sessions/**` | Durable temporal context and append-only SessionEntry history |
| Skills | `/api/v1/skills/**` | Versioned procedural memory and semantic Skill discovery |
| Agents | `/api/v1/agents/**` | Durable Agent Profile management and explicit Skill/Session associations |
| Cognitive Memory | `/api/v1/memories/**` | Typed durable facts/preferences with lifecycle and temporal truth |

Read [Knowledge Memory](Knowledge-Memory), [Cognitive Memory](Cognitive-Memory), [Sessions](Sessions), and [Skills & Agents](Skills-and-Agents) before choosing an endpoint family.

---

## 6. Cognitive Memory endpoints

Native Cognitive Memory exposes exactly five operations:

```text
POST /api/v1/memories
GET  /api/v1/memories/{memory_id}
POST /api/v1/memories/recall
POST /api/v1/memories/{memory_id}/supersede
POST /api/v1/memories/{memory_id}/forget
```

There is intentionally no generic:

```text
PATCH /api/v1/memories/{memory_id}
```

A changed truth is represented by **Supersede**. Destructive erasure is represented by **Forget**.

See [Cognitive Memory](Cognitive-Memory) for lifecycle semantics and examples.

---

## 7. Durable knowledge pipeline operations

Knowledge writes such as Remember, Cognify, Improve, Forget, and administrative Dataset deletion are represented by durable `PipelineRun`s.

The public run surface is:

```text
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/retry
```

`GET /runs` can filter by status, pipeline type, dataset and associated `session_uuid`.

Cancellation is cooperative. A running external call is not forcibly killed in the middle; a cancelling run finishes the current safe point and converges to a terminal state.

Retry creates a **new run/attempt**. It does not rewrite the historical failed/cancelled run.

When a knowledge operation offers `wait=false`, the request can return after the run has been durably queued; poll the corresponding run for progress. `wait=true` observes the same durable run while the HTTP request waits for a terminal result up to the configured timeout.

---

## 8. Idempotency

Idempotency semantics differ by domain.

### Cognitive Memory mutations

Create, Supersede and Forget support the `Idempotency-Key` header.

```http
Idempotency-Key: customer-42-preference-001
```

Use the **same key for retries of the same logical mutation**.

```text
same key + same semantic request
→ replay original logical outcome

same key + different semantic request
→ IDEMPOTENCY_CONFLICT
```

Idempotency does not mean semantic deduplication. Two identical facts created intentionally under different keys can still be distinct memories.

### Session entries

SessionEntry append uses an optional body field named `external_id`, unique within a Session, to make one exact append safely replayable.

Read [Sessions](Sessions) for that model.

---

## 9. Pagination

Collection endpoints generally expose bounded pagination using:

```text
limit
offset
```

The precise defaults and maximums are endpoint-specific and are documented in the canonical OpenAPI/schema.

Do not assume every association collection is paginated: some explicit management association endpoints intentionally return their complete bounded collection.

---

## 10. Request validation

Public request models are strict.

Unknown fields are generally rejected instead of silently ignored. This is intentional: a misspelled field should fail visibly rather than appear to work while being discarded.

Expect malformed or semantically invalid requests to produce a `422`-class `INVALID_REQUEST` response.

---

## 11. OpenAPI and Swagger

In development environments (`APP_ENV=dev` or `development`), the application exposes its documentation/OpenAPI surfaces.

In production, those documentation routes are intentionally absent and return `404` rather than being protected behind authentication.

This reduces accidental production disclosure of the interactive API surface.

The repository's versioned documentation remains available regardless of runtime environment:

- [`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- this Wiki
- release notes and product contracts in the repository

---

## 12. Health and readiness

Use:

```text
GET /health/live
```

for process liveness.

Use:

```text
GET /health/ready
```

before sending business traffic.

Readiness covers the operational prerequisites of the running service, including migration/bootstrap state and required runtime dependencies.

A process can be live while deliberately not ready, for example while an automatic migration is still in progress or when the schema is not at the expected revision.

---

## 13. Practical integration sequence

A robust client normally does this:

```text
1. check /health/ready at deployment/connection boundaries
2. authenticate with X-API-Key
3. GET /api/v1/info and negotiate optional capabilities
4. choose the correct domain family
5. use stable idempotency identity for retryable mutations
6. persist returned resource IDs/run IDs
7. branch on stable error codes
8. keep request_id for diagnostics
```

For language-specific examples and an n8n recipe, continue with [Integration Guide](Integration-Guide).

---

## 14. Canonical reference

This page favors discoverability over exhaustive schema detail.

For exact request fields, enum values, status codes, limits and response bodies, the authoritative reference is:

[`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)

When a Wiki explanation and the versioned contract shipped with a release disagree, the versioned contract is authoritative.
