# Troubleshooting

This guide is organized by symptom rather than subsystem.

Start with the public health endpoints, keep the `request_id` from failed API responses, and prefer supported Run/retry/restore workflows over direct database edits.

For full operational procedures, see [Operations & Deployment](Operations-and-Deployment) and the versioned [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

---

## 1. First rule: distinguish liveness from readiness

```text
GET /health/live
GET /health/ready
```

Neither endpoint requires `X-API-Key`.

### `/health/live`

A successful response means the process itself is running:

```json
{"status":"ok"}
```

It does **not** prove PostgreSQL, Neo4j, or the worker is ready.

### `/health/ready`

Readiness checks dependencies and worker state.

When anything required is not operational, it returns HTTP `503` with:

```text
status = not_ready
```

and a per-check map.

Do not bypass readiness just because liveness is green.

---

## 2. `/health/live` fails

If liveness itself is unreachable:

1. confirm the container/process is running;
2. inspect container/service logs;
3. verify port/domain routing;
4. confirm the application process did not exit during configuration validation;
5. inspect deployment environment variables/secrets.

Typical root causes at this level are process startup or routing failures, not a single failed memory operation.

---

## 3. Live works, ready is `503`

Read the individual readiness checks first.

The normal runtime checks include PostgreSQL, Neo4j, and the worker.

A check may safely report details such as:

```text
check failed
check timed out
```

without exposing internal exception data.

Then narrow the problem:

```text
postgres not ready → database/migration/connectivity
neo4j not ready    → graph dependency/connectivity
worker not ready   → worker/bootstrap/operational state
```

Existing reads may still work in some degraded states, but new durable pipeline work can be rejected.

---

## 4. Database schema is not ready

Sofias Memory uses Alembic as the sole schema authority.

Check your migration mode:

```text
DATABASE_MIGRATION_MODE=auto
```

or:

```text
DATABASE_MIGRATION_MODE=verify_only
```

### `auto`

A fresh database or known ancestor should migrate automatically during startup under the serialized migration bootstrap.

### `verify_only`

The application never auto-migrates. Readiness remains `not_ready` until the schema is already at exact head.

For v0.7.0, the expected Alembic head is:

```text
0018
```

Read-only diagnostics:

```bash
docker compose run --rm sofias-memory alembic current
docker compose run --rm sofias-memory alembic heads
```

Do not stamp or manually rewrite Alembic state to silence readiness.

---

## 5. Migration bootstrap refuses the schema

The automatic bootstrap intentionally fails closed for ambiguous states such as:

- non-empty but unversioned schema;
- multiple heads;
- foreign/diverged revision;
- unrecognized migration ancestry.

This is not a condition to “force through”.

Take a backup, identify why the schema is outside the known migration chain, and follow the canonical operations procedure.

---

## 6. API returns authentication errors

All private `/api/v1/**` routes require:

```text
X-API-Key
```

Health routes are the exception.

Stable authentication-related errors include:

```text
MISSING_API_KEY
INVALID_API_KEY
```

Check that:

- the header name is exactly `X-API-Key`;
- your reverse proxy forwards it;
- the configured key matches the running deployment;
- a workflow/credential store did not inject an old value.

Do not place the key in a URL query parameter.

---

## 7. Keep the `request_id`

Success and error envelopes include a request correlation identity.

For a failed request, preserve:

```text
error.request_id
```

Use it to correlate application logs without copying sensitive request contents into tickets or chat messages.

Human-readable error messages are secondary; stable error codes are the programmatic contract.

---

## 8. A knowledge operation was accepted but nothing seems to happen

If the operation created a PipelineRun, inspect the Run rather than resubmitting blindly:

```text
GET /api/v1/runs/{run_id}
```

Check:

```text
status
progress
current_step
error_code
error_message
steps[]
```

See [Runs & Reliability](Runs-and-Reliability).

---

## 9. Run stays `queued`

Check:

1. `/health/ready`;
2. worker readiness/configuration;
3. whether `WORKER_ENABLED` or equivalent deployment configuration disables execution;
4. application logs for worker startup/recovery events;
5. PostgreSQL availability.

A write that requires the worker may return a `WORKER_DISABLED` service-unavailable error when execution cannot be admitted.

Do not change the Run row manually to `running`.

---

## 10. Run failed

Inspect:

```text
GET /api/v1/runs/{run_id}
```

Then look at the failed step’s safe:

```text
error.code
error.message
```

Fix the underlying dependency/configuration issue first.

If the Run is retryable:

```text
POST /api/v1/runs/{run_id}/retry
```

Retry creates a new durable Run and preserves the original attempt history.

---

## 11. Cancellation seems slow

Cancellation is cooperative.

If a Run is currently inside an external call, Sofias Memory does not forcibly kill that call mid-flight.

Expected transitional state:

```text
running → cancelling → cancelled
```

Continue polling the Run.

Do not assume `cancelling` means the worker is stuck.

---

## 12. Dataset is stuck in `deleting`

Administrative Dataset deletion is a durable pipeline.

If the delete Run failed, the Dataset can remain `deleting` intentionally so the partial operation is not mistaken for an active namespace.

Find the associated Run and retry it through:

```text
POST /api/v1/runs/{run_id}/retry
```

A new delete request can return:

```text
DATASET_DELETING
```

when manual Run retry is the correct recovery path.

Do not reset Dataset status directly in PostgreSQL.

---

## 13. Recall returns no or poor results

Check the knowledge state before blaming retrieval:

1. confirm the correct Dataset(s) were queried;
2. inspect Dataset Sources;
3. inspect Dataset statistics;
4. verify Sources are `active`;
5. verify Cognify/processing Runs succeeded;
6. review query mode and request parameters;
7. inspect Query provenance;
8. record Feedback if the result is genuinely low-quality;
9. run/observe Improve when maintenance is appropriate.

Relevant pages:

- [Datasets & Sources](Datasets-and-Sources)
- [Provenance & Feedback](Provenance-and-Feedback)
- [Knowledge Memory](Knowledge-Memory)

---

## 14. Cognitive Recall does not return a MemoryItem

Check the Cognitive Memory semantics deliberately:

- requested `memory_types`;
- requested `scopes`;
- `as_of` timestamp;
- `valid_from` / `valid_until`;
- supersession state;
- `include_superseded`;
- `min_relevance`;
- whether the item was Forgotten.

A Forgotten MemoryItem is intentionally excluded even from historical `as_of` Recall.

See [Cognitive Memory](Cognitive-Memory).

---

## 15. Supersede returns `MEMORY_STATE_CONFLICT`

Supersede is only valid against an `active` target for a new logical operation.

A target already `superseded` or `forgotten` rejects a distinct new supersession.

If this was actually a retry of an earlier Supersede request, reuse the same valid `Idempotency-Key` and identical semantic request rather than inventing a new mutation.

---

## 16. `IDEMPOTENCY_CONFLICT`

This means one `Idempotency-Key` is already bound to a different semantic mutation.

Do not blind-retry the same key with changing payloads or targets.

Generate keys according to logical operation identity:

```text
same operation retry → same key
new operation        → new key
```

The reserved `sys:` key namespace is not available to callers.

---

## 17. Provider dependency unavailable

External embedding/LLM failures can surface as:

```text
DEPENDENCY_UNAVAILABLE
```

Check:

- provider API key;
- provider endpoint/model configuration;
- network/DNS/TLS;
- quota/rate limit;
- embedding dimensions/model compatibility.

For Cognitive Create/Supersede, external embedding is intentionally performed before the short authoritative mutation transaction; provider failure should therefore not leave a partially committed Cognitive mutation.

---

## 18. Neo4j unavailable

Neo4j is required by the current runtime’s knowledge-graph readiness contract, but it is not authoritative.

If Neo4j state is damaged or lost:

- protect PostgreSQL first;
- do not copy Neo4j state back into PostgreSQL as authority;
- use the supported graph rebuild/reconciliation procedures from PostgreSQL.

Native Cognitive Memory is PostgreSQL-only and has no Cognitive Neo4j projection.

See the versioned [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md) for graph rebuild procedures.

---

## 19. Filesystem/S3 source storage problems

First determine the configured backend:

```text
STORAGE_BACKEND=filesystem
```

or:

```text
STORAGE_BACKEND=s3
```

For filesystem mode, verify the persistent source data volume/path exists and is writable by the container.

For S3 mode, verify:

- endpoint;
- bucket;
- region if required;
- access credentials;
- prefix configuration;
- IAM/bucket permissions;
- network reachability.

Do not treat `/data/sources` as disposable in S3 mode; it still participates in durable ingress/in-transit recovery state.

---

## 20. Source shows `storage_available=false`

Source provenance can explicitly report whether original source storage is currently available.

If PostgreSQL metadata exists but `storage_available=false`, investigate the configured source storage backend rather than assuming the Source row is corrupt.

This distinction helps diagnose storage outages independently from knowledge metadata.

---

## 21. Query provenance shows `available=false`

A historical query may reference evidence that was later Forgotten/deactivated.

That is a supported state.

The audit identity remains, but Sofias Memory will not fabricate the removed quote/source as if it were still available.

Use `available` as the truth signal.

---

## 22. Swagger/OpenAPI returns 404

In production, this is expected.

The documentation surface is enabled only for development-style environments (`APP_ENV=dev` or `development`).

In production, `/docs` and `/openapi.json` are intentionally absent rather than merely authentication-protected.

Use the Wiki and versioned API documentation for production operation.

---

## 23. Response says request too large

The API has request-body limits and per-schema limits.

A `REQUEST_TOO_LARGE`/validation failure should be fixed by changing the request shape or using the appropriate file/source ingestion path.

Do not increase limits blindly without understanding memory/storage implications.

---

## 24. Backup/restore problems

Remember the authority model:

**Authoritative / must protect:**

- PostgreSQL;
- persistent source-original state;
- configured S3 namespace when used.

**Reconstructible:**

- Neo4j projection.

Restore PostgreSQL/source state coherently, then rebuild the graph projection when required.

See [Operations & Deployment](Operations-and-Deployment) before attempting repair.

---

## 25. What not to do

Avoid these shortcuts:

- manually mark Runs succeeded;
- manually reset Dataset/Source lifecycle states;
- edit graph_outbox rows to “make Neo4j match”;
- stamp Alembic to head without understanding schema state;
- force-create Cognitive replacements in PostgreSQL;
- restore forgotten content from logs/ledger assumptions;
- make Neo4j authoritative over PostgreSQL;
- retry indefinitely without interpreting stable errors.

They bypass the invariants the public APIs are designed to protect.

---

## 26. Information to collect before escalating

A useful incident report should include non-secret operational evidence:

```text
Sofias Memory version
request_id
endpoint + HTTP method
HTTP status
stable error code
run_id (if applicable)
run status + failing step
/health/ready result
database migration mode
Alembic current/head (when schema-related)
storage backend
relevant sanitized logs
```

Do **not** include:

```text
API_KEY
COGNITIVE_IDEMPOTENCY_HMAC_KEY
DB passwords
provider API keys
full sensitive memory/source contents unless explicitly necessary
```

---

## 27. Related pages

- [Runs & Reliability](Runs-and-Reliability)
- [Datasets & Sources](Datasets-and-Sources)
- [Provenance & Feedback](Provenance-and-Feedback)
- [Operations & Deployment](Operations-and-Deployment)
- [API Guide](API-Guide)
- [Cognitive Memory](Cognitive-Memory)
- [Knowledge Memory](Knowledge-Memory)

For authoritative operational procedures, use the versioned [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).