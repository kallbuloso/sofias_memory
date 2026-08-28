# API Guide

This is a human-readable guide to the semantics of the Sofias Memory API — the
things the formal schema alone doesn't explain. For exact request/response
shapes, field constraints, and every field name, use the schema the running
application serves itself:

- `GET /openapi.json` — the raw OpenAPI 3.1 document.
- `GET /docs` — Swagger UI, rendered from that same document.

**Both require `X-API-Key`, like every other route except `/health/*`** — they
are not public. That also means `/docs` is not directly usable by simply
opening it in a browser: a plain browser request has no way to attach a
custom header, so an anonymous visit gets a `401 MISSING_API_KEY` JSON body
instead of the UI, and even a way to force the first request through (a
browser extension, a header-injecting proxy) would still need to satisfy every
follow-up request Swagger UI's own JavaScript makes (starting with fetching
`/openapi.json` itself) the same way. The reliable way to retrieve the schema
is:

```bash
curl -sS -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" "$SOFIAS_MEMORY_URL/openapi.json"
```

This guide does not repeat those schemas; it explains how to use the API
correctly without having read the internal implementation history.

All examples use these placeholders:

```bash
export SOFIAS_MEMORY_URL="http://127.0.0.1:8000"
export SOFIAS_MEMORY_API_KEY="sf-your-real-key"
```

## 1. Authentication

Every route under `/api/v1` requires:

```text
X-API-Key: sf-...
```

compared against the configured key in constant time. `GET /health/live` and
`GET /health/ready` are the only routes that do not require it. A missing key
returns `401 MISSING_API_KEY`; a wrong key returns `401 INVALID_API_KEY`. The key
is never accepted as a query string parameter.

```bash
curl -sS "$SOFIAS_MEMORY_URL/health/live"

curl -sS "$SOFIAS_MEMORY_URL/api/v1/info" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

## 2. Response envelope

Every successful response is wrapped:

```json
{
  "data": { "...": "endpoint-specific" },
  "meta": {
    "request_id": "a1b2c3d4-...",
    "timestamp": "2026-01-01T00:00:00Z"
  }
}
```

Every error response uses a parallel, stable shape:

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "Safe public message.",
    "details": {},
    "request_id": "a1b2c3d4-..."
  }
}
```

`request_id` always matches the `X-Request-Id` response header (and the
inbound one, if you sent it) — use it to correlate a call with server logs.
Error messages are always safe to display: no stack traces, no internal paths,
no secrets, ever appear in `error.message` or `error.details`.

## 3. `ErrorCode` reference

The stable, closed set of error codes you may see:

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | Request failed validation (bad field, unknown field, etc.). |
| `MISSING_API_KEY` | No `X-API-Key` header was sent. |
| `INVALID_API_KEY` | The key sent does not match the configured key. |
| `CONFIGURATION_ERROR` | Server-side configuration problem (not your request). |
| `DEPENDENCY_UNAVAILABLE` | A required dependency (e.g. a URL fetch) was unavailable. |
| `REQUEST_TOO_LARGE` | Request body exceeded the configured size limit. |
| `INTERNAL_ERROR` | Unexpected server error; safe generic message only. |
| `IDEMPOTENCY_CONFLICT` | Same `Idempotency-Key` reused for different work. |
| `RESERVED_IDEMPOTENCY_KEY_NAMESPACE` | Client sent a key starting with the internally reserved `sys:` prefix. |
| `WORKER_DISABLED` | The internal worker isn't operational; a new run can't be created right now. |
| `RUN_NOT_RETRYABLE` | Manual retry attempted on a run that isn't in a retryable terminal state. |
| `MAIN_DATASET_DELETE_FORBIDDEN` | Attempted administrative delete of the `main` dataset. |
| `DATASET_DELETING` | Operation rejected because the target dataset has an in-flight administrative delete. |
| `DATASET_DELETED` | Operation rejected because the target dataset is already a tombstone. |

Example — missing key:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "$SOFIAS_MEMORY_URL/api/v1/info"
# 401, body: {"error":{"code":"MISSING_API_KEY", ...}}
```

Example — validation error (HTTP `422`, using FastAPI's standard
`HTTPValidationError` shape rather than the envelope above — this is the one
place the response shape differs, since it comes from request parsing itself):

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{}'
# 422, body: {"detail":[{"loc":["body","content"],"msg":"Field required", ...}]}
```

## 4. `Idempotency-Key`

Send an `Idempotency-Key` header on `POST /remember`, `/remember/file`,
`/remember/url`, `/cognify`, `/improve`, or `/forget` to make retried requests
safe:

- **Same key + same work** (same endpoint, same effective payload) while the
  original run is `queued`, `running`, or already `succeeded` → you get back
  the **same `run_id`**, never a duplicate run.
- **Same key + different work** → `409 IDEMPOTENCY_CONFLICT`.
- **Key starting with `sys:`** → `400 RESERVED_IDEMPOTENCY_KEY_NAMESPACE` (that
  prefix is reserved for the server's own internal mechanisms, e.g. manual
  retry lineage).
- **No key at all** → each request may create a new run; you are responsible
  for retry safety yourself in that case.

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" \
  -H "Idempotency-Key: my-app-import-42" \
  -H "Content-Type: application/json" \
  -d '{"dataset":"main","mode":"full","wait":false,"content":"Ada Lovelace worked with Charles Babbage."}'
```

## 5. `wait=true` / `wait=false`

Every write endpoint accepts `wait` (default varies by endpoint — see
`/openapi.json` for each one's default). Both values run through the **exact
same durable pipeline**; there is no separate synchronous code path:

- `wait=false` → you get `202 Accepted` as soon as the run is **durably
  queued** — the `PipelineRun` row already exists and is observable via
  `GET /api/v1/runs/{run_id}` before your HTTP response even arrives.
- `wait=true` → the server polls that same run for you and responds once it
  reaches a terminal state (`succeeded`/`failed`), or times out
  (`REQUEST_WAIT_TIMEOUT_SECONDS`) and falls back to returning the current
  state with `202` instead — **a wait timeout never cancels the run**; the
  worker keeps processing it, and you can keep polling
  `GET /api/v1/runs/{run_id}` for the same `run_id`.

## 6. `PipelineRun` lifecycle

```text
queued → running → succeeded
             │           
             ├────────→ failed
             │
             └────────→ cancelling → cancelled
```

`queued → cancelled` is also possible directly (a run cancelled before it was
ever claimed). Every run has: `pipeline_type` (`remember`, `cognify`,
`improve`, `forget`, or `dataset_delete`), `status`, `progress` (0–1),
`current_step`, `attempt`, and timestamps. Its ordered `PipelineStep`s go
through the equivalent per-step lifecycle independently.

## 7. Runs: list, detail, retry, cancel

```bash
# List runs (optionally filter by dataset/status/type — see /openapi.json)
curl -sS "$SOFIAS_MEMORY_URL/api/v1/runs" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Get one run, including its steps
curl -sS "$SOFIAS_MEMORY_URL/api/v1/runs/$RUN_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Cancel (cooperative — an in-flight external call, e.g. an LLM request, is
# never interrupted mid-call; cancellation lands at the next safe point)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/runs/$RUN_ID/cancel" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Manual retry of a FAILED/CANCELLED run — always creates a brand-new run
# (with retry_of_run_id pointing back at the original); the original run's
# own history is never mutated.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/runs/$RUN_ID/retry" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

Both `cancel` and `retry` return `200` if the run they report on is already in
a terminal state (idempotent observation — e.g. retrying an already-retried
run just shows you the existing retry), or `202` if they just triggered a new
state transition you should poll for (`RUNNING → CANCELLING`, or a
newly-created retry run that is itself still non-terminal). Retrying a run
that isn't in a retryable terminal state returns `409 RUN_NOT_RETRYABLE`.

## 8. Remember: text, file, URL

Three endpoints, one underlying pipeline:

```bash
# Text
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "dataset": "main",
    "mode": "full",
    "wait": false,
    "content": "Ada Lovelace was a 19th-century mathematician who worked with Charles Babbage on the Analytical Engine."
  }'

# File (multipart)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember/file" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" \
  -F "file=@notes.pdf" \
  -F "dataset=main" \
  -F "mode=full" \
  -F "wait=false"

# URL (HTTPS only; the fetch itself runs in the worker, guarded against SSRF —
# it never touches loopback, link-local, private, or metadata-endpoint addresses)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember/url" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"dataset":"main","mode":"full","wait":false,"url":"https://example.com/article"}'
```

`dataset` is the dataset **slug** (default `"main"`), not its UUID.

## 9. Remember modes: `ingest` vs `full`

- `mode=ingest` (the field default) — persists the normalized source durably
  for later processing; does not itself chunk/embed/extract. Use this to
  ingest now and `Cognify` later, e.g. in bulk.
- `mode=full` — the complete pipeline: chunk, embed, extract entities/
  relations, summarize, and project to the graph, in one run.

## 10. Recall modes

`POST /api/v1/recall` accepts one `mode` (default `rag`):

| Mode | What it returns |
|---|---|
| `chunks` | Ranked chunk context only, no generation. |
| `summaries` | Ranked summary context only. |
| `rag` | Chunk context plus a generated, grounded answer. |
| `graph` | Graph-seeded traversal context plus a generated answer. |
| `hybrid` | Rank-fused combination of vector and lexical/graph signals. |
| `triplets` | Entity/relation triplets as structured context. |

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/recall" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"datasets":["main"],"query":"Who worked with Charles Babbage?","mode":"graph"}'
```

`datasets` is a list (recall can span more than one dataset slug at once).
Every returned context item and reference carries the `source_id`/
`chunk_id`/`document_id` it came from — that is the provenance chain back to
the original ingested content.

## 11. Cognify

Processes sources that are pending (freshly ingested, not yet processed),
explicitly selected by `source_ids`, or triggers a full dataset **rebuild**
onto a new generation when `rebuild=true`. While a rebuild is in flight, the
previous generation stays authoritative for reads — a partially-rebuilt
generation is never visible, and it only becomes the new authoritative state
once the rebuild run fully succeeds.

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/cognify" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"dataset":"main","wait":false}'
```

## 12. Improve

Background hygiene over already-processed memory: feedback-weighted ranking,
entity deduplication, relation embedding refresh, summary maintenance, and
graph reconciliation (in that fixed order). It never runs implicitly — you
always call it explicitly. You may restrict it to specific `stages`; omitting
`stages` runs all of them.

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/improve" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"dataset":"main","wait":false}'
```

## 13. Forget

One endpoint, three scopes, determined by which fields you send (scope is not
an explicit field):

- **Source** — send `source_id`. Removes that source's memory/content.
- **Dataset** — send neither `source_id` nor `everything=true` (just
  `dataset`, defaulting to `"main"`). Removes a dataset's content; the
  dataset itself stays `ACTIVE` and reusable afterward — this is *not* the
  same thing as deleting the dataset namespace (§14 below).
- **Everything** — send `everything: true` **and** `confirm: "DELETE
  EVERYTHING"` exactly (any other string is rejected with no mutation at
  all). Set `memory_only: true` on any scope to deactivate processed memory
  while keeping the original source/storage intact.

```bash
# Forget one source
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/forget" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"source_id":"'"$SOURCE_ID"'","wait":false}'

# Forget everything (destructive; confirm string is checked exactly)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/forget" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"everything":true,"confirm":"DELETE EVERYTHING","wait":false}'
```

## 14. Administrative Dataset DELETE

`DELETE /api/v1/datasets/{dataset_id}` is a **different operation from Forget
Dataset** — it does not require any confirmation payload (unlike Forget
Everything's exact-string check above), but it permanently retires the
dataset **namespace itself**:

| | Forget (dataset scope) | Administrative Delete |
|---|---|---|
| Removes | dataset's content | the namespace itself |
| Dataset afterward | `ACTIVE` (reusable) | `DELETED` (terminal tombstone) |
| `name`/`slug` | stays usable | reserved forever |

Lifecycle: `ACTIVE → DELETING → DELETED`. It runs as its own durable
`PipelineRun` (`pipeline_type: dataset_delete`), so it is retriable
(`POST /api/v1/runs/{run_id}/retry`) exactly like any other run if it fails
partway through. The `main` dataset can never be deleted this way
(`409 MAIN_DATASET_DELETE_FORBIDDEN`). Once `DELETING`/`DELETED`, further
writes targeting that dataset are rejected (`409 DATASET_DELETING` /
`409 DATASET_DELETED`) rather than silently reactivating it.

```bash
curl -sS -X DELETE "$SOFIAS_MEMORY_URL/api/v1/datasets/$DATASET_ID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

## 15. Dataset management

```bash
# Create
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/datasets" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"my-project"}'

# List / get / sources / stats
curl -sS "$SOFIAS_MEMORY_URL/api/v1/datasets" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/datasets/$DATASET_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/datasets/$DATASET_ID/sources" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/datasets/$DATASET_ID/stats" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Rename (rejected once the dataset is DELETING/DELETED)
curl -sS -X PATCH "$SOFIAS_MEMORY_URL/api/v1/datasets/$DATASET_ID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"renamed-project"}'
```

A `name`/`slug` used by any dataset — active or already a `DELETED`
tombstone — can never be reused by a new dataset; tombstones are kept
permanently for audit, not recycled.

## 16. Response size discipline

The examples above are intentionally small. In real responses from your own
data, never expect (and never post, in your own integration's logs) full
document/chunk text, embedding vectors, prompts sent to the LLM, raw provider
payloads, internal storage paths, or the internal `worker_id` to appear —
none of these are exposed by the public API by design.

## 17. Feedback

Record a thumbs-up/down (or neutral) signal against a previous recall answer
or reference, to be picked up by `Improve`'s feedback-weighting stage:

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/feedback" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"query_id":"'"$QUERY_ID"'","target_type":"answer","score":1}'
```
