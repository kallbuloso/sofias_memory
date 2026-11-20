# Cognitive Memory

Cognitive Memory is Sofias Memory's first-class model for durable facts, preferences, decisions, and persistent semantic context.

Use it when information should exist directly as memory instead of being represented as a fake document, source, or chat-history entry.

Cognitive Memory was introduced in `v0.7.0` and is authoritative in PostgreSQL + pgvector.

---

## 1. When to use Cognitive Memory

Cognitive Memory is a good fit for information such as:

- durable preferences;
- persistent characteristics;
- project or application facts;
- decisions that should remain retrievable later;
- semantically useful context that is not naturally a document;
- facts with an explicit validity period;
- memories that may later be superseded or precisely forgotten.

Examples:

```text
Prefers concise technical explanations.
Project Atlas uses PostgreSQL as its authoritative database.
The customer approved the revised delivery date.
Preferred deployment region is South America.
```

Do **not** use Cognitive Memory merely because some text needs embeddings. Source-backed files, URLs, and documents belong to [Knowledge Memory](Knowledge-Memory).

---

## 2. MemoryItem

The primary Cognitive Memory resource is a `MemoryItem`.

A non-forgotten item conceptually carries:

```text
memory_id
memory_type
scope
content
lifecycle
confidence?
valid_from?
valid_until?
created_at
superseded_at?
superseded_by?
forgotten_at?
provenance
```

The embedding is stored internally in PostgreSQL + pgvector and is never exposed by the public API.

---

## 3. Memory types

The current contract supports exactly two Cognitive Memory types.

### `profile`

Use `profile` for persistent preferences, characteristics, settings-like context, or other profile-oriented facts.

```text
Prefers metric units.
Preferred response language is Portuguese.
Uses dark mode by default.
```

### `semantic`

Use `semantic` for persistent facts, concepts, project knowledge, or decisions that should exist directly as memory.

```text
Project Atlas uses PostgreSQL.
Contract renewal is scheduled for November.
The new API gateway is the authoritative ingress path.
```

The current Cognitive Memory contract does **not** define `episodic` or `procedural` MemoryItems.

Procedural knowledge is modeled separately through Skills.

---

## 4. Scopes

Every non-forgotten Cognitive Memory has one explicit scope.

Supported forms are:

```text
global
project:<key>
```

Examples:

```text
global
project:atlas
project:billing-api
```

Scope matching is exact.

A recall request for:

```json
{
  "scopes": ["project:atlas"]
}
```

does not automatically include `global`.

If the caller wants both:

```json
{
  "scopes": ["global", "project:atlas"]
}
```

must be supplied explicitly.

Scope is a retrieval namespace, not an authentication or tenancy boundary.

---

## 5. Provenance

Every Cognitive `MemoryItem` has exactly one first-class provenance record.

Provenance answers:

> Where did this memory come from?

Supported origin kinds are:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

The provenance model includes a `source_system` slug and, depending on the origin, may include opaque external references such as:

```text
conversation_uuid
turn_uuid
task_uuid
confirmation_ref
source_ref
observed_at
```

These are external references only. Sofias Memory does not create cross-database foreign keys into the caller's database.

Example:

```json
{
  "origin_kind": "user_asserted",
  "source_system": "crm",
  "source_ref": "customer-profile:42"
}
```

`source_system` should identify the originating system, not hide a full resource identifier.

---

## 6. Confidence

`confidence` is optional and, when present, ranges from `0.0` to `1.0`.

It represents epistemic or extraction confidence. It is not authorization and is not a universal truth score.

`inferred` provenance requires confidence.

A user assertion does **not** automatically imply:

```text
confidence = 1.0
```

Confidence does not affect Cognitive Recall ranking in v0.7.0.

---

## 7. Temporal validity

A memory may optionally define:

```text
valid_from
valid_until
```

Semantics:

- `valid_from` is inclusive;
- `valid_until` is exclusive.

Example:

```json
{
  "valid_from": "2026-09-01T00:00:00Z",
  "valid_until": "2026-10-01T00:00:00Z"
}
```

The memory is eligible starting exactly at September 1 and is no longer valid starting exactly at October 1.

Temporal validity is independent of semantic relevance.

---

## 8. Lifecycle

A Cognitive Memory has one of three lifecycle states:

```text
active
superseded
forgotten
```

### Active

An `active` memory may represent current truth, subject to its validity window.

### Superseded

A `superseded` memory has been explicitly replaced by one newer MemoryItem.

Supersession preserves the old memory as historical evidence.

### Forgotten

A `forgotten` memory is a destructive tombstone.

Forget removes the retrievable cognitive payload. The tombstone preserves only minimal identity/lifecycle evidence and scrubbed provenance evidence.

Forgotten content cannot be returned by Cognitive Recall, including historical recall.

---

## 9. Create a memory

Endpoint:

```text
POST /api/v1/memories
```

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Idempotency-Key: profile-001" \
  -H "Content-Type: application/json" \
  -d '{
    "memory_type": "profile",
    "scope": "global",
    "content": "Prefers concise technical explanations.",
    "provenance": {
      "origin_kind": "user_asserted",
      "source_system": "example-app",
      "source_ref": "profile:42"
    }
  }'
```

Create is synchronous.

It does not create a `PipelineRun`.

The content is embedded before the short authoritative PostgreSQL transaction.

---

## 10. Get by identity

Endpoint:

```text
GET /api/v1/memories/{memory_id}
```

Example:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/memories/<memory_id>"
```

Get is an identity lookup, not semantic search.

A forgotten item remains addressable as a safe tombstone.

---

## 11. Typed Cognitive Recall

Endpoint:

```text
POST /api/v1/memories/recall
```

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/memories/recall" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How should technical explanations be presented?",
    "memory_types": ["profile", "semantic"],
    "scopes": ["global"],
    "top_k": 5
  }'
```

Each recall result contains:

```text
memory
relevance
is_current_truth
```

Retrieval uses exact pgvector cosine similarity.

The deterministic ranking is:

```text
relevance DESC
created_at DESC
memory_id ASC
```

Confidence does not alter ranking.

---

## 12. Historical recall with `as_of`

Cognitive Recall can evaluate memory against a historical timestamp.

Example:

```json
{
  "query": "Which support channel was preferred?",
  "scopes": ["global"],
  "as_of": "2026-08-01T12:00:00Z",
  "top_k": 10
}
```

This is not implemented as a simple check for `lifecycle == active`.

A memory that is superseded today may still have been current truth at the requested historical timestamp.

`is_current_truth` tells the caller whether the returned item was current truth at the effective `as_of` time.

Forgotten memories are never returned, even when the requested `as_of` predates the Forget operation.

---

## 13. Supersede

Endpoint:

```text
POST /api/v1/memories/{memory_id}/supersede
```

Use Supersede when an existing memory should remain as history but a newer memory should become current.

Example semantic transition:

```text
Old:
Preferred support channel is email.

New:
Preferred support channel is WhatsApp.
```

The replacement inherits the old memory's:

```text
memory_type
scope
```

The caller supplies the replacement content, optional confidence/validity, and new provenance.

The transition is atomic:

```text
old ACTIVE -> SUPERSEDED
replacement -> ACTIVE
```

A target that is already superseded or forgotten cannot be superseded again by a new distinct operation.

---

## 14. Precise Forget

Endpoint:

```text
POST /api/v1/memories/{memory_id}/forget
```

Cognitive Forget operates on one exact MemoryItem.

After Forget, the authoritative payload is destructively scrubbed:

```text
content -> null
embedding -> null
scope -> null
confidence -> null
valid_from -> null
valid_until -> null
```

External provenance references are also removed.

Minimal evidence remains, including identity, memory type, lifecycle timestamps, lineage where relevant, and provenance `origin_kind` + `source_system`.

Forget does not cascade through supersession lineage.

Forgetting an old superseded item does not forget its replacement.

---

## 15. Idempotency

Cognitive write operations support `Idempotency-Key`.

Safe retry semantics are based on the same key plus the same normalized semantic request.

The server persists a keyed HMAC digest, not the plaintext request body.

Configuration:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

This secret must be high entropy and distinct from `API_KEY`.

Important distinction:

> Idempotency is not semantic deduplication.

Two equal Create requests without the same Idempotency-Key may create two distinct MemoryItems.

---

## 16. Capability negotiation

Call:

```text
GET /api/v1/info
```

Cognitive Memory contract `1` advertises:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

Integrations should negotiate these capabilities instead of inferring them from application SemVer.

---

## 17. What Cognitive Memory does not do

The v0.7.0 contract intentionally does not provide:

```text
PATCH /memories
semantic deduplication
AI conflict resolution
importance weighting
episodic MemoryItems
procedural MemoryItems
Cognitive Neo4j projection
Cognitive graph_outbox
```

These boundaries are part of the model, not missing implementation details.

---

## 18. Cognitive Memory vs. other concepts

| Need | Use |
|---|---|
| Durable fact, preference, decision, persistent semantic context | Cognitive Memory |
| Source-backed file/URL/document knowledge | [Knowledge Memory](Knowledge-Memory) |
| Temporary/temporal interaction history | Session |
| Reusable procedure | Skill |
| Durable identity/configuration for an external agent | Agent profile |
| Long-running source-processing operation | PipelineRun |

---

## Go deeper

- [Core Concepts](Core-Concepts)
- [Getting Started](Getting-Started)
- [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [Native Cognitive Memory Feature Contract](https://github.com/kallbuloso/sofias_memory/blob/main/docs/product/Sofias_Memory_Feature_Contract_v0.7.0_Native_Cognitive_Memory.md)
- [ADR-0016](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0016-native-cognitive-memory-model-and-lifecycle.md)

This Wiki page is explanatory. The versioned contract shipped with the repository remains authoritative.