# Provenance & Feedback

Provenance answers **where knowledge came from**. Feedback records **how useful a past Recall result was**.

They are related but different concerns:

- provenance is evidence lineage;
- feedback is a durable evaluation signal that can later influence ranking through Improve.

This page covers the source-backed Knowledge Memory provenance API. Cognitive Memory has its own first-class provenance model documented in [Cognitive Memory](Cognitive-Memory).

---

## 1. Why provenance matters

A retrieval result is more useful when a caller can answer:

- which Source produced this knowledge?
- which Document/Chunk supports it?
- which evidence supports a graph relation?
- what context was used by a previous Recall query?
- is that referenced evidence still available now?

Sofias Memory persists enough lineage to answer those questions through explicit read APIs.

---

## 2. Provenance endpoints

The public provenance API is read-only:

```text
GET /api/v1/provenance/source/{source_id}
GET /api/v1/provenance/relation/{relation_id}
GET /api/v1/provenance/query/{query_id}
```

All require `X-API-Key`.

These endpoints hydrate authoritative PostgreSQL state. Provenance is not reconstructed from Neo4j as the source of truth.

---

## 3. Source provenance

```text
GET /api/v1/provenance/source/{source_id}
```

This traces one Source into the knowledge derived from it.

The result includes Source metadata such as:

```text
source_id
dataset_id
kind
name
mime_type
original_uri
byte_size
status
storage_available
```

and bounded collections of:

```text
documents
chunks
entities
relations
```

Use this when you need to inspect what a Source contributed to the knowledge base.

---

## 4. Documents in Source provenance

A provenance Document includes bounded metadata rather than full normalized text:

```text
document_id
title
language
chunk_count
```

This keeps the provenance response useful without turning it into a full document-download API.

---

## 5. Chunk evidence

Source-level provenance can return bounded Chunk evidence:

```text
chunk_id
document_id
ordinal
quote
start_char
end_char
```

`quote` is the evidence snippet exposed for inspection.

The character offsets describe where that evidence sits within its authoritative Document representation.

---

## 6. Entity and relation contributions

Source provenance can also show entities mentioned by chunks and relation summaries supported by the Source.

An entity contribution exposes:

```text
entity_id
name
entity_type
description
```

A relation summary exposes:

```text
relation_id
source_entity_id
target_entity_id
predicate
confidence
```

Use the relation provenance endpoint when you need the evidence trail for one particular relation.

---

## 7. Relation provenance

```text
GET /api/v1/provenance/relation/{relation_id}
```

This traces an authoritative relation back to the chunks that support it.

The relation-level response exposes:

```text
relation_id
dataset_id
source_entity_id
target_entity_id
predicate
description
confidence
evidence[]
```

Each evidence item can include:

```text
source_id
source_name
document_id
chunk_id
chunk_ordinal
quote
start_char
end_char
confidence
url
```

This is the correct API when a consumer needs to explain why a graph relation exists.

---

## 8. Query provenance

```text
GET /api/v1/provenance/query/{query_id}
```

Recall queries are durable enough to be audited later.

Query provenance exposes:

```text
query_id
query_text
dataset_ids
mode
answer
model
created_at
references
session_uuid
session_context
```

The persisted Query is the audit identity. Keep the returned `query_id` when a downstream workflow may need explanation, review, or feedback.

---

## 9. References can become unavailable

A past query reference is hydrated against current state.

Each reference includes:

```text
source_id
document_id
chunk_id
chunk_ordinal
score
available
quote
source_name
```

`available=false` is meaningful.

For example, evidence used by a historical query may later have been forgotten or otherwise made unavailable.

Sofias Memory preserves the audit identity without pretending deleted evidence still exists.

---

## 10. Session context provenance

When RAG Recall explicitly uses Session context, Query provenance can expose the exact SessionEntries used.

Each item includes:

```text
entry_id
role
content
available
```

This context provenance is distinct from knowledge-reference provenance.

That distinction matters: conversational/session context and retrieved knowledge do not have the same origin or lifecycle.

See [Sessions](Sessions) for the Session model.

---

## 11. Provenance is not authorization

Evidence that a Source, Chunk, SessionEntry, Skill, or other resource contributed context does not grant authority.

Consumers should never interpret provenance as:

- permission to execute a tool;
- permission to reveal data;
- a policy grant;
- a trusted instruction merely because it was retrieved.

Provenance tells you origin and evidence lineage.

Policy decisions remain the caller's responsibility.

---

## 12. Feedback endpoint

```text
POST /api/v1/feedback
```

Feedback applies to a **past Recall query**.

It can target either:

```text
answer
reference
```

and uses a score:

```text
-1 = negative
 0 = neutral
 1 = positive
```

---

## 13. Feedback on an answer

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/feedback" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query_id": "<query-uuid>",
    "target_type": "answer",
    "score": 1,
    "comment": "Answer matched the source material."
  }'
```

For `target_type=answer`, omit `target_id`.

---

## 14. Feedback on one reference

For one specific retrieved reference:

```json
{
  "query_id": "<query-uuid>",
  "target_type": "reference",
  "target_id": "<reference-uuid>",
  "score": -1,
  "comment": "This reference was not relevant to the question."
}
```

For `target_type=reference`, `target_id` is required and must correspond to the referenced query’s actual result shape.

---

## 15. Feedback persistence

A persisted feedback record exposes:

```text
feedback_id
query_id
target_type
target_id
score
comment
applied_at
created_at
```

`comment` is optional and normalized; blank comments become absent.

The public maximum comment length is 4000 characters.

---

## 16. Feedback is not real-time ranking mutation

Recording feedback does **not** retroactively modify a past query and does not instantly rewrite ranking state.

Feedback is persisted immediately, but its ranking effect is applied when the Improve pipeline’s feedback-weight stage runs.

Conceptually:

```text
Recall
  ↓
query_id + references
  ↓
POST /feedback
  ↓
feedback persisted
  ↓
Improve feedback_weights stage
  ↓
future ranking can reflect applied feedback
```

This separation keeps feedback durable and observable rather than hiding an immediate ranking mutation inside the POST request.

---

## 17. `applied_at`

`applied_at` is nullable.

A newly recorded feedback item can exist before Improve has applied it.

Consumers should not assume:

```text
created_at == applied_at
```

or that a non-null `feedback_id` means ranking was already recalculated.

---

## 18. Feedback and Improve

Improve is a durable knowledge-maintenance pipeline.

Feedback is one signal Improve can consume alongside other maintenance/reconciliation work.

Because Improve creates a PipelineRun, its execution can be observed using [Runs & Reliability](Runs-and-Reliability).

---

## 19. Cognitive Memory provenance is different

Native Cognitive Memory does not reuse this Source/Chunk provenance API as its primary provenance model.

A `MemoryItem` carries its own first-class provenance with origin kinds such as:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

Precise Cognitive Forget also scrubs external provenance references while preserving only minimal provenance identity required by the contract.

See [Cognitive Memory](Cognitive-Memory).

---

## 20. Recommended audit workflow

```text
Recall
  ↓
keep query_id
  ↓
inspect query provenance when needed
  ↓
inspect Source/Relation provenance for deeper evidence
  ↓
record Feedback if a human/system evaluates the result
  ↓
run/observe Improve when maintenance is appropriate
```

---

## 21. Related pages

- [Knowledge Memory](Knowledge-Memory)
- [Datasets & Sources](Datasets-and-Sources)
- [Sessions](Sessions)
- [Runs & Reliability](Runs-and-Reliability)
- [Cognitive Memory](Cognitive-Memory)
- [API Guide](API-Guide)

For exact schemas, use the versioned [API semantics](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).