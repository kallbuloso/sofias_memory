# Core Concepts

Sofias Memory is easier to use when its concepts remain separate.

The project deliberately avoids turning every kind of durable context into a generic "memory record". Documents, cognitive facts, sessions, procedures, agents, provenance, and background runs have different semantics and different lifecycle rules.

This page is the mental model to use before designing an integration.

---

## 1. The authority model

The most important architectural rule is:

> **PostgreSQL + pgvector is authoritative.**

Durable application state lives in PostgreSQL, including datasets, sources, chunks, entities, relations, runs, sessions, skills, agent profiles, provenance, and Cognitive Memory.

Neo4j is a **reconstructible projection** used for knowledge-graph workloads. It is not authoritative and can be rebuilt from PostgreSQL.

Cognitive Memory has no Neo4j projection in v0.7.0.

Original source objects are stored durably on the local filesystem by default or through the supported S3-compatible storage backend.

---

## 2. Two memory planes

Sofias Memory exposes two intentionally different memory planes.

### Knowledge Memory

Knowledge Memory is **source-backed**.

Use it when your memory originates from material such as:

- text;
- files;
- URLs;
- documents;
- reference material;
- imported knowledge corpora.

The conceptual flow is:

```text
Dataset
  └── Source
        └── Document
              └── Chunks
                    ├── embeddings
                    ├── entities
                    ├── relations
                    ├── summaries
                    └── provenance
```

Knowledge Memory is designed for ingestion, processing, retrieval, provenance, RAG, graph traversal, reconciliation, and source/dataset lifecycle operations.

### Cognitive Memory

Cognitive Memory is **first-class durable semantic memory**.

Use it for facts, preferences, persistent characteristics, decisions, or other semantically useful context that should exist directly as memory instead of pretending to be a document.

Its primary resource is:

```text
MemoryItem
```

A Cognitive Memory does not require a Dataset, Source, Document, or Chunk.

This separation is deliberate. If something is a durable fact, it should not need a fake document merely to become retrievable.

---

## 3. Dataset

A `Dataset` is a logical namespace for source-backed Knowledge Memory.

Datasets group related Sources and provide an explicit boundary for ingestion, rebuild, recall, and administrative lifecycle operations.

A Dataset is **not** an authentication boundary and is not generic multi-tenancy.

Cognitive Memory does not use Dataset as its scope model.

---

## 4. Source, Document, and Chunk

A `Source` represents original ingested material.

Depending on the input, the durable original may be text, a file, or content fetched from an HTTPS URL.

A `Document` represents the normalized/processed document state associated with that Source and generation.

`Chunk`s are retrievable semantic units derived from the processed document. They carry embeddings and participate in source-backed recall.

These concepts are part of Knowledge Memory, not Cognitive Memory.

---

## 5. Knowledge Recall

The legacy Knowledge Memory endpoint:

```text
POST /api/v1/recall
```

retrieves source-backed knowledge.

Depending on the selected mode, recall can use vector retrieval, lexical retrieval, summaries, graph traversal, hybrid rank fusion, or graph-grounded RAG.

Knowledge Recall can produce source-grounded context and provenance back to the original material.

It should not be confused with Cognitive Recall.

---

## 6. MemoryItem

A `MemoryItem` is the first-class resource of Cognitive Memory.

In v0.7.0, supported memory types are:

```text
profile
semantic
```

### `profile`

Use `profile` for durable preferences, persistent characteristics, configuration-like personal or application context, and other profile-oriented facts.

Examples:

```text
Prefers concise technical explanations.
Uses metric units.
Primary project language is Portuguese.
```

### `semantic`

Use `semantic` for persistent facts, decisions, concepts, or knowledge that should exist directly as Cognitive Memory.

Examples:

```text
Project Atlas uses PostgreSQL as its authoritative database.
The customer approved the revised delivery date.
The preferred deployment region is South America.
```

`episodic` and `procedural` are not Cognitive Memory types in the current contract.

Procedural memory is represented separately by Skills.

---

## 7. Cognitive Memory scopes

Every non-forgotten Cognitive Memory has an explicit scope.

Supported scopes are:

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

Requesting:

```text
project:atlas
```

does not automatically include `global` or another project scope.

If a caller wants both, it must request both explicitly.

Cognitive scope is a retrieval namespace, not authentication or tenancy.

---

## 8. Provenance

Provenance answers:

> **Where did this memory come from?**

Sofias Memory treats provenance as first-class data rather than an arbitrary metadata object.

For Cognitive Memory, every `MemoryItem` has exactly one provenance record.

Supported origin kinds are:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

The provenance model can carry a system slug plus external references such as conversation, turn, task, confirmation, source, and observation time where appropriate.

Those external references are opaque identifiers. Sofias Memory does not create cross-database foreign keys into the calling application.

`source_system` is only the identifier of the originating system, for example:

```text
crm
workflow-engine
research-tool
example-app
```

It is not a place to hide full resource identifiers.

---

## 9. Confidence

Cognitive Memory may carry an optional caller-supplied `confidence` between `0.0` and `1.0`.

It represents epistemic/extraction confidence, not authorization or absolute truth.

`inferred` provenance requires confidence.

A user assertion does **not** automatically imply `confidence = 1.0`.

Confidence does not change Cognitive Recall ranking in v0.7.0.

---

## 10. Cognitive Memory lifecycle

A Cognitive Memory has one of three lifecycle states:

```text
active
superseded
forgotten
```

### Active

An `active` MemoryItem may represent current truth, subject to its temporal validity window.

### Superseded

A `superseded` MemoryItem has been explicitly replaced by exactly one newer MemoryItem.

Supersession preserves history. It does not erase the old memory.

The old item records when it was superseded and which MemoryItem replaced it.

### Forgotten

A `forgotten` MemoryItem is a destructive tombstone.

Forget removes the cognitive payload, including content, embedding, scope, confidence, and validity window. External provenance references are also scrubbed.

Only minimal lifecycle/identity evidence remains.

Forgotten content is not available to current or historical Cognitive Recall.

---

## 11. Supersede vs. Forget

These operations solve different problems.

### Supersede

Use Supersede when:

> "This memory existed, but a newer memory is now the current version."

Example:

```text
Old: Preferred support channel is email.
New: Preferred support channel is WhatsApp.
```

The old memory remains historical evidence and points to its replacement.

### Forget

Use Forget when:

> "This exact memory must no longer be retrievable as cognitive content."

Forget is destructive and precise by `memory_id`.

Forgetting a superseded predecessor does not automatically forget its replacement.

---

## 12. Temporal truth

Cognitive Memory supports temporal validity and historical recall.

A memory can optionally define:

```text
valid_from
valid_until
```

`valid_from` is inclusive.

`valid_until` is exclusive.

Typed Cognitive Recall can also receive:

```text
as_of
```

This allows callers to ask:

> "Which relevant memories were considered current truth at this time?"

A memory may be `superseded` today and still have been current truth at an earlier `as_of` timestamp.

This is why historical truth is not implemented as a simple `lifecycle == active` filter.

---

## 13. Cognitive Recall

The Cognitive Memory endpoint is:

```text
POST /api/v1/memories/recall
```

It is separate from Knowledge Recall.

Cognitive Recall uses exact pgvector cosine similarity in v0.7.0 and returns, for each result:

```text
memory
relevance
is_current_truth
```

Ranking is deterministic:

```text
relevance DESC
created_at DESC
memory_id ASC
```

Confidence does not modify that ranking.

A forgotten MemoryItem never appears, even for historical `as_of` queries.

---

## 14. Idempotency

Cognitive write operations can use:

```text
Idempotency-Key
```

The key protects callers from accidentally applying the same mutation multiple times during retries.

The server stores a keyed, non-reversible HMAC digest of the semantic request rather than storing the plaintext request body for replay detection.

The HMAC secret is configured through:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

It must be distinct from `API_KEY`.

Idempotency is not semantic deduplication. Two equal memories created without the same Idempotency-Key remain two independent MemoryItems.

---

## 15. Sessions

A `Session` is a durable temporal context boundary.

It can contain append-only `SessionEntry` history and can be associated with Remember/Recall operations for contextual workflows.

A Session is **not** permanent Cognitive Memory.

Conversation history and durable memory solve different problems and should not be silently merged.

---

## 16. Skills

A `Skill` is first-class procedural memory.

Skills support immutable revisions, current-revision selection, rollback, archive/restore, semantic resolve, and standalone `SKILL.md` interoperability.

Sofias Memory stores and resolves Skills.

It does not execute the procedure and does not authorize tool use.

This is why `procedural` is not a Cognitive `MemoryItem` type in v0.7.0.

---

## 17. Agent profiles

Agent profiles store durable identity/configuration for external agent systems.

Sofias Memory also supports explicit Agent↔Skill and Agent↔Session associations.

An Agent profile is management state. Sofias Memory does not run the agent, select a provider/model for it, or own its external runtime session.

---

## 18. Runs

Source-backed Knowledge Memory mutations use durable `PipelineRun`s.

Runs make asynchronous operations observable and provide retry/cancel behavior.

Cognitive Memory Create, Supersede, and Forget are intentionally synchronous and do **not** use PipelineRun.

This distinction prevents a simple first-class memory mutation from being forced through the heavier source-processing pipeline.

---

## 19. Forget in the two memory planes

There are two different concepts named Forget because they operate on different memory planes.

### Knowledge Forget

The legacy Knowledge Memory `/api/v1/forget` removes source-backed memory by its supported knowledge scopes.

### Cognitive Forget

```text
POST /api/v1/memories/{memory_id}/forget
```

forgets one exact Cognitive Memory destructively.

The two APIs are intentionally separate and should not be treated as aliases.

---

## 20. Authentication and tenancy boundary

Sofias Memory is currently single-user by design.

Private API routes use one static:

```text
X-API-Key
```

Datasets and Cognitive scopes are organizational/retrieval boundaries, not security tenants.

The project intentionally does not currently provide:

```text
user accounts
organizations
RBAC
ACLs
generic multi-tenancy
```

Do not build an integration that assumes those concepts exist inside Sofias Memory.

---

## 21. Capability negotiation

Call:

```text
GET /api/v1/info
```

instead of guessing feature support from the application version.

Cognitive Memory contract `1` advertises the implemented capabilities explicitly:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

This gives external integrations a stable machine-readable compatibility boundary.

---

## 22. Recommended mental model

When deciding where something belongs, ask:

| Question | Use |
|---|---|
| Is this source-backed material that should preserve document provenance? | Knowledge Memory |
| Is this a durable fact, preference, decision, or persistent semantic context? | Cognitive Memory |
| Is this temporary/temporal interaction context? | Session |
| Is this an executable procedure another system may choose to use? | Skill |
| Is this durable configuration/identity for an external agent? | Agent profile |
| Is this a long-running source-processing operation? | PipelineRun |

Keeping these boundaries explicit produces cleaner integrations and prevents accidental coupling between unrelated lifecycle models.

---

## Go deeper

- [Getting Started](Getting-Started)
- [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
- [Architecture Decision Records](https://github.com/kallbuloso/sofias_memory/tree/main/docs/adr)
- [Product contracts](https://github.com/kallbuloso/sofias_memory/tree/main/docs/product)

This Wiki explains the product for users and integrators. Versioned ADRs, product contracts, and operational documents in the repository remain the engineering source of truth.
