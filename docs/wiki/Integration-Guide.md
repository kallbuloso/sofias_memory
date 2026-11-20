# Integration Guide

Sofias Memory is designed to be consumed through a stable HTTP API.

You do not need a language-specific SDK to integrate it. Any application, automation platform, backend, script, or agent runtime that can make HTTP requests can use the same contract.

This guide covers the common integration patterns and includes a practical n8n recipe using the built-in HTTP Request node.

---

## 1. Base URL and authentication

A typical self-hosted instance might be available at:

```text
https://memory.example.com
```

All private routes under:

```text
/api/v1/**
```

require:

```text
X-API-Key: <your-api-key>
```

Health endpoints are public:

```text
GET /health/live
GET /health/ready
```

Do not put the API key in source code or workflow text when your platform provides a credential/secret store.

---

## 2. Negotiate capabilities first

Before depending on an optional capability, call:

```text
GET /api/v1/info
```

Example:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/info"
```

For Cognitive Memory contract `1`, the service advertises:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

Integrations should check the machine-readable contract/capabilities instead of inferring feature support from application SemVer.

---

## 3. Choose the correct memory plane

Before choosing an endpoint, decide what kind of memory you are storing.

Use [Knowledge Memory](Knowledge-Memory) when the information comes from source material such as documents, files, URLs, manuals, or reference corpora.

Use [Cognitive Memory](Cognitive-Memory) when the information is a durable fact, preference, decision, characteristic, or persistent semantic context that should exist directly as memory.

This decision matters because the lifecycle, retrieval, provenance, and Forget semantics are intentionally different.

---

## 4. Cognitive Memory integration pattern

A typical Cognitive Memory flow is:

```text
1. GET /api/v1/info
2. POST /api/v1/memories
3. POST /api/v1/memories/recall
4. GET /api/v1/memories/{memory_id} when identity lookup is needed
5. POST /api/v1/memories/{memory_id}/supersede when truth changes
6. POST /api/v1/memories/{memory_id}/forget when exact memory must be erased
```

Do not use Supersede as Delete and do not use Forget as Update.

---

## 5. Create Cognitive Memory

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Idempotency-Key: customer-42-preference-001" \
  -H "Content-Type: application/json" \
  -d '{
    "memory_type": "profile",
    "scope": "global",
    "content": "Prefers communication by email.",
    "provenance": {
      "origin_kind": "user_asserted",
      "source_system": "crm",
      "source_ref": "customer:42"
    }
  }'
```

If the caller retries the same semantic mutation with the same `Idempotency-Key`, Sofias Memory can safely replay the original logical outcome.

Idempotency is not semantic deduplication. A different key or no key can represent a distinct Create operation.

---

## 6. Recall Cognitive Memory

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories/recall" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How does this customer prefer to be contacted?",
    "scopes": ["global"],
    "top_k": 5
  }'
```

Use the returned:

```text
relevance
is_current_truth
```

as explicit retrieval evidence.

Do not interpret a returned memory as an authorization rule, system instruction, or policy grant merely because it is relevant.

---

## 7. Knowledge Memory integration pattern

A common source-backed flow is:

```text
1. create/select Dataset
2. Remember source material
3. observe the durable PipelineRun
4. Cognify/process when required
5. POST /api/v1/recall
6. consume context + provenance
7. explicitly Improve/reconcile when appropriate
8. use the correct Forget or administrative lifecycle operation when needed
```

Knowledge writes may be asynchronous and represented by durable Runs.

For exact request schemas and `wait=true/false` semantics, use the versioned [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 8. Error handling

Sofias Memory uses a stable error envelope and request correlation metadata.

Integrations should branch on stable error codes/status classes rather than parsing human-readable messages.

Common classes include:

```text
401/403-style authentication/configuration failures
404 missing resource
409 idempotency or lifecycle conflict
422 invalid request
503 dependency/unavailable state
```

For Cognitive Memory, important stable errors include concepts such as:

```text
MEMORY_NOT_FOUND
MEMORY_STATE_CONFLICT
IDEMPOTENCY_CONFLICT
INVALID_REQUEST
DEPENDENCY_UNAVAILABLE
```

Use the exact contract from the versioned [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 9. Retry policy

Only retry operations when the failure class and operation semantics make retry safe.

For Cognitive write mutations, provide a stable `Idempotency-Key` when your caller may retry after timeout or transport failure.

A practical policy is:

```text
network timeout / transient 503
→ bounded retry with same Idempotency-Key for the same mutation

409 IDEMPOTENCY_CONFLICT
→ do not blind-retry; inspect caller logic

409 MEMORY_STATE_CONFLICT
→ refresh the resource state and decide explicitly

422 INVALID_REQUEST
→ fix the request; do not retry unchanged

auth failure
→ fix credentials/configuration; do not loop
```

Avoid infinite retries.

---

## 10. n8n integration today

Sofias Memory does not require a dedicated n8n node.

The built-in **HTTP Request** node can call any Sofias Memory REST endpoint today.

Official n8n HTTP Request documentation:

https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/

The node supports HTTP methods, URLs, headers, JSON bodies, credentials, response formatting, and curl import.

A dedicated Sofias Memory community/custom node can be layered on top of the same HTTP contract later without changing Sofias Memory itself.

---

## 11. n8n: create a Header Auth credential

Prefer storing the API key in n8n credentials rather than typing it into every workflow node.

Conceptually configure a generic/header credential with:

```text
Header name:
X-API-Key

Header value:
<your Sofias Memory API key>
```

Then reuse that credential in each HTTP Request node.

If your n8n deployment uses another secret-management pattern, follow that platform policy instead.

---

## 12. n8n: Create Memory node

Add an **HTTP Request** node.

Configure:

```text
Method:
POST

URL:
https://memory.example.com/api/v1/memories

Authentication:
Header credential containing X-API-Key

Send Headers:
Yes

Additional header:
Idempotency-Key = {{$execution.id}}:profile:{{$json.customer_id}}

Send Body:
Yes

Body Content Type:
JSON
```

Example JSON body:

```json
{
  "memory_type": "profile",
  "scope": "global",
  "content": "Prefers communication by email.",
  "provenance": {
    "origin_kind": "imported",
    "source_system": "workflow-engine",
    "source_ref": "customer:42"
  }
}
```

For production workflows, generate an idempotency key that remains stable across retries of the **same logical mutation**. Do not blindly generate a new random key on every retry.

---

## 13. n8n: Recall Memory node

Add another HTTP Request node.

Configure:

```text
Method:
POST

URL:
https://memory.example.com/api/v1/memories/recall

Authentication:
Header credential containing X-API-Key

Send Body:
Yes

Body Content Type:
JSON
```

Example body:

```json
{
  "query": "How does this customer prefer to be contacted?",
  "scopes": ["global"],
  "top_k": 5
}
```

The output can then feed downstream workflow logic.

A good integration keeps the returned `memory`, `relevance`, and `is_current_truth` fields distinct instead of collapsing them into one text string too early.

---

## 14. n8n: project-scoped memory

If a workflow operates inside a project boundary, use an explicit project scope:

```json
{
  "memory_type": "semantic",
  "scope": "project:atlas",
  "content": "Production deployments use the South America region.",
  "provenance": {
    "origin_kind": "imported",
    "source_system": "workflow-engine",
    "source_ref": "deployment-config:atlas"
  }
}
```

Recall must request that scope explicitly:

```json
{
  "query": "Where do we deploy Atlas?",
  "scopes": ["project:atlas"]
}
```

Requesting `project:atlas` does not automatically include `global`.

If both are needed:

```json
{
  "scopes": ["global", "project:atlas"]
}
```

---

## 15. n8n: recommended workflow shape

A simple memory-aware automation can use this shape:

```text
Trigger
  ↓
Normalize workflow input
  ↓
HTTP Request: /api/v1/info (optional cached compatibility check)
  ↓
HTTP Request: /api/v1/memories/recall
  ↓
Business / AI / routing logic
  ↓
Decision: should new durable memory be persisted?
  ↓ yes
HTTP Request: /api/v1/memories
```

Do not automatically persist every transient workflow value as durable memory.

The workflow should decide intentionally what deserves long-term persistence.

---

## 16. n8n: Supersede instead of overwriting

Cognitive Memory does not expose `PATCH`.

When a durable fact changes, first resolve the exact current memory and call:

```text
POST /api/v1/memories/{memory_id}/supersede
```

This preserves historical lineage rather than silently editing the old fact in place.

That distinction is useful in automations where changing configuration, customer preference, or project decisions should remain auditable over time.

---

## 17. n8n: precise Forget

When one exact Cognitive Memory must be removed:

```text
POST /api/v1/memories/{memory_id}/forget
```

This is destructive.

Do not substitute Knowledge Memory `/api/v1/forget` for Cognitive Forget; they operate on different memory planes.

A production workflow should generally require an explicit business decision before destructive Forget.

---

## 18. Import curl into n8n

The n8n HTTP Request node supports importing curl commands.

That makes examples from this Wiki or the repository useful as a starting point:

1. copy a working curl example;
2. open an HTTP Request node;
3. use n8n's curl import;
4. replace literal secrets with n8n credentials/expressions;
5. parameterize body fields with workflow data.

After importing, inspect the generated node configuration instead of assuming every literal value is production-ready.

---

## 19. Python example

Using `requests`:

```python
import requests

base_url = "https://memory.example.com"
headers = {
    "X-API-Key": "sf-...",
    "Content-Type": "application/json",
}

response = requests.post(
    f"{base_url}/api/v1/memories/recall",
    headers=headers,
    json={
        "query": "What is the preferred deployment region?",
        "scopes": ["global"],
        "top_k": 5,
    },
    timeout=30,
)
response.raise_for_status()
data = response.json()
```

Use your normal secret-management mechanism rather than hardcoding the key.

---

## 20. JavaScript example

```javascript
const response = await fetch(
  "https://memory.example.com/api/v1/memories/recall",
  {
    method: "POST",
    headers: {
      "X-API-Key": process.env.SOFIAS_MEMORY_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query: "What is the preferred deployment region?",
      scopes: ["global"],
      top_k: 5,
    }),
  },
);

if (!response.ok) {
  throw new Error(`Sofias Memory request failed: ${response.status}`);
}

const data = await response.json();
```

In production, parse the stable error envelope instead of throwing only on HTTP status.

---

## 21. PHP / Laravel example

With Laravel's HTTP client:

```php
use Illuminate\Support\Facades\Http;

$response = Http::withHeaders([
    'X-API-Key' => config('services.sofias_memory.api_key'),
])->post(
    config('services.sofias_memory.url').'/api/v1/memories/recall',
    [
        'query' => 'What is the preferred deployment region?',
        'scopes' => ['global'],
        'top_k' => 5,
    ],
);

$response->throw();
$data = $response->json();
```

For mutation retries, keep the same `Idempotency-Key` for the same logical operation.

---

## 22. Network and deployment considerations

Keep PostgreSQL and Neo4j private to the deployment network.

Expose only the Sofias Memory application through your reverse proxy/TLS boundary.

For callers running in the same Docker/network environment, use the service/internal DNS name when appropriate instead of routing through the public internet.

For cross-host integrations, use HTTPS and protect the static API key as a secret.

---

## 23. Do not bypass readiness

Before depending on Sofias Memory during startup/deployment orchestration, use:

```text
GET /health/ready
```

A live process is not necessarily ready to serve business operations.

This distinction matters during automatic schema migration, dependency recovery, worker startup, and storage convergence.

---

## 24. Integration checklist

Before calling an integration production-ready, confirm:

- the API is behind TLS;
- `X-API-Key` is stored as a secret;
- `/api/v1/info` capability negotiation is implemented where useful;
- Knowledge vs. Cognitive Memory is chosen intentionally;
- mutation retries use stable idempotency keys;
- 409/422/503 responses are handled explicitly;
- no infinite retry loop exists;
- scope selection is explicit;
- destructive Forget requires the intended business decision;
- readiness is used during deployment/startup orchestration;
- the caller does not treat recalled memory as authorization or executable policy by default.

---

## Go deeper

- [Getting Started](Getting-Started)
- [Core Concepts](Core-Concepts)
- [Cognitive Memory](Cognitive-Memory)
- [Knowledge Memory](Knowledge-Memory)
- [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
- [n8n HTTP Request node documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)

This Wiki guide provides integration patterns. The versioned Sofias Memory API contract remains authoritative for exact schemas and behavior.