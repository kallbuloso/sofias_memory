# API Guide

This is a human-readable guide to the semantics of the Sofias Memory API — the
things the formal schema alone doesn't explain. For exact request/response
shapes, field constraints, and every field name, use the schema the running
application serves itself:

- `GET /openapi.json` — the raw OpenAPI 3.1 document.
- `GET /docs` — Swagger UI, rendered from that same document.

**Both exist only when `APP_ENV=dev` or `APP_ENV=development`** (matched
case-insensitively, surrounding whitespace ignored). In every other
environment — including `production`, the default — neither route is
registered at all: `GET /docs` and `GET /openapi.json` both return a plain
`404`, not an auth error, because the surface simply doesn't exist. `/redoc`
is never registered in any environment (`404` always); Swagger UI is the
product's only interactive documentation surface.

When enabled, `/docs` and `/openapi.json` are public, like `/health/*` — the
only deliberate exceptions to "every route requires `X-API-Key`" — so Swagger
UI is directly usable in a plain browser tab without a header-injecting
workaround. This exposes the API's *shape* (routes, request/response schemas,
field names), never any data — every `/api/v1/**` route underneath still
requires `X-API-Key`. Use Swagger UI's "Authorize" button to enter it once
per page load (it is not persisted across reloads) and then "Try it out"
calls include it automatically. A quick way to fetch just the schema without
a browser, when docs are enabled:

```bash
curl -sS "$SOFIAS_MEMORY_URL/openapi.json"
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
`GET /health/ready` are the only routes that do not require it (`/docs` and
`/openapi.json` are also exempt, but only exist at all when `APP_ENV=dev` or
`development`). A missing key returns `401 MISSING_API_KEY`; a wrong key
returns `403 INVALID_API_KEY`. The key is never accepted as a query string
parameter.

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
| `SESSION_ARCHIVED` | New Session activity (SessionEntry append, Recall, Remember) rejected because the Session is archived. Safe replay, manual retry, reads, restore, and `PATCH` are never blocked by this. |
| `INVALID_SCOPE` | (Episodes, `422`) `scope` is not a canonical `project:<key>` (`global` is rejected for contextual resources). |
| `INVALID_CONTEXT_KEY` | (Episodes, `422`) `context_key` does not match `[a-z0-9][a-z0-9._:-]{0,127}`. |
| `INVALID_EPISODE_KIND` | (Episodes, `422`) `kind` is not one of the nine v1 Episode kinds. |
| `EPISODE_NOT_FOUND` | (Episodes, `404`) The target Episode does not exist. |
| `EPISODE_STATE_CONFLICT` | (Episodes, `409`) The Episode is FORGOTTEN, so it cannot be invalidated. |
| `CHECKPOINT_NOT_FOUND` | (Checkpoints, `404`) The target Context Checkpoint does not exist. |
| `CURRENT_CHECKPOINT_NOT_FOUND` | (Checkpoints, `404`) The Context Stream has no ACTIVE checkpoint. |
| `CONTEXT_STATE_CONFLICT` | (Checkpoints, `409`) Stale writer: the Supersede target is no longer the ACTIVE checkpoint, or Create on a stream that already has one. |
| `CHECKPOINT_STATE_CONFLICT` | (Checkpoints, `409`) Invalidate/Forget is not allowed in the checkpoint's current lifecycle. |
| `EVIDENCE_NOT_FOUND` | (Evidence, `404`) A declared support ref does not exist. |
| `EVIDENCE_UNAVAILABLE` | (Evidence, `409`) A declared support ref is not currently available. |
| `INVALID_EVIDENCE_REFERENCE` | (Evidence, `422`) A support ref has an unknown `source_type` or belongs to another Project Scope. |
| `INVALID_EVIDENCE_CYCLE` | (Evidence, `422`) The declared support would create a dependency cycle. |
| `INVALID_SUPPORT_POLICY` | (Evidence, `422`) Unknown support `mode`, empty `refs`, or more than 50 refs. |

Example — missing key:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "$SOFIAS_MEMORY_URL/api/v1/info"
# 401, body: {"error":{"code":"MISSING_API_KEY", ...}}
```

Example — validation error (HTTP `422`, same `ErrorEnvelope` shape as every
other error, with `error.code=INVALID_REQUEST` and each field-level problem
listed in `error.details.errors`):

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/remember" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{}'
# 422, body: {"error":{"code":"INVALID_REQUEST","message":"Invalid request.",
#   "details":{"errors":[{"loc":["body","content"],"msg":"Field required", ...}]}, ...}}
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
# List runs (optionally filter by dataset/status/type/session_uuid — see /openapi.json)
curl -sS "$SOFIAS_MEMORY_URL/api/v1/runs" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Filter by the Session a run is associated with
curl -sS "$SOFIAS_MEMORY_URL/api/v1/runs?session_uuid=$SESSION_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

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

`RunSummaryResult`/`RunDetailResult.session_uuid` exposes the Session a run
is first-class associated with (`null` if none). This FK is always the
authority: a legacy, pre-v0.3.0 run's `input` may happen to contain a
textual `session_id`, but that is never inferred into `session_uuid` — it
stays `null`. Manual retry preserves the original run's `session_uuid`
verbatim, including when that Session is now archived.

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

All three accept an optional `session_id` (the caller-facing external
Session key). When present, the Session is resolved or lazily created
(rejected with `409 SESSION_ARCHIVED` if it exists and is archived) inside
the same durable submission as the run, and `PipelineRun.session_id` is the
resulting first-class association — surfaced back as
`RememberTextResult.session_uuid` (and, once the run reaches a terminal
state, on `GET /api/v1/runs/{run_id}` too).

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

### Session and Session Context

Recall also accepts an optional `session_id` (the caller-facing external
Session key). When present, the Session is resolved/lazily created (rejected
with `409 SESSION_ARCHIVED` if archived) and the resulting Query is
associated with it — surfaced back as `RecallResult.session_uuid`.

`include_session_context` (default `false`) opts into injecting a bounded,
deterministic window of the Session's most recent SessionEntries into RAG
generation. It is only valid when `session_id` is set, `mode=rag`, and
`only_context=false` — any other combination is `400 INVALID_REQUEST`,
never silently ignored:

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/recall" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "datasets": ["main"],
    "query": "What did we discuss about Ada Lovelace?",
    "mode": "rag",
    "session_id": "conversation:42",
    "include_session_context": true
  }'
```

Session Context affects generation only — it never rewrites the retrieval
query, never performs coreference resolution, and never changes which
chunks/entities/relations are retrieved. Even with zero knowledge hits, a
non-empty selected Session Context is itself valid generation input: the RAG
answer is still generated from that context alone (an empty `context`/
`references` array in the response, but a real answer) — this does not
create new semantic memory; it is generation input only, not a Remember.

## 10a. Provenance

```bash
curl -sS "$SOFIAS_MEMORY_URL/api/v1/provenance/source/$SOURCE_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/provenance/relation/$RELATION_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/provenance/query/$QUERY_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

`GET /api/v1/provenance/query/{query_id}` now also includes `session_uuid`
and `session_context` — the exact, ordered SessionEntries that were used in
that Query's RAG generation (empty when none were), each re-hydrated and
validated against the Query's own Session (an id can never leak content
from a different Session). Knowledge provenance (`references`) and Session
provenance are independent: a knowledge reference can become
`available=false` after Forget/Dataset Delete removes the underlying
content, while `session_uuid`/`session_context` remain valid and unaffected.

## 11a. Sessions

```bash
# Create (session_id omitted -> server generates one)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/sessions" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"session_id": "conversation:42"}'

# List / get
curl -sS "$SOFIAS_MEMORY_URL/api/v1/sessions" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Rename / update metadata
curl -sS -X PATCH "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Support ticket #42"}'

# Archive / restore
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID/archive" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID/restore" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Append a SessionEntry (external_id optional, unique per Session)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID/entries" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"role": "user", "content": "Who worked with Charles Babbage?", "external_id": "turn-1"}'

# List SessionEntries / Queries associated with this Session
curl -sS "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID/entries" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/sessions/$SESSION_UUID/queries" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

`session_id` is the caller-supplied external key (case-sensitive,
immutable, globally unique); `session_uuid` is the internal/public
structural identity everything else (Recall, Remember, Runs, provenance)
associates against.

**Archive is an admission barrier, not a lock on everything.** It blocks
*new* activity — a new SessionEntry append, a new Recall, a new Remember —
with `409 SESSION_ARCHIVED`. It does **not** block: safe SessionEntry
replay (same `external_id` + same payload), Remember's own idempotent
replay of an already-admitted run, manual retry (preserves the original
run's Session even if archived), plain reads, administrative `PATCH`
(rename/metadata), or `restore`.

**SessionEntry `external_id`** (optional, unique per Session): the same
`external_id` with the same semantic payload safely replays — `201`, same
`entry_id`, no duplicate row, observable even after the Session is later
archived. The same `external_id` with a *different* payload is
`409 IDEMPOTENCY_CONFLICT`.

`GET /api/v1/sessions/{session_uuid}/queries` and the Run/Query provenance
endpoints only ever return rows first-class associated by the persisted FK
— never a row that merely happens to contain matching text.

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

## 18. Skills

First-class, durable **procedural memory**: Sofias Memory stores, versions,
and semantically resolves Skills. It never executes one, never selects one
on the caller's behalf, and never authorizes tool use — the caller decides
what to do with a resolved Skill.

```bash
# Create a Skill and its first SkillRevision atomically.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "deploy-app", "description": "Deploys the app to production.",
       "procedure": "1. Run tests.\n2. Build.\n3. Deploy.", "tags": ["deploy"]}'

# List / get / list revisions / get one revision (never leaks `procedure`
# except the single-revision-detail endpoint).
curl -sS "$SOFIAS_MEMORY_URL/api/v1/skills" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/revisions" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/revisions/1" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Create a new revision (201 new content, 200 safe replay of identical content).
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/revisions" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"description": "Deploys the app to production (v2).", "procedure": "1. Run tests.\n2. Build.\n3. Deploy.\n4. Smoke test."}'

# Roll back current_revision (never creates a revision, never re-embeds).
curl -sS -X PATCH "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"current_revision": 1}'

# Archive / restore (discovery filter only, never a write barrier).
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/archive" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/restore" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Standalone SKILL.md import (new Skill) / revision import / export.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/import" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "---\nname: deploy-app-2\ndescription: Deploys the app.\n---\nDo the thing."}'
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/revisions/import" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "---\nname: deploy-app\ndescription: Deploys the app to production.\n---\n1. Run tests.\n2. Build.\n3. Deploy."}'
curl -sS "$SOFIAS_MEMORY_URL/api/v1/skills/$SKILL_UUID/revisions/1/export" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Semantic resolve — discovery/ranking only.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/skills/resolve" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "how do I deploy the app?", "top_k": 5}'
```

`skill_uuid` is the structural identity; `name` (portable, lowercase
`a-z0-9-`, 1..64 chars) is the immutable logical identity a `SKILL.md`
frontmatter round-trips through. A `SkillRevision` is immutable and
addressed by its per-Skill integer `revision`, never by an internal id.

**Identity/idempotency status codes:**

| Route | New/different | Duplicate/identical |
|---|---|---|
| `POST /skills` | `201` | `409 INVALID_REQUEST` (name already exists, any status) |
| `POST /skills/import` | `201` | `409 INVALID_REQUEST` — **always**, even when the content is byte-for-byte identical to an existing Skill; `content_sha256` never decides this route's outcome |
| `POST .../revisions` | `201` | `200` safe replay (same `content_sha256` within this Skill) — `current_revision` unchanged |
| `POST .../revisions/import` | `201` | `200` safe replay, same rule as above; frontmatter `name` must equal the target Skill's `name` exactly, or `422` |

**Archive is a discovery filter, not a write barrier.** An archived Skill
still answers `GET`, creates new revisions, accepts `PATCH current_revision`,
and exports — every one of those returns its ordinary status code. The only
thing archive changes is `POST /skills/resolve`: an archived Skill is never
a candidate, regardless of similarity score. `restore` preserves whatever
`current_revision` the Skill has *at the moment of restore* (e.g. a revision
created while archived) — it never reconstructs the pointer from before the
archive.

**Progressive disclosure.** `GET /skills`, `GET /skills/{skill_uuid}`,
`GET .../revisions` (list), and `POST /skills/resolve` never include
`procedure` — only `skill_uuid`, `name`, `description`, `current_revision`,
`tags`, `declared_tools`, `compatibility`, and (resolve only) `score`. The
full `procedure` is available from exactly one place:
`GET /skills/{skill_uuid}/revisions/{revision}`.

**`declared_tools` / `allowed-tools` is descriptive metadata only** — never
an authorization grant, a permission, a runtime guarantee, or a check that
the named tool is actually installed. The caller alone decides what it is
allowed to execute; declaring a tool name here has no effect on that
decision.

**Semantic resolve.** `POST /skills/resolve` ranks `active`-status Skills'
*current* revision by cosine similarity against `query`, returning up to
`top_k` (default 5, max 20) matches ordered by `score` descending — higher
`score` always means more similar, and there is no hidden similarity
threshold: every ranked candidate up to `top_k` is returned, and
`matches: []` (never `404`) when no active Skill exists. `resolve` never
fetches `procedure`, never creates a SessionEntry/Query/PipelineRun, and
never picks a Skill on the caller's behalf.

**Standalone `SKILL.md` interoperability.** Import/export cover the portable
subset only — frontmatter `name`, `description`, `license`, `compatibility`,
`metadata` (a flat `map<string,string>`), and `allowed-tools` (a
space-separated string), with the Markdown body as `procedure`. Bundled
packages (`scripts/`, `references/`, `assets/`), zip/package import, a
filesystem watcher, and a remote registry are explicitly out of scope.
`tags` is a Sofias Memory extension with no portable frontmatter field of
its own: it round-trips through the reserved transport key
`metadata["sofias-memory.tags"]` (a JSON-array-encoded string) — on import
that key is parsed into `tags`, then removed before the rest of `metadata`
is validated, so it is never persisted as semantic metadata; on export it is
re-synthesized into the document from the persisted `tags`, without ever
touching the persisted `metadata`.

**`content_sha256` is a semantic hash, not a file digest.** It is the SHA-256
of a canonical JSON representation of the revision's eight semantic fields
(`name`, `description`, `procedure`, `license`, `compatibility`, `metadata`,
`tags`, `declared_tools`) — never the SHA-256 of the original or exported
`SKILL.md` bytes. This is exactly why re-importing an export of the same
content (whatever its YAML key order, quoting, or line endings) safe-replays
instead of creating a new revision.

## 19. Agents

First-class, durable **Agent profile management** (ADR-0014): Sofias Memory
stores and manages Agent identity/configuration and two explicit
associations. It never executes an Agent, never selects a provider/model on
its behalf, and never manages a provider session. Exactly **12 operations**
total: 6 Agent management + 3 Agent↔Skill + 3 Agent↔Session — no more, no
runtime-shaped route of any kind.

```bash
# Create an Agent. `name` is portable (lowercase a-z0-9-, 1..64 chars) and
# immutable; already existing (in any status) is a 409, never an upsert.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/agents" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "release-bot", "display_name": "Release Bot",
       "description": "Drives the release checklist.", "instructions": "Follow docs/operations.md."}'

# List (default: active only) / get full detail (includes instructions/metadata).
curl -sS "$SOFIAS_MEMORY_URL/api/v1/agents" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/agents?status=archived" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# PATCH the profile -- wholesale metadata replacement, not a deep merge;
# permitted even when the Agent is archived (archive is a discovery filter,
# never a management barrier).
curl -sS -X PATCH "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"description": "Drives the v0.5.0 release checklist.", "metadata": {"team": "platform"}}'

# Archive / restore -- idempotent, no timestamp churn on replay.
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/archive" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/restore" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Associate a Skill -- omitted/null pinned_revision follows the Skill's
# current revision live; an integer pins that exact revision.
curl -sS -X PUT "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/skills/$SKILL_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" -d '{}'
curl -sS -X PUT "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/skills/$SKILL_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{"pinned_revision": 1}'

# List this Agent's associated Skills.
curl -sS "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/skills" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Associate a Session -- no request body.
curl -sS -X PUT "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/sessions/$SESSION_UUID" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# List this Agent's associated Sessions.
curl -sS "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/sessions" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Remove either association (idempotent -- a non-existent association still
# returns 204; only the Agent itself must exist).
curl -sS -X DELETE "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/skills/$SKILL_UUID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X DELETE "$SOFIAS_MEMORY_URL/api/v1/agents/$AGENT_UUID/sessions/$SESSION_UUID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

### 19.1 Agent management semantics

- `name` is immutable and portable: lowercase `a-z0-9-`, 1..64 chars,
  validated the same way `Skill.name` is. `PATCH` never accepts `name`.
- `GET /agents` with no `status` filter returns **only `active`** Agents —
  deliberately different from Session/Skill listings, which default to
  every status. `?status=archived` (or `?status=active`) is explicit.
- Archived Agents remain fully manageable: `GET` detail, `PATCH`, and both
  association families all keep working — archive is a discovery/
  availability filter over the default list, never an admission barrier
  over management (mirrors Skill archive semantics).
- `PATCH` replaces `display_name`/`description`/`instructions`/`metadata`
  individually per field actually sent; `metadata` is a **wholesale
  replacement**, never a deep merge, and an explicit `metadata: null` is
  rejected (`422`) — omit the field instead to leave it unchanged.
- `archive`/`restore` are idempotent: replaying either against an Agent
  already in that state is a no-op with unchanged `updated_at`/
  `archived_at`.
- **Identity/idempotency status codes:**

| Route | New/different | Duplicate/identical |
|---|---|---|
| `POST /agents` | `201` | `409 INVALID_REQUEST` (name already exists, any status) |
| `GET /agents/{agent_uuid}`, `PATCH`, `.../archive`, `.../restore` on a missing Agent | — | `404 INVALID_REQUEST` |

- **Progressive disclosure.** `GET /agents` (list) never includes
  `instructions` or `metadata` — only `agent_uuid`, `name`, `display_name`,
  `description`, `status`, `created_at`, `updated_at`, `archived_at`.
  `GET /agents/{agent_uuid}` (detail) includes both.

### 19.2 Agent↔Skill semantics

- `pinned_revision` omitted or explicit `null` means "follow `Skill.
  current_revision` live" — the association always reflects whatever the
  Skill's current revision is at read time, including after a later
  rollback. A positive integer pins the association to exactly that
  revision, and a Skill `current_revision` rollback afterward does **not**
  move the pin — `effective_revision` keeps returning the pinned number
  until the association is explicitly re-set.
- `GET` discloses `current_revision` (the Skill's own live pointer),
  `pinned_revision` (this association's pin, or `null`), and
  `effective_revision` (the revision actually in effect: the pin if set,
  otherwise `current_revision`) side by side, plus the *effective*
  revision's own `description`/`tags`/`declared_tools`/`compatibility`.
- Never returned on any of the 3 Agent↔Skill endpoints: `procedure`,
  `resolution_embedding`, `content_sha256`, `metadata`, `license`, the
  internal `pinned_revision_id`, or any `SkillRevision.id` — `pinned_revision`
  is always the same 1-based public revision integer every other Skill
  endpoint uses, never an internal UUID.
- `PUT` is a full idempotent upsert: replaying an identical payload is a
  `200` no-op; a different `pinned_revision` on the same pair re-pins in
  place (same association, same `association_created_at`) rather than
  creating a second row.
- An archived Skill's association is unaffected and remains fully visible/
  manageable: `GET`/`PUT`/`DELETE` all keep working, `effective_revision`
  and `pinned_revision` are unchanged, and `association_created_at` never
  changes on its account.
- `invalid_pinned_revision` (a number with no matching `SkillRevision` for
  that Skill) is `422 INVALID_REQUEST`; a missing Agent or Skill on `PUT`/
  `GET`/`DELETE` is `404 INVALID_REQUEST`.

**`declared_tools` is descriptive metadata only** — exactly the same
non-guarantee as `docs/api.md` §18 already states for Skills directly, and
it does not change when a Skill is associated with an Agent. It is never an
authorization grant, a permission, a runtime guarantee, or a check that the
named tool is actually installed — Sofias Memory does not execute an Agent
or a Skill, so there is no execution path for it to gate in the first
place. The caller alone decides what it is allowed to execute.

### 19.3 Agent↔Session semantics

Agent↔Session is **M:N** and records the **current explicit management
association only** — never ownership in either direction, never historical
participation, and never exact per-operation provenance.

- `PUT` has **no request body**. It is an idempotent ensure-association:
  the first `PUT` creates the row and sets `association_created_at`; a
  replay `PUT` is a `200` no-op that leaves `association_created_at`
  unchanged. `DELETE` removes the row outright — this is not an audit log,
  so there is nothing left to inspect afterward. Re-`PUT`-ing the same pair
  after a `DELETE` creates a **new** row with a **new**
  `association_created_at`, strictly later than the one that was deleted.
- `GET` discloses exactly `session_uuid`, `session_id`, `name`, `status`,
  and `association_created_at` — never `SessionEntry`/`Query`/
  `PipelineRun` content, never a transcript.
- Associating with an archived Session is permitted (`200`) and is
  management metadata only — it never creates a `SessionEntry`/`Query`/
  `PipelineRun`, never unarchives the Session, and never touches
  `Session.updated_at`/`archived_at`.
- Because the association is M:N, a Session with two associated Agents has
  **no per-operation discriminator**: a `Query`/`PipelineRun` linked to
  that Session (via the existing `session_id` provenance, ADR-0012) cannot
  be attributed to one specific Agent from `agent_sessions` alone — neither
  `queries` nor `pipeline_runs` nor `session_entries` carries an `agent_id`
  column, and none ever will as part of this association.
- A missing Agent or Session on `PUT`/`GET`/`DELETE` is `404
  INVALID_REQUEST`; `DELETE` needs only the Agent to exist (removing a
  non-existent association, or one whose `session_uuid` never referenced
  anything at all, is still `204`).

## 20. Cognitive Memory

First-class, durable **Native Cognitive Memory** (ADR-0016): a small,
typed, synchronously-written unit of durable fact/preference (`profile` or
`semantic`), independent of Dataset/Source/Document/Chunk and independent
of Session history. This release implements the complete v0.7 mutable
lifecycle: **Create**, **Get**, **typed Recall**, **Supersede**, and
**Forget**.

### 20.1 Compatibility negotiation

`GET /api/v1/info` gains, purely additively, three fields alongside every
existing one (`name`, `version`, `environment`, `config_fingerprint`,
`llm_model`, `embedding_model`, `embedding_dimensions`, all unchanged):

```json
{
  "api_contract_version": "1",
  "contracts": {"cognitive_memory": "1"},
  "capabilities": [
    "cognitive_memory.write",
    "cognitive_memory.get",
    "cognitive_memory.recall",
    "cognitive_memory.supersede",
    "cognitive_memory.forget"
  ]
}
```

`api_contract_version` is a machine contract identifier, independent of
`version` (the application release SemVer) — never infer Cognitive Memory
support from `version >= 0.7.0` alone. `capabilities` lists only operations
genuinely implemented by the running HEAD: the five Cognitive Memory
capabilities of its Feature Contract, plus the Contextual Project Memory
capabilities described in §21.1.

```bash
curl -sS "$SOFIAS_MEMORY_URL/api/v1/info" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

### 20.2 Create

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/memories" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "memory_type": "profile",
        "scope": "global",
        "content": "Prefers teal interfaces.",
        "provenance": {
          "origin_kind": "user_asserted",
          "source_system": "sofias-assistant",
          "turn_uuid": "b3b6b5a0-...-...-...-..."
        }
      }'
```

Create is **synchronous** — `201` returns the finished `MemoryItem`
directly, never a `PipelineRun`/`202`. The external embedding call always
happens before the short authoritative PostgreSQL transaction; no
transaction is ever held open across it.

- `memory_type`: `profile` or `semantic` only — anything else is `422
  INVALID_REQUEST`.
- `scope`: exactly `global` or `project:<key>` (lowercase `a-z0-9._-`,
  1..128 chars) — exact-match only, no hierarchy/wildcard.
- `content`: 1..16384 Unicode characters after CRLF/CR→LF normalization and
  edge trim; blank-after-trim is rejected.
- `confidence`: nullable `[0,1]`; **required** when `provenance.origin_kind`
  is `inferred`; never synthesized for any other origin.
- `valid_from`/`valid_until`: optional, timezone-aware timestamps
  (normalized to UTC); when both are present, `valid_until` must be
  strictly after `valid_from`.
- `provenance.origin_kind`: `user_asserted`, `tool_observed`, `imported`,
  `inferred`, or `assistant_generated` — each has its own minimum required
  fields beyond `source_system` (see `docs/adr/0016-native-cognitive-memory-model-and-lifecycle.md`
  §7.3).
- Embedding is **never** returned by any Cognitive Memory response.

**`Idempotency-Key` is optional.** Without it, every valid request is an
independent Create — identical `content`/`scope`/`provenance` submitted
twice (no key) produces **two distinct** `memory_id` values; there is no
content-hash dedupe. With it:

| Same key | Semantic request | Result |
|---|---|---|
| yes | same (all semantic fields match) | replays the original `201` outcome, same `memory_id`, no second embedding call |
| yes | different | `409 IDEMPOTENCY_CONFLICT` |
| — | key in the reserved `sys:` namespace | `400 RESERVED_IDEMPOTENCY_KEY_NAMESPACE` |

The race between two concurrent Create calls sharing one key is resolved by
a PostgreSQL `UNIQUE` claim on the key, never by a pre-check alone: exactly
one caller creates the `MemoryItem`+provenance+ledger row; every other
concurrent caller either replays that same winner (same request) or gets
`409` (different request) — never a duplicate, never a `500`.

### 20.3 Get

```bash
curl -sS "$SOFIAS_MEMORY_URL/api/v1/memories/$MEMORY_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
```

`200` with the same `MemoryItem` shape Create returns. A syntactically
valid but non-existent UUID is `404 MEMORY_NOT_FOUND`; a malformed path
segment is `422 INVALID_REQUEST` (the project's ordinary path-parameter
validation contract). Get never calls the embedding provider and never
reads Neo4j.

The response shape is deliberately lifecycle-safe: `scope`/`content`/
`confidence`/`valid_from`/`valid_until` and every `provenance` field beyond
`origin_kind`/`source_system` are nullable so a later SUPERSEDED/FORGOTTEN
tombstone (once those transitions exist) fits the exact same schema —
this release only ever produces `lifecycle: "active"`.

### 20.4 Typed Recall

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/memories/recall" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "query": "What interface color does the user prefer?",
        "scopes": ["global"],
        "top_k": 10
      }'
```

`POST /api/v1/memories/recall` is a **separate, read-only** operation from
the legacy knowledge `POST /api/v1/recall` — the two never share a request/
response shape, and neither one's semantics changes because of the other.
Cognitive Recall never touches Neo4j, never uses an ANN/HNSW index (exact
cosine similarity only), never falls back to lexical search, and never
creates a `PipelineRun`.

Request fields:

- `query` (required): 1..8192 Unicode characters after normalization.
- `memory_types` (optional, default `["profile", "semantic"]`): restrict to
  one or both types — `episodic`/`procedural`/anything else is `422`.
- `scopes` (required, 1..16): canonical scopes, exact-match only — the same
  `global`/`project:<key>` grammar as Create. Requesting `["project:alpha"]`
  never implicitly includes `global`; list both explicitly if you want both.
- `top_k` (optional, default `10`, range `1..50`): applied **after**
  structural/temporal filtering and `min_relevance`, never before.
- `as_of` (optional, default: server now UTC): timezone-aware, normalized
  to UTC. A future value is `422 INVALID_REQUEST`.
- `include_superseded` (optional, default `false`): see current-truth
  semantics below.
- `min_relevance` (optional, `[-1, 1]`): inclusive threshold on the same
  cosine `relevance` value the response returns — never on a raw/inverted
  distance.

Response: `200`, `SuccessEnvelope[MemoryRecallResult]`, `{"items": [...]}`
where each item is `{"memory": <MemoryItem>, "relevance": <float>,
"is_current_truth": <bool>}`. Embedding is never returned. `relevance` is
cosine similarity in `[-1, 1]`, computed by PostgreSQL/pgvector over the
authoritative full-precision `VECTOR(3072)` — never a raw distance value.

**Current-truth semantics (the central invariant).** Eligibility is never
decided by an item's *present* `lifecycle` label alone — a `MemoryItem` now
`superseded` is still returned, with `is_current_truth: true`, when it
*was* current truth at the effective `as_of`:

- `created_at <= as_of` — an item created after `as_of` did not exist yet
  and is always excluded, regardless of every other field.
- The semantic validity window applies to **every** returned item,
  regardless of `include_superseded`: `valid_from` is **inclusive** (at
  `as_of == valid_from`, eligible) and `valid_until` is **exclusive** (at
  `as_of == valid_until`, **not** eligible). A historical superseded item
  outside its own `valid_from`/`valid_until` window at `as_of` is excluded
  even with `include_superseded=true` — having existed and not yet been
  forgotten is never sufficient on its own.
- With `include_superseded=false` (default): only items that were current
  truth *at `as_of`* are returned — additionally `superseded_at IS NULL OR
  as_of < superseded_at`. A presently-`superseded` item queried before its
  own `superseded_at` (and inside its validity window) is included; queried
  at or after it, it is excluded.
- With `include_superseded=true`: historical superseded items are also
  returned, still subject to `created_at <= as_of`, non-`forgotten`, *and*
  the validity window above — never merely because they existed and were
  not yet superseded. Each result carries its own correct
  `is_current_truth` — `false` once `as_of >= superseded_at`, even when the
  item is otherwise still inside its validity window.
- `forgotten` items are **never** returned, under any `include_superseded`
  or `as_of` value — Forget destroys cognitive content, and history cannot
  reconstruct what no longer exists.

Deterministic total order: `relevance` descending, then `created_at`
descending, then `memory_id` ascending. `confidence` never affects ranking.

### 20.5 Supersede

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/memories/$MEMORY_ID/supersede" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "content": "The user now prefers dark mode.",
        "provenance": {
          "origin_kind": "user_asserted",
          "source_system": "sofias-assistant",
          "turn_uuid": "b3b6b5a0-...-...-...-..."
        }
      }'
```

Atomically replaces an `ACTIVE` MemoryItem with exactly one new `ACTIVE`
replacement. Success is `200` with `{"old": <MemoryItem>, "replacement":
<MemoryItem>}` — `old.lifecycle` is `superseded`, `replacement.lifecycle`
is `active`. There is no other Supersede response shape.

- The replacement **always inherits** `memory_type` and `scope` from the
  target — the request body never accepts either field; sending them is
  `422 INVALID_REQUEST`. Changing type/scope is conceptually a new memory,
  never a supersession of this one.
- The request body is otherwise the same shape as Create minus
  `memory_type`/`scope`: `content` (required), `confidence`, `valid_from`/
  `valid_until`, and a fresh `provenance` for the replacement — the same
  normalization/validation rules apply (`inferred` still requires
  `confidence`, etc.).
- The external embedding call for the replacement's content always happens
  before the short authoritative PostgreSQL transaction; no transaction is
  ever held open across it. Embedding failure leaves the old item
  untouched (`ACTIVE`), creates no replacement, and returns `503
  DEPENDENCY_UNAVAILABLE`.
- Only an `ACTIVE` target can be superseded. A target that is already
  `superseded` or `forgotten` (and the request is a genuinely different
  operation, not a replay — see below) is `409 MEMORY_STATE_CONFLICT`. A
  syntactically valid but non-existent `memory_id` is `404
  MEMORY_NOT_FOUND`.
- A replacement can later be superseded again, forming a strictly linear
  chain — an old item has at most one direct replacement.

**Idempotency and the same-operation replay precedence invariant.**
`Idempotency-Key` works exactly as it does for Create, with one critical
addition the Feature Contract requires: the authoritative key claim is
resolved **before** the target's lifecycle is ever evaluated. Concretely,
retrying the exact same key + exact same replacement request after that
very retry's own earlier attempt already moved `old` to `superseded`
replays the original `200` outcome (same `old`/`replacement` pair) — it
never becomes a `409 MEMORY_STATE_CONFLICT` just because the target is no
longer `ACTIVE`. Only a genuinely different operation (a different
key/request against a target that is not `ACTIVE`) gets the conflict.
Reusing a key already bound to a different target, a different payload, or
even a prior `Create` call is `409 IDEMPOTENCY_CONFLICT`. The race between
two concurrent Supersede calls sharing one key converges on exactly one
replacement via the same PostgreSQL `UNIQUE` claim Create uses; two
concurrent calls with *different* keys against the same target converge to
exactly one `200` winner and one `409 MEMORY_STATE_CONFLICT` loser, via
ordinary PostgreSQL row-level locking — no advisory locks, no `sleep`.

### 20.6 Forget

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/memories/$MEMORY_ID/forget" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" \
  -H "Idempotency-Key: $(uuidgen)"
```

Precisely and destructively forgets the exact `memory_id` — no request
body exists or is accepted. Success is `200` with the same `MemoryItem`
shape as Create/Get/Supersede, now a tombstone. A non-existent `memory_id`
is `404 MEMORY_NOT_FOUND`. Forget never touches `POST /api/v1/forget`
(the legacy Source/Dataset/Everything pipeline), never cascades to a
superseding replacement or a superseded predecessor, and never creates a
`PipelineRun`/Neo4j/`graph_outbox` side effect.

**Destructive scrub, atomically.** Both `ACTIVE` and `SUPERSEDED` targets
transition to `forgotten` in one transaction that also destroys:

- `content`, the embedding, `scope`, `confidence`, `valid_from`,
  `valid_until` on the `MemoryItem` itself;
- every external provenance reference on the (preserved, still 1:1)
  `memory_provenance` row: `conversation_uuid`, `turn_uuid`, `task_uuid`,
  `confirmation_ref`, `source_ref`, `observed_at`.

Only `memory_id`, `memory_type`, `lifecycle`, `created_at`, `forgotten_at`,
prior `superseded_at`/`superseded_by` (if the item had already been
superseded before this Forget), and `provenance.origin_kind`/
`provenance.source_system` survive — the tombstone shape Get already
documents. Forgetting a `SUPERSEDED` item preserves its lineage identity
(`superseded_at`/`superseded_by` untouched) without affecting its
replacement in any way; forgetting an `ACTIVE` item creates no replacement
and leaves `superseded_at`/`superseded_by` `null`. After commit, the
content is not retrievable through `GET`, typed Recall (current or
historical `as_of`, with or without `include_superseded`), provenance, or
the idempotency ledger — the ledger keeps only a keyed HMAC digest, never
a copy of the destroyed content.

**`FORGOTTEN -> FORGOTTEN` is a resource-state idempotent no-op** — this
holds even with a **brand-new** `Idempotency-Key` the target has never
seen before: the response is still `200` with the identical tombstone
(`forgotten_at` unchanged), and no second destructive mutation occurs.
Normal same-key semantics still take precedence over that no-op, though:
if the key was already bound to a *different* request (a different target,
or reused from Create/Supersede), it is `409 IDEMPOTENCY_CONFLICT` even
when the target you are now naming happens to already be `forgotten` — the
no-op never silently absorbs a caller's key-reuse mistake.

### 20.7 Explicitly not yet implemented

`PATCH /api/v1/memories/{id}` does not exist and never will — correction
happens only through Supersede (preserving history) or a new Create.
Cognitive Memory never creates a `PipelineRun`, never projects to Neo4j,
and never emits a `graph_outbox` event, for any of Create/Get/Recall/
Supersede/Forget.

## 21. Contextual Project Memory

Persistent, explicit, project-scoped **context** for applications and agents:
what happened (**Episodes**), what is current (**Context Checkpoints**), why it
is believed (explicit **Evidence**), and a structured, read-only **Contextual
Recall** over all of it. PostgreSQL is authoritative for every contextual
resource; nothing is projected to Neo4j, no `graph_outbox` event and no
`PipelineRun` is created, and no LLM is ever called. Nothing contextual is
created automatically: every Episode, Checkpoint and Evidence link is written
explicitly by the caller.

Returned content is *context*, never authority: it is untrusted data, never an
instruction, permission or plan. Authorization, planning, execution and tool
permissions stay the responsibility of the Consumer.

### 21.1 Compatibility negotiation

`GET /api/v1/info` advertises the feature contract and exactly five
capabilities, additively next to Cognitive Memory (`api_contract_version`
stays `"1"`):

```json
{
  "api_contract_version": "1",
  "contracts": {"cognitive_memory": "1", "contextual_project_memory": "1"},
  "capabilities": [
    "cognitive_memory.write",
    "cognitive_memory.get",
    "cognitive_memory.recall",
    "cognitive_memory.supersede",
    "cognitive_memory.forget",
    "contextual_project_memory.episodes",
    "contextual_project_memory.checkpoints",
    "contextual_project_memory.recall",
    "contextual_project_memory.explain",
    "contextual_project_memory.history"
  ]
}
```

Check `contracts.contextual_project_memory == "1"` and the capabilities you
need before calling these routes; never infer support from `version`.
Evidence and Forget propagation are integral guarantees of the contract, not
separate capabilities.

### 21.2 Scope and Context Streams

- `scope`: `project:<key>` only (Cognitive Memory grammar; `global` →
  `422 INVALID_SCOPE`). A Project Scope is a contextual namespace, never an
  ACL/tenant boundary, and never a Knowledge dataset.
- `context_key`: a Context Stream inside the project,
  `[a-z0-9][a-z0-9._:-]{0,127}` (otherwise `422 INVALID_CONTEXT_KEY`).
  Checkpoints and Contextual Recall default it to `main`; an Episode without
  `context_key` is **project-wide** and stays `null`.

### 21.3 Episodes

First-class historical experience (*what happened*). An Episode is never
current truth: it never creates or supersedes a `MemoryItem`, never becomes a
`SessionEntry`, and is never extracted automatically.

```bash
# Create (Idempotency-Key is REQUIRED on every contextual mutation)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/episodes" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "scope": "project:sofias-memory",
        "context_key": "feature:contextual-memory",
        "kind": "decision",
        "summary": "Episode was chosen as a first-class aggregate.",
        "occurred_at": "2026-11-17T21:00:00Z",
        "provenance": {"origin_kind": "user_asserted", "source_system": "sofias-assistant"},
        "support": {
          "mode": "all_required",
          "refs": [{"source_type": "memory_item", "source_id": "'"$MEMORY_ID"'"}]
        }
      }'

# List (scope required; default lifecycle=active; keyset pagination)
curl -sS "$SOFIAS_MEMORY_URL/api/v1/episodes?scope=project:sofias-memory&limit=20" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Get / invalidate / forget
curl -sS "$SOFIAS_MEMORY_URL/api/v1/episodes/$EPISODE_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/episodes/$EPISODE_ID/invalidate" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" -d '{"reason": "Found to be inaccurate."}'
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/episodes/$EPISODE_ID/forget" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Idempotency-Key: $(uuidgen)"
```

- `kind`: `decision`, `investigation`, `experiment`, `incident`, `discovery`,
  `change`, `outcome`, `learning`, `milestone`.
- `summary` required; `situation`/`reason`/`outcome` optional; text fields must
  be non-blank (CRLF/CR→LF, edges trimmed).
- `occurred_at`: caller-supplied, timezone-aware (normalized to UTC), may be in
  the past, rejected beyond a 5-minute clock-skew tolerance in the future;
  independent of server-generated `created_at`.
- `confidence`: nullable `[0,1]`, required when `origin_kind` is `inferred`.
- `provenance`: `origin_kind` (same five values as Cognitive Memory),
  `source_system` (required), optional opaque `conversation_ref`/`turn_ref`.
  **Provenance is not Evidence.**
- `support`: optional explicit Evidence (§21.5). Omitted means an independent
  assertion with no declared dependency.
- The embedding is computed before the short PostgreSQL transaction; on
  provider failure → `503 DEPENDENCY_UNAVAILABLE` and nothing is persisted. It
  is never returned.

Lifecycle: `active → invalidated`, `active|invalidated → forgotten`;
`forgotten` is terminal. Invalidate on an invalidated Episode and Forget on a
forgotten Episode are `200` state no-ops; Invalidate on a forgotten Episode is
`409 EPISODE_STATE_CONFLICT`. Forget is destructive: every semantic field, the
embedding and the external provenance refs are scrubbed, leaving only
`episode_id`, `lifecycle`, timestamps and `origin_kind`/`source_system`. A
forgotten Episode never appears in listings; read it by id. There is no
`PATCH`/`PUT`/`DELETE`, restore or unforget.

### 21.4 Context Checkpoints

A Context Checkpoint is the explicit, structured snapshot of *where the work is
now* for one Context Stream `(scope, context_key)`: `objective`,
`current_state` (required), `approved_decisions`, `pending_work`,
`open_items`, `blockers`, `next_action` — each statement optionally with its
own Evidence `support`. Exactly one checkpoint per stream is `active`
(current); revisions increase monotonically and are never reused.

```bash
# Create the current checkpoint of a stream (only when the stream has no ACTIVE one)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "scope": "project:sofias-memory",
        "context_key": "main",
        "objective": {"text": "Deliver contextual project memory."},
        "current_state": {"text": "Contract approved; implementation in progress."},
        "pending_work": [{"text": "Run the longitudinal validation."}],
        "next_action": {"text": "Write the migration tests."},
        "provenance": {"origin_kind": "assistant_generated", "source_system": "sofias-assistant"}
      }'

# Read the current checkpoint of a stream (404 CURRENT_CHECKPOINT_NOT_FOUND if none)
curl -sS "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints/current?scope=project:sofias-memory&context_key=main" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"

# Replace it with a complete new snapshot (the read checkpoint is the expected base)
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints/$CHECKPOINT_ID/supersede" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" -d @next-snapshot.json

# Get by id / list / invalidate / forget
curl -sS "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints/$CHECKPOINT_ID" -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints?scope=project:sofias-memory&context_key=main&lifecycle=superseded" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY"
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints/$CHECKPOINT_ID/invalidate" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" -d '{"reason": "No longer reflects the project."}'
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/context/checkpoints/$CHECKPOINT_ID/forget" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Idempotency-Key: $(uuidgen)"
```

- Lifecycle: `active → superseded` (only through Supersede), `active →
  invalidated`, any non-forgotten `→ forgotten` (terminal).
- **Stale writers:** Supersede succeeds only while its target is still the
  ACTIVE checkpoint. If another writer already superseded it, the call fails
  with `409 CONTEXT_STATE_CONFLICT` and nothing is created: re-read
  `/current`, merge, and supersede the new current checkpoint. Never retry
  blindly with a new key.
- Create on a stream that already has an ACTIVE checkpoint →
  `409 CONTEXT_STATE_CONFLICT`. After the current one is invalidated (or lost
  its support, §21.6), a new explicit Create starts the next revision; it is
  never recorded as a Supersede of the invalidated revision.
- Listing defaults to ACTIVE checkpoints; history requires an explicit
  `lifecycle` filter. A forgotten checkpoint keeps only its identity, stream
  and revision.
- Idempotency (all contextual mutations): same key + same request replays the
  original result (resolved before any state rejection); same key + different
  request → `409 IDEMPOTENCY_CONFLICT`; `sys:`-prefixed keys → `400`; a missing
  header → `422 INVALID_REQUEST`. Keys and requests are stored only as keyed
  HMAC identities, never in plain text.

### 21.5 Evidence and support

`support = {"mode": "all_required" | "any_sufficient", "refs": [...]}` declares
which resources an Episode or a checkpoint statement depends on. A ref is
`{"source_type", "source_id"}` with `source_type` one of `session_entry`,
`memory_item`, `episode`, `source`, `document`, `chunk` (1–50 refs, an
unordered set). Every ref must exist (`404 EVIDENCE_NOT_FOUND`), be currently
available (`409 EVIDENCE_UNAVAILABLE`), belong to the same project when it is
an Episode or a project MemoryItem (`422 INVALID_EVIDENCE_REFERENCE`), and not
form a dependency cycle (`422 INVALID_EVIDENCE_CYCLE`); an invalid policy is
`422 INVALID_SUPPORT_POLICY`.

Responses return each declared ref with its *current* `availability`
(`available`, `unavailable`, `forgotten`) — identities only, never the source
content. Evidence is only what the caller declared: it is never inferred from
provenance, timestamps, checkpoint lineage or semantic similarity.

### 21.6 Forget propagation

A successful Forget may propagate through **explicit Evidence dependencies**
— never a blind cascade:

- an Episode whose `all_required` support loses a forgotten ref, or whose
  `any_sufficient` refs are all forgotten, is forgotten (scrubbed) as well, and
  so are its own dependants, transitively;
- an `any_sufficient` Episode that still has an available ref stays active;
- a checkpoint statement that loses its required support is removed; if the
  `current_state` of the ACTIVE checkpoint is removed, the checkpoint becomes
  `invalidated` — with **no automatic fallback** to an older revision.

This applies to Cognitive Forget, Episode Forget and Knowledge Forget
(the resources that can be Evidence). With Knowledge Forget `memory_only: true` the original Source may be
preserved for reprocessing, but contextual content that lost its required
support is still scrubbed, and re-cognify never resurrects it. Forgotten
content never comes back after a restart; there is no restore.

### 21.7 Contextual Recall

`POST /api/v1/context/recall` is synchronous and **read-only** (no Query,
Session entry, PipelineRun or Evidence is created) and returns a structured
`ProjectContextBundle` — never a generated answer.

```bash
curl -sS -X POST "$SOFIAS_MEMORY_URL/api/v1/context/recall" \
  -H "X-API-Key: $SOFIAS_MEMORY_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "scope": "project:sofias-memory",
        "context_key": "main",
        "intent": "explain",
        "query": "Why is PostgreSQL authoritative?",
        "datasets": ["docs"],
        "include_evidence": true
      }'
```

| `intent` | Returns |
|---|---|
| `resume` | the current checkpoint, current project Cognitive Memory and relevant ACTIVE Episodes |
| `context` | the same, focused on the query |
| `explain` | the same, plus the bounded causal trace of the explicit Evidence of the returned resources |
| `history` | the current checkpoint as orientation, SUPERSEDED/INVALIDATED revisions (revision DESC) and ACTIVE/INVALIDATED Episodes (`occurred_at` DESC); no current memories |

- `query` is required (1–8192 characters). At most **one** embedding call is
  made per request, shared by Cognitive Memory, Episodes and Knowledge. If the
  provider fails, the bundle degrades deterministically (lexical Knowledge,
  recency for Episodes/memories) with `warnings=["PARTIAL_CONTEXT"]` and the
  affected `degraded_sources`.
- Knowledge is used **only** for datasets listed explicitly in `datasets`
  (at most 16; a dataset that does not exist or is not ACTIVE →
  `422 INVALID_REQUEST`). Omitting `datasets` means no Knowledge at all —
  never an implicit `main`. `knowledge_references` carry identities and a
  local rank, never chunk text; a retrieval hit is never Evidence.
- Cognitive Memory is limited to the requested project scope: `global` memory
  is never included automatically.
- `include_evidence=true` returns `evidence[]`: each identity once, with its
  `availability` and its `owners` (`owner_type` `context_statement`/`episode`,
  `owner_id`, `support_mode`), so a chain such as statement → Episode B →
  Episode A → Chunk is reconstructable. Intermediate Episodes appear as
  identities only. A trace cut by a limit is never presented as complete.
- `limits`: `memories` (default 8, max 50), `episodes` (8, 50),
  `historical_checkpoints` (4, 20), `knowledge` (8, 50), `evidence` (12, 100).
  The response is bounded; anything omitted is signalled with
  `PARTIAL_CONTEXT`.
- Warnings: `NO_CURRENT_CHECKPOINT`, `PARTIAL_CONTEXT`,
  `EVIDENCE_UNAVAILABLE`, `AMBIGUOUS_CURRENT_TRUTH`. Forgotten content is
  never returned by any intent; content whose support is currently
  insufficient is excluded from current context.
- PostgreSQL unavailable → `503 DEPENDENCY_UNAVAILABLE` (never an empty
  bundle pretending nothing exists). Contextual Recall reads only PostgreSQL;
  it never queries Neo4j (see §21.9 for the service-level startup dependency).

### 21.8 What Contextual Project Memory does not do

No automatic checkpoint creation, Episode extraction, Evidence inference,
summarization, contradiction resolution or consolidation; no Neo4j projection;
no cross-project recall; no LLM explanation. All of these remain out of scope.

### 21.9 Operational note: Neo4j and service startup

Every contextual operation reads and writes only PostgreSQL and never uses
Neo4j. This does **not** make the service start without Neo4j: when Neo4j is
configured, the service's standard startup bootstrap (shared by every feature)
includes it, and while that bootstrap has not succeeded the service is not
operational — every business route, contextual routes included, answers
`503 DEPENDENCY_UNAVAILABLE`. If Neo4j becomes unavailable *after* the service
is operational, Episodes, Context Checkpoints, Evidence, Contextual Recall and
Forget propagation keep working, and `GET /health/ready` reports the outage.
