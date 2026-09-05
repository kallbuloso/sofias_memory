# ADR-0012: First-Class Durable Sessions

## Status

accepted

## Context

The original Sofias Memory product baseline intentionally treated sessions as lightweight correlation metadata only. A caller-supplied `session_id` could accompany operations, but there was no first-class Session resource, no durable contextual history, no session lifecycle, no session-aware query provenance, and no separate session synchronization or cache.

That constraint was appropriate for the original single-user memory MVP.

Sofias Memory now needs to serve agentic runtimes such as Sofia's Assistant, where durable temporal context, interaction history, retrieval provenance, and future learning workflows must be correlated without turning the memory service itself into an agent runtime.

The existing implementation already exposes partial session semantics:

- Remember accepts a caller-supplied `session_id`;
- Recall accepts a `session_id` for correlation;
- historical metadata may contain session identifiers;
- `Query` already persists Recall audit and provenance;
- `PipelineRun` already persists durable operational history.

Keeping `session_id` as metadata-only would leave those concepts disconnected and would make future Agent and learning features depend on ad-hoc metadata.

At the same time, copying Cognee's session cache, TTL, synchronization, embedding, or background-persistence architecture would introduce complexity that Sofias Memory does not currently require.

## Decision

Sofias Memory will introduce **first-class durable Sessions** beginning in v0.3.0.

### Session semantics

A Session is a persistent **temporal context boundary**.

It is not:

- permanent semantic memory;
- a Dataset;
- an authentication or authorization principal;
- an agent runtime;
- a cache;
- a TTL-managed object;
- a Neo4j graph entity.

Session state is authoritative in PostgreSQL.

A Session may span multiple Datasets over its lifetime. Dataset scope remains attached to the individual operation that uses a Dataset.

### Identity

Each Session has:

- an internal UUID primary key;
- one globally unique, immutable, case-sensitive external key supplied by the caller.

The public API exposes these concepts as:

- `session_uuid`: structural UUID identity;
- `session_id`: caller-facing external key.

Existing callers may continue supplying textual `session_id` values.

The external key is normalized by one shared validation rule across all Session-aware entry points and is limited to 255 characters.

### Session entries

Durable contextual history is represented by `SessionEntry`.

A SessionEntry is an append-only contextual record containing at least:

- Session reference;
- role label;
- textual content;
- metadata;
- creation timestamp.

`role` is open-ended textual metadata. It does not map automatically to privileged LLM provider roles and cannot create system-level instructions.

SessionEntry is contextual history, not permanent semantic memory.

### Lifecycle

The v0.3.0 Session lifecycle contains only:

```text
active <-> archived
```

Archive acts as an **admission barrier**:

- new contextual activity is rejected;
- already-admitted work is allowed to finish;
- existing PipelineRun retries remain allowed;
- reads remain allowed;
- archive never deletes history or permanent memory.

There is no public hard-delete or purge operation in v0.3.0.

### Recall integration

`Query` gains an optional Session relationship.

Recall without a Session retains its previous behavior.

Recall with a Session associates the resulting Query with that Session.

Session context injection is explicitly opt-in and initially applies only to RAG answer generation. It does not change retrieval, rewrite the query, perform coreference resolution, or semantically search Session history.

When SessionEntries are injected into RAG generation, the Query must durably record the exact ordered SessionEntry identifiers that were used.

Only complete SessionEntries may be injected. Individual entries are not silently truncated.

This preserves query provenance across both:

- retrieved long-term knowledge;
- temporal Session context.

### Pipeline integration

`PipelineRun` gains an optional Session relationship.

For Session-aware write operations such as Remember, Session resolution or creation occurs inside the durable PostgreSQL submission transaction together with the PipelineRun and PipelineSteps.

The shared pipeline submission target resolution therefore expands conceptually from:

```text
dataset
source
```

to:

```text
dataset
source
session
```

when applicable.

A PipelineRun retry preserves the Session associated with the original run.

The Session relationship belongs to the **operation**, not to the resulting Source. A deduplicated Source may be used by multiple Sessions and therefore must not acquire Session ownership.

### Delete policy

The relationship policies are:

```text
Session -> SessionEntry
ON DELETE CASCADE
```

because SessionEntry has no independent semantic value without its Session.

```text
Session -> Query
ON DELETE SET NULL
```

and:

```text
Session -> PipelineRun
ON DELETE SET NULL
```

because query and pipeline audit records remain independently meaningful.

The v0.3.0 public API does not expose Session hard deletion despite these database policies.

### Forget and Dataset deletion

Forget operates on semantic memory and derived knowledge.

It does not remove:

- Sessions;
- SessionEntries;
- Query audit;
- Feedback;
- PipelineRun audit.

`DELETE EVERYTHING` continues to mean all memory managed by the Forget workflow, not all contextual or audit records in the Sofias Memory instance.

Administrative Dataset deletion also does not delete Sessions or Session history.

### Legacy data

No heuristic backfill of first-class Sessions will be performed from historical:

- `Document.metadata`;
- `MemoryEntry.session_id`;
- `PipelineRun.input`;
- other metadata containing session-like identifiers.

First-class Session history begins with v0.3.0.

Historical correlation metadata remains historical and is not reinterpreted as complete Session provenance.

### Storage and projection

Sessions and SessionEntries exist only in PostgreSQL.

They are not projected through `graph_outbox` and do not create Neo4j nodes or relationships.

Neo4j remains a rebuildable projection of the semantic knowledge graph, not a general-purpose representation of operational or contextual state.

### Explicit exclusions

v0.3.0 does not introduce:

- Redis or another Session cache;
- TTL or automatic expiration;
- Session embeddings;
- semantic Session search;
- automatic summarization or compaction;
- automatic Session-to-memory promotion;
- automatic Query-to-SessionEntry creation;
- query rewriting from Session history;
- Session projection to Neo4j;
- users, tenants, ownership, ACLs, or per-Session credentials;
- Agent or Skill runtime behavior.

Any of these capabilities requires separate product scope and, where architectural boundaries change, a follow-up ADR.

## Consequences

Sofias Memory gains a durable contextual layer that can support conversational applications, workflows, and future agent identities without becoming an agent runtime.

`Query` remains the authoritative audit of Recall operations rather than being overloaded as a conversational turn.

`SessionEntry` provides explicit temporal context while Remember continues to represent deliberate long-term memory creation.

Future Agent Management can associate Agents with Sessions without redesigning query or pipeline provenance.

Future learning or promotion workflows can explicitly transform Session context into permanent memory without making every interaction automatically durable knowledge.

The schema gains new Session tables and nullable relationships from Query and PipelineRun, but no new Neo4j projection responsibilities or external infrastructure dependency.

The original metadata-only Session baseline is intentionally amended beginning with v0.3.0.

## Alternatives Rejected

- **Keep `session_id` as metadata only.** Rejected because it cannot provide durable lifecycle, contextual history, or reliable provenance for agentic consumers.

- **Treat Query as the conversational turn model.** Rejected because a Recall Query is an internal memory operation and may not correspond to the final interaction presented by an external agent runtime.

- **Store Session context as MemoryEntry.** Rejected because MemoryEntry is Dataset-scoped semantic memory, while a Session is contextual and may span multiple Datasets.

- **Attach Session directly to Source.** Rejected because Source deduplication allows the same durable knowledge object to participate in multiple Sessions.

- **Automatically inject Session history whenever `session_id` is present.** Rejected because it would silently change the semantics of existing callers.

- **Use Session context to rewrite retrieval queries in v0.3.0.** Rejected to preserve current Recall retrieval behavior and keep contextual generation auditable and bounded.

- **Copy Cognee's cache, TTL, embedding, and synchronization architecture.** Rejected because those mechanisms add operational complexity without being required for the approved v0.3.0 semantics.

- **Project Sessions into Neo4j.** Rejected because Session state is contextual/operational rather than semantic graph knowledge.

- **Backfill Sessions from legacy metadata.** Rejected because historical data cannot reconstruct complete Session history reliably and would create false provenance.

- **Make Session an authentication or authorization boundary.** Rejected because Sofias Memory retains its single static application access key and Sessions represent context, not security identity.

## References

- v0.3.0 Sessions Feature Contract.
- `docs/product/Sofias_Memory_PRD_SPECS.md` — original metadata-only Session baseline.
- `docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md`.
- `docs/adr/0003-single-static-api-key.md`.
- `docs/adr/0007-postgresql-enums-fk-delete-policies.md`.
- `docs/adr/0009-worker-queue-and-pipeline-lifecycle-contract.md`.