# Sessions

Sessions are durable temporal context containers.

They let a caller preserve an ordered history of contextual entries, associate knowledge operations with a stable session identity, and optionally include recent Session context in supported Recall flows.

A Session is **not** a Cognitive `MemoryItem`, not a chat-provider session, and not an authorization boundary.

---

## 1. Mental model

A Session has two public identities:

```text
session_uuid
session_id
```

`session_uuid` is the structural UUID used by relationships and filters inside Sofias Memory.

`session_id` is the caller-facing external identifier. You may supply it when creating a Session; if omitted, the server generates a UUID and uses its textual form for both identities.

A Session also carries:

```text
name?
status
metadata
created_at
updated_at
archived_at?
```

---

## 2. What Sessions are for

Typical uses include:

- conversation/thread context;
- one workflow execution lineage;
- a support case or ticket timeline;
- a research/reasoning thread;
- grouping knowledge operations under one external context identity;
- retaining append-only contextual events that should not become durable Cognitive Memory automatically.

A Session gives the caller a durable temporal container. The caller still decides what, if anything, becomes long-lived Cognitive Memory.

---

## 3. Session lifecycle

Sessions have a management lifecycle rather than destructive deletion.

Supported management operations:

```text
POST  /api/v1/sessions
GET   /api/v1/sessions
GET   /api/v1/sessions/{session_uuid}
PATCH /api/v1/sessions/{session_uuid}
POST  /api/v1/sessions/{session_uuid}/archive
POST  /api/v1/sessions/{session_uuid}/restore
```

There is intentionally no public Session hard-delete/purge route.

Archive/restore changes Session availability/state while preserving its identity and history.

---

## 4. Create a Session

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/sessions" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "support-case-4821",
    "name": "Customer support case 4821",
    "metadata": {
      "channel": "web",
      "customer_ref": "customer:42"
    }
  }'
```

`session_id` is optional.

Creating a Session with an already-existing external `session_id` is a conflict, not a silent upsert.

---

## 5. Update Session metadata

`PATCH /sessions/{session_uuid}` can change only:

```text
name
metadata
```

It cannot change:

```text
session_id
status
```

`metadata` replacement is whole-object replacement, not a deep merge.

Use the explicit archive/restore operations for status transitions.

---

## 6. SessionEntry: append-only context

Each Session can contain append-only entries:

```text
POST /api/v1/sessions/{session_uuid}/entries
GET  /api/v1/sessions/{session_uuid}/entries
```

There is intentionally no item-level:

```text
PATCH
PUT
DELETE
```

for SessionEntry.

That append-only rule keeps historical Session context stable and auditable.

---

## 7. Append a SessionEntry

A SessionEntry contains:

```text
external_id?
role
content
metadata
```

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/sessions/$SESSION_UUID/entries" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "external_id": "event-00017",
    "role": "user",
    "content": "Please keep this case focused on the billing issue.",
    "metadata": {
      "channel": "web"
    }
  }'
```

### `role` is descriptive, not privileged

`role` is an open-ended contextual label such as:

```text
user
assistant
agent
tool
system-note
workflow
```

Sofias Memory does not translate it into a privileged provider/LLM role.

### `external_id` enables safe replay

When present, `external_id` is unique within that Session and identifies one exact append operation.

This is useful when an external system may retry delivery after a timeout.

Without `external_id`, every successful append is a new SessionEntry.

---

## 8. Query history

A Session also exposes a lightweight history of knowledge queries associated with it:

```text
GET /api/v1/sessions/{session_uuid}/queries
```

Each query-history item can include:

```text
query_id
dataset_ids
mode
query_text?
answer?
model?
created_at
```

This is a lightweight projection for Session history, not the complete provenance structure of the query.

Use the dedicated provenance APIs when you need full evidence tracing.

---

## 9. Sessions and Knowledge Recall

The legacy/source-backed Recall API can associate a query with a Session using the external:

```text
session_id
```

Session association is additive: Recall remains valid without a Session.

When Session context should participate in a RAG answer, the caller can opt in with:

```text
include_session_context = true
```

This is deliberately opt-in rather than automatic.

The supported semantic combination is constrained: Session context inclusion is intended for grounded RAG response generation, not as a hidden modifier to every retrieval mode.

For exact Recall request constraints, use the canonical [`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 10. Sessions and PipelineRuns

Knowledge pipeline work can be structurally associated with a Session.

`GET /api/v1/runs` supports filtering by:

```text
session_uuid
```

This lets a caller inspect durable ingestion/processing work related to one Session without conflating the textual external `session_id` with the internal structural relation.

---

## 11. Sessions and Agents

Agent Profiles can be explicitly associated with Sessions:

```text
GET    /api/v1/agents/{agent_uuid}/sessions
PUT    /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
DELETE /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
```

The association is management metadata only.

It does **not** mean Sofias Memory runs the Agent, injects the Session automatically into a model, owns the conversation runtime, or stores a hidden participation transcript.

Read [Skills & Agents](Skills-and-Agents) for the management model.

---

## 12. Sessions vs Cognitive Memory

A SessionEntry is not automatically a Cognitive Memory.

Use a Session when you need:

```text
temporal context
ordered event history
conversation/workflow continuity
```

Use Cognitive Memory when you need:

```text
a durable fact or preference
first-class provenance
scope
validity window
current/historical truth
Supersede
precise destructive Forget
```

A caller may inspect Session context and explicitly decide to create a `MemoryItem`, but that promotion decision belongs to the caller.

---

## 13. Sessions vs provider sessions

Do not reuse one identifier to represent all of these concepts:

```text
Sofias Memory session_uuid
Sofias Memory session_id
external conversation/thread ID
provider realtime/session ID
Cognitive memory_id
PipelineRun run_id
```

Keeping them distinct avoids accidental coupling between infrastructure layers.

---

## 14. Practical pattern

A typical flow is:

```text
1. create or resolve your external Session identity
2. append meaningful context events
3. associate Remember/Recall work when appropriate
4. use session_uuid to inspect related runs/history
5. archive the Session when the context is no longer active
6. explicitly create Cognitive Memory only for facts that deserve durable semantic memory
```

---

## 15. Canonical references

- [`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [ADR-0012 — First-class durable Sessions](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0012-first-class-durable-sessions.md)
- [Session Feature Contract](https://github.com/kallbuloso/sofias_memory/blob/main/docs/product/Sofias_Memory_Feature_Contract_v0.3.0_Sessions.md)

For a broader domain map, return to [Core Concepts](Core-Concepts).
