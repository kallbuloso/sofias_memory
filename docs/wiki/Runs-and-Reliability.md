# Runs & Reliability

Sofias Memory uses durable `PipelineRun`s to represent asynchronous source-backed work such as Remember, Cognify, Improve, Forget, and administrative Dataset deletion.

This page explains how to observe, retry, cancel, and reason about those runs safely.

Cognitive Memory mutations are different: Create, Supersede, and precise Forget under `/memories` are synchronous and do not create PipelineRuns.

---

## 1. Why Runs exist

Knowledge operations can involve multiple durable steps, external providers, storage, PostgreSQL, and graph projection.

A PipelineRun gives that work a stable identity that survives beyond one HTTP request.

This lets clients:

- start work without holding an HTTP connection open;
- poll progress;
- inspect the current step;
- see safe failure information;
- cancel cooperatively;
- retry eligible failed/cancelled work;
- preserve history across attempts.

---

## 2. Pipelines that create Runs

Current pipeline types are:

```text
remember
cognify
improve
forget
dataset_delete
```

These are source-backed/knowledge lifecycle operations.

Native Cognitive Memory operations use a separate transactional model and idempotency ledger.

---

## 3. Run lifecycle

A PipelineRun status is one of:

```text
queued
running
succeeded
failed
cancelling
cancelled
```

Typical flow:

```text
queued → running → succeeded
                 ↘ failed

queued/running → cancelling → cancelled
```

Do not infer success merely because the original HTTP request was accepted.

For asynchronous work, the durable Run is the execution authority.

---

## 4. List Runs

```text
GET /api/v1/runs
```

The endpoint is paginated and supports filters including:

```text
status
type
dataset_id
session_uuid
limit
offset
```

A list item exposes:

```text
run_id
pipeline_type
dataset_id
source_id
session_uuid
status
progress
current_step
attempt
created_at
started_at
finished_at
error_code
error_message
metrics
```

Example:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/runs?status=failed&limit=50"
```

---

## 5. Inspect one Run

```text
GET /api/v1/runs/{run_id}
```

The detailed response includes the persisted step plan.

Each step exposes:

```text
step_id
name
ordinal
status
attempt
metrics
error
started_at
finished_at
```

Public step errors are deliberately sanitized to stable/safe fields:

```text
code
message
```

Internal provider, SQL, or stack-trace details are not part of the public Run contract.

---

## 6. Progress

`progress` is a fraction from:

```text
0.0 → 1.0
```

It reflects completed planned steps.

`current_step` tells you which step is currently executing when one exists.

Use the Run detail rather than inventing client-side progress from timestamps.

---

## 7. `wait=false` and `wait=true`

Write endpoints in the knowledge pipeline can expose synchronous-wait vs immediate-return behavior.

The key rule is:

> Both modes create and observe the same durable PipelineRun.

`wait=false` returns once the Run has been durably queued/accepted.

`wait=true` lets the request wait for the same Run to reach a terminal state, up to the configured timeout.

A timeout does not mean the work vanished. Keep the `run_id` and query the Runs API.

---

## 8. Retry

```text
POST /api/v1/runs/{run_id}/retry
```

Retry does not mutate the original Run into a new attempt.

Instead, Sofias Memory creates a new durable child Run representing another attempt at the same work.

Expected properties:

- new `run_id`;
- incremented `attempt`;
- original Run history preserved;
- original Session association preserved when present;
- only eligible terminal non-succeeded Runs can be retried.

If the retry operation is accepted, poll the returned/new Run normally.

---

## 9. Cancel

```text
POST /api/v1/runs/{run_id}/cancel
```

Cancellation is cooperative.

It does not forcibly terminate an external call already in progress.

A running Run may move to:

```text
running → cancelling → cancelled
```

The worker reaches a safe cancellation point after the current in-flight step/call returns.

This behavior avoids unsafe partial interruption of external operations.

---

## 10. Retry vs starting the operation again

Prefer the Run retry API when recovering a failed durable operation.

Starting a brand-new business request may represent new work and may have different idempotency/lifecycle semantics.

This is especially important for destructive workflows such as Dataset deletion.

If a Dataset remains in `deleting` after a failed delete Run, retry that Run instead of creating manual database repair steps.

---

## 11. Worker disabled

Some write/control operations require the durable worker.

If the worker is disabled or unavailable, the API can return:

```text
WORKER_DISABLED
```

or a service-unavailable response appropriate to the endpoint.

Do not loop aggressively.

Check deployment configuration and `/health/ready` first.

---

## 12. Failure handling pattern

A robust client flow is:

```text
Submit operation
      ↓
Persist run_id
      ↓
Poll GET /runs/{run_id}
      ↓
┌──────────────┬──────────────┬──────────────┐
│ succeeded    │ failed       │ cancelled    │
│ continue     │ inspect      │ decide       │
│              │ error/steps  │ whether retry│
└──────────────┴──────────────┴──────────────┘
                       ↓
               POST /runs/{id}/retry
               when eligible
```

Always use bounded polling/backoff rather than a tight loop.

---

## 13. Runs and Sessions

Runs can be structurally associated with a Session.

The public field is:

```text
session_uuid
```

The Runs list filter also uses `session_uuid`.

A manual retry preserves the original Run’s Session association.

This gives callers an auditable execution history connected to durable contextual sessions without turning Session history into execution state.

---

## 14. Runs and idempotency

PipelineRun durability and request idempotency are related but different concepts.

A Run answers:

> What durable work is/was executed?

Idempotency answers:

> Is this mutation request a replay of the same logical operation?

Native Cognitive Memory intentionally uses its own HMAC-keyed idempotency ledger and does not use PipelineRun.

Do not combine these models in client logic.

---

## 15. Reliability boundaries

Sofias Memory deliberately prefers:

- durable PostgreSQL state;
- explicit Run identities;
- persisted step plans;
- stable public error codes;
- cooperative cancellation;
- explicit retry;
- serialized schema migration;
- reconstructible Neo4j projection.

It does not promise that every external dependency can be interrupted, nor does it hide failed operations behind silent automatic retries forever.

Failures remain observable.

---

## 16. Operational checks

When Runs stop progressing:

1. check `/health/live`;
2. check `/health/ready`;
3. inspect the specific Run;
4. inspect its steps and safe error fields;
5. verify worker/provider/database/storage availability;
6. retry only if the Run contract permits it.

Do not manually edit PipelineRun/PipelineStep rows as an operational shortcut.

See [Troubleshooting](Troubleshooting) for a symptom-driven checklist.

---

## 17. Related pages

- [Datasets & Sources](Datasets-and-Sources)
- [Knowledge Memory](Knowledge-Memory)
- [Sessions](Sessions)
- [Troubleshooting](Troubleshooting)
- [Operations & Deployment](Operations-and-Deployment)
- [API Guide](API-Guide)

For exact Run schemas and endpoint semantics, use the versioned [API semantics](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).