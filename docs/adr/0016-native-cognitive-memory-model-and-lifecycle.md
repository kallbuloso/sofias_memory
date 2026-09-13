# ADR-0016: Native Cognitive Memory Model and Lifecycle

## Status

accepted

## Context

Sofias Memory already provides strong first-class domains for imported knowledge (`Dataset` / `Source` / `Document` / `Chunk`), temporal contextual history (`Session` / `SessionEntry`, ADR-0012), procedural knowledge (`Skill`, ADR-0013), durable Agent Profile management (ADR-0014), and durable pipeline execution. It also preserves PostgreSQL as the authoritative store with Neo4j as a rebuildable projection (ADR-0002), and since v0.6.0 it can bring an eligible PostgreSQL schema to Alembic head automatically through the serialized, fail-closed startup mechanism defined by ADR-0015.

The Sofia's Assistant Slice 05 / Gate I6 discovery found a domain gap: none of the existing resources correctly represents small, durable, typed cognitive facts such as a user preference ("prefers teal interfaces"), a durable semantic fact, or a decision that should be recalled independently of document ingestion and independently of temporal Session history.

Using the existing surfaces would require one of several semantic distortions:

- fake `Source` / `Document` / `Chunk` rows for facts that are not documents;
- `SessionEntry` as permanent semantic memory, contradicting ADR-0012's explicit "contextual history, not permanent semantic memory" boundary;
- uncontracted JSON metadata as the primary domain model;
- artificial Datasets used as pseudo-personal-memory namespaces;
- routing a small synchronous write through `PipelineRun` merely because durable pipelines already exist.

These workarounds would move domain ownership into incidental infrastructure and make precise Forget, provenance, temporal truth, supersession, compatibility negotiation, and cross-repository integration ambiguous.

The discovery therefore concluded: **a native Cognitive Memory domain contract is required before Sofia's Assistant implements its Cognitive Memory MVP.**

## Decision

Sofias Memory will introduce a first-class **Native Cognitive Memory** domain beginning in v0.7.0.

### 1. MemoryItem is a native aggregate

`MemoryItem` is not represented as Dataset, Source, Document, Chunk, SessionEntry, Skill, Agent, Query, or PipelineRun.

Its authoritative identity is a server-generated UUID (`memory_id`). Content hashes are never identity and never trigger automatic deduplication or upsert.

The aggregate is intentionally small and mutation-restricted: cognitive content and semantic fields are immutable after creation. Corrections happen through explicit supersession; removal happens through Forget.

### 2. v0.7 types are PROFILE and SEMANTIC only

The first contract supports:

- `PROFILE` — durable preferences, relevant personal characteristics, declared settings/preferences;
- `SEMANTIC` — durable facts, knowledge, decisions, concepts, and semantically useful information.

`EPISODIC` and `PROCEDURAL` are deferred. Existing Skills are not reclassified as Cognitive Memory.

### 3. Candidate and confirmation remain outside Memory

`MemoryCandidate` belongs to Sofia's Assistant (or another caller's equivalent orchestration domain). Pending/confirmation/approval/rejection state is never stored by Sofias Memory.

The Memory API accepts an already-authorized write from its caller. It may persist an opaque `confirmation_ref` as provenance evidence after confirmation occurred, but never owns confirmation UX, PermissionGrant, Policy, or authority decisions.

### 4. Scope is native and deliberately small

Cognitive Memory is not Dataset-scoped.

v0.7 supports exactly:

```text
global
project:<canonical-key>
```

Scope matching is exact. There is no wildcard, hierarchical inheritance, generic tenant, user ACL, or role model.

This gives Sofia's Assistant the two concrete namespaces it currently requires without smuggling tenancy into the product.

### 5. PostgreSQL remains authoritative; pgvector is sufficient for v0.7

The planned authoritative relational shape is deliberately explicit:

```text
memory_items
    one row per stable MemoryItem identity/lifecycle

memory_provenance
    one-to-one with memory_items; structured origin/external refs

cognitive_memory_idempotency
    synchronous mutation replay/conflict evidence; never a PipelineRun
```

These names are part of the architecture draft and may only change before ADR acceptance if a concrete repository naming conflict is found. `memory_type`, `lifecycle`, and `origin_kind` should use PostgreSQL-enforced enum/check semantics consistent with ADR-0007; scope remains validated text because `project:<key>` is data, not an enum value.

`memory_items.embedding` is nullable specifically because FORGOTTEN must physically lose its vector. Database constraints/checks must make the lifecycle invariants structurally testable: non-forgotten items require content/scope/embedding; SUPERSEDED requires `superseded_at` + `superseded_by`; FORGOTTEN requires `forgotten_at` and forbids cognitive payload columns. The self-reference must prevent `superseded_by == id`, and a replacement may have at most one direct predecessor so the lineage remains linear.

Following ADR-0002, PostgreSQL is the sole source of truth for:

- MemoryItem identity and content;
- lifecycle and temporal fields;
- cognitive provenance;
- supersession lineage;
- authoritative embedding;
- idempotency evidence.

Cognitive embeddings follow ADR-0006's authoritative `VECTOR(3072)` and cosine metric.

v0.7 typed recall uses exact cosine similarity over the authoritative full-precision vector after structural/temporal filtering. It does not require a new ANN/halfvec index in this release. ANN can be added later if measured scale requires it without changing MemoryItem identity or lifecycle semantics.

### 6. Neo4j is not part of Native Cognitive Memory v0.7

No `MemoryItem` node or Cognitive Memory relationship is projected to Neo4j in v0.7. Consequently create, supersede and Forget do not emit Cognitive Memory `graph_outbox` events.

This is a deliberate MVP boundary, not an exemption from ADR-0002. If Cognitive Memory is projected later, PostgreSQL must remain authoritative and projection writes/deletes must use the transactional outbox and remain rebuildable.

### 7. First-class provenance is one-to-one for the full MemoryItem lifecycle

Every MemoryItem has exactly one `memory_provenance` row. For a non-forgotten item it contains:

- `origin_kind`;
- `source_system`;
- optional external conversation/turn/task UUIDs;
- optional `confirmation_ref`;
- optional `source_ref`;
- optional `observed_at`.

The supported origin kinds are USER_ASSERTED, TOOL_OBSERVED, IMPORTED, INFERRED and ASSISTANT_GENERATED.

External UUIDs/refs are not cross-database foreign keys and never imply that Sofias Memory owns Conversation, Turn, Task, or confirmation state.

Provider Session ID is explicitly excluded.

`source_system` is only a canonical system slug such as `sofias-assistant`; it is never a resource-instance reference. Conversation, Turn, Task, import/tool source and confirmation identities belong only in their dedicated external-reference fields.

### 8. Confidence is first-class; importance is deferred

`confidence` is a nullable caller-supplied value in `[0,1]`, immutable after create and required for `INFERRED` origin. It expresses epistemic/extraction confidence, not authority, and does not affect v0.7 ranking.

`importance` is deferred. Adding a number before a stable ranking/decay/consolidation policy exists would create central domain metadata with undefined effect.

### 9. Temporal truth is explicit but not a scheduler

MemoryItem may carry immutable caller-supplied `valid_from` and `valid_until` timestamps. `valid_from` is inclusive, `valid_until` exclusive.

Lifecycle is not automatically changed when a validity timestamp passes. `ACTIVE` therefore means "not superseded/forgotten", not "necessarily current at wall clock now".

Current truth at reference time T requires:

- the item has cognitive content (not FORGOTTEN);
- `created_at <= T`;
- it had not yet been superseded at T;
- T is inside its optional semantic validity interval.

This is a deliberately limited system-time + valid-time contract, not a general bitemporal database architecture.

Historical recall is evaluated against those timestamps, **not against the item's present lifecycle label alone**. With `as_of=T` and `include_superseded=false`, an item that is `SUPERSEDED` today remains eligible when it was current truth at T (`created_at <= T < superseded_at`, plus semantic-validity predicates). Implementations must not pre-filter such queries with `lifecycle = ACTIVE`; `FORGOTTEN` remains ineligible at every `as_of` because its content no longer exists.

### 10. Lifecycle is ACTIVE -> SUPERSEDED/FORGOTTEN

The v0.7 state machine is:

```text
ACTIVE -> SUPERSEDED
ACTIVE -> FORGOTTEN
SUPERSEDED -> FORGOTTEN
FORGOTTEN -> FORGOTTEN (resource-state idempotent no-op, including a new idempotency key)
```

No restore/unforget exists.

`SUPERSEDED` retains content/provenance for historical inspection and may participate in explicitly historical recall.

`FORGOTTEN` does not retain recoverable cognitive content.

### 11. Write is synchronous and is not PipelineRun

Create and supersede are synchronous request/response operations.

The required external embedding call happens **before** the short authoritative PostgreSQL transaction. No PostgreSQL transaction remains open across the external provider call.

Create ordering:

```text
validate/canonicalize
committed-outcome idempotency pre-check (optimization only)
embedding call
short PostgreSQL transaction
    authoritative idempotency claim/check (UNIQUE + locking/serialization)
    winner mutates; loser resolves/replays winner or conflicts
commit
response
```

The pre-check is not a correctness boundary. After embedding, the transaction performs the authoritative claim/check. A concurrent loser of the same key waits for/reads the committed winner: same keyed request digest replays the winner; a different digest is `IDEMPOTENCY_CONFLICT`. No resource mutation happens before that claim is resolved.

Supersede uses the same external-before-transaction ordering. Its authoritative idempotency claim/check occurs **before** checking the old row's lifecycle, then the winner locks the old row and atomically creates the replacement + transitions the old item.

No PipelineRun/PipelineStep is created. A durable worker is not required for a small write whose only required external operation is embedding.

### 12. Supersession is a linear, atomic lineage operation

`POST /memories/{id}/supersede` requires the target to be ACTIVE.

The replacement inherits the old item's `memory_type` and `scope`; those identity-of-meaning fields cannot be silently changed as part of the same lineage transition.

Inside one PostgreSQL transaction the implementation:

1. performs the authoritative Idempotency-Key claim/check under a database `UNIQUE` guarantee;
2. if this request lost that claim, resolves the committed winner **before any lifecycle precondition** and either replays same-request outcome or returns `IDEMPOTENCY_CONFLICT`;
3. only for the claim winner, row-locks the old item;
4. re-checks `ACTIVE`;
5. creates one replacement ACTIVE;
6. transitions old -> SUPERSEDED;
7. sets `superseded_at` and `superseded_by`;
8. records the idempotent outcome;
9. commits.

Concurrent supersessions of one old item cannot both succeed. Same-operation replay has precedence over a state conflict created by its own winner: a retry using the same key/request replays the winner even though the winner already changed the target to `SUPERSEDED`. A genuinely distinct second operation with a new key observes a non-ACTIVE target and returns an explicit state conflict.

No semantic search is used to infer which item should be superseded.

### 13. Precise Forget destroys cognitive material

`POST /memories/{memory_id}/forget` acts only on the exact MemoryItem.

On successful Forget, the authoritative record becomes a tombstone. The implementation removes or makes irrecoverable:

- content;
- embedding;
- scope value/project key;
- confidence;
- validity timestamps;
- external provenance references, including conversation/turn/task refs, confirmation ref, source ref and observed timestamp.

The tombstone may retain only the minimal data required for stable identity, lifecycle evidence, linear supersession evidence and safe idempotency:

- `memory_id`;
- `memory_type`;
- lifecycle/timestamps;
- `superseded_by` where applicable;
- non-body idempotency evidence.

The existing `memory_provenance` row is preserved 1:1 and **scrubbed atomically in the same Forget transaction**. After commit it contains only the original `origin_kind` and `source_system`; `conversation_uuid`, `turn_uuid`, `task_uuid`, `confirmation_ref`, `source_ref` and `observed_at` are all `NULL`. `source_system` remains only a system slug, never a disguised resource reference.

`FORGOTTEN -> FORGOTTEN` is a resource-state idempotent no-op even when the caller supplies a new idempotency key. Normal key semantics still run first: a previously used key replays same-request or conflicts on different-request; a fresh key then observes FORGOTTEN and returns the same tombstone without further destructive mutation.

FORGOTTEN content is never returned by GET/recall and never becomes searchable, including for historical `as_of` queries.

This privacy rule intentionally overrides historical reconstruction.

### 14. Idempotency is synchronous PostgreSQL state, not content dedupe

Cognitive mutation endpoints use the project's established `Idempotency-Key` semantics:

- same key + same normalized semantic request -> replay same logical outcome;
- same key + different request -> `IDEMPOTENCY_CONFLICT`;
- caller keys in reserved `sys:` namespace are rejected.

Because Cognitive Memory is not PipelineRun-backed, its idempotency evidence is stored in a dedicated PostgreSQL cognitive-mutation ledger (exact physical table/name may be finalized by implementation, but the separation from PipelineRun is architectural).

The ledger stores operation identity, a **keyed non-reversible normalized-request digest**, and result identities; it must not store a second copy of raw cognitive content.

The persisted digest must be HMAC-based (for example HMAC-SHA-256) or provide an equivalent security property: a high-entropy server-side secret not stored alongside the digest and resistance to offline dictionary guessing of low-entropy memory content. Raw/unkeyed SHA-256, checksums, and other unkeyed request/content hashes are forbidden. The digest key must remain available/stable for the full lifetime of any retained ledger entry for which idempotent replay is still guaranteed. The digest is internal and never API-visible.

Correctness under a race is database-authoritative: the cognitive idempotency ledger has a `UNIQUE` key claim. A read-only pre-check may avoid an unnecessary embedding call, but after embedding and inside the short PostgreSQL transaction the request must atomically claim/check the key. Only the claim winner mutates domain state. A loser reads the committed winner and replays it on same digest or returns `IDEMPOTENCY_CONFLICT` on different digest. For Supersede this claim/check precedes lifecycle validation, so same-operation replay cannot be misclassified as `MEMORY_STATE_CONFLICT` because its own winner already superseded the row.

Content hash is never used for semantic dedupe or MemoryItem identity.

### 15. Recall is a separate typed API

Existing `POST /api/v1/recall` remains knowledge recall.

Native Cognitive Memory uses a separate endpoint (`POST /api/v1/memories/recall`) so callers can reason about typed lifecycle, scope, provenance and current truth without changing the meaning of the existing knowledge API.

Recall uses required explicit scopes, optional memory types, `top_k`, optional non-future `as_of`, optional `include_superseded`, and optional relevance threshold.

With `include_superseded=false`, eligibility means current truth **at the requested `as_of`**, not present `ACTIVE` lifecycle. A row now SUPERSEDED is included when it was current at T. `include_superseded=true` additionally permits items that had already ceased to be current by T, subject to the temporal validity contract.

FORGOTTEN is structurally ineligible.

Ranking is deterministic: cosine relevance descending, then `created_at` descending, then UUID ascending.

Recall itself is a synchronous read and does not create PipelineRun. v0.7 does not add a separate durable Cognitive Recall audit table; operational recall audit remains a caller responsibility, while MemoryItem provenance remains a Memory responsibility.

### 16. Compatibility is negotiated, not inferred from app SemVer

`GET /api/v1/info` is extended additively with:

- `api_contract_version`;
- a `contracts` map containing `cognitive_memory: "1"` when implemented;
- explicit Cognitive Memory capability strings.

Existing info fields remain unchanged.

A consumer must not infer Cognitive Memory support from `version >= 0.7.0` alone. Missing/unsupported contract/capabilities means Cognitive Memory is unavailable to that consumer, while the rest of the service may remain usable.

### 17. Migration model follows ADR-0015

v0.7 may add one or more Alembic revisions after current head `0017` for Native Cognitive Memory.

The exact decomposition is an implementation/backlog decision, but every revision must satisfy ADR-0015's automatic-migration requirements, including safe resumability from any committed Alembic revision reached before interruption.

There is no automatic backfill from existing Documents, SessionEntries, Skills or Agent Profiles into MemoryItem.

## Consequences

Sofia's Assistant can persist a preference/fact as the domain object it actually is, recall only current/allowed cognitive truth, atomically replace stale truth, and precisely forget one memory without deleting a Source/Dataset or abusing Session history.

The API adds a new small synchronous write path and a new PostgreSQL/pgvector retrieval path. This is simpler than routing tiny cognitive mutations through the durable pipeline subsystem, but it creates dedicated idempotency and concurrency obligations that must be covered directly by database constraints/locking/tests.

The privacy guarantee for FORGOTTEN is stronger than ordinary soft deletion: historical reconstruction intentionally loses cognitive content and external provenance after Forget.

Because v0.7 does not project Cognitive Memory into Neo4j, graph availability cannot block Cognitive Memory create/recall/supersede/forget. A later graph feature must be introduced deliberately through ADR-0002/outbox semantics.

The model does not guarantee global semantic consistency. Two independently-created ACTIVE items may contradict each other until a caller explicitly supersedes or forgets one. This is an explicit non-guarantee, not an implementation bug.

## Alternatives Rejected

- **Fake Source/Document/Chunk per memory.** Rejected because it lies about domain identity, creates ingestion/provenance/deletion artifacts, and couples personal cognitive memory to Dataset semantics.
- **SessionEntry as permanent memory.** Rejected because ADR-0012 explicitly defines it as append-only temporal context, not permanent semantic memory.
- **Metadata-only Cognitive Memory.** Rejected because lifecycle, provenance, scope, temporal truth, constraints and Forget must be structurally enforceable rather than conventions hidden inside JSON.
- **Artificial per-user/preference Dataset.** Rejected because Dataset is a knowledge-corpus namespace, not a Cognitive Memory scope or authorization boundary.
- **PipelineRun for every Cognitive Memory write.** Rejected because it adds worker/durable-stage machinery to a small synchronous mutation with no multi-stage durable processing requirement.
- **Neo4j-first or Neo4j-required recall.** Rejected by ADR-0002 and because pgvector/PostgreSQL fully satisfies the v0.7 retrieval requirement.
- **Soft Forget (`lifecycle=FORGOTTEN` while retaining content/embedding).** Rejected because it violates the user-facing meaning of Forget and leaves sensitive content searchable/recoverable.
- **In-place PATCH of memory content.** Rejected because it destroys the exact historical boundary supersession is intended to preserve.
- **Semantic dedupe/content-hash upsert.** Rejected because similarity/equality of propositions is not identity and cannot safely decide caller intent.
- **Automatic contradiction resolution/consolidation AI.** Rejected as deferred product behavior with material semantic consequences.
- **Generic tenant/user ACL model.** Rejected because the concrete consumer only requires global/project scope and Sofias Memory retains its current single-static-key security model (ADR-0003).
- **EPISODIC/PROCEDURAL in the first release.** Rejected to avoid inflating v0.7 before PROFILE/SEMANTIC lifecycle is proven.
- **Importance score without a concrete policy.** Rejected because a central number with no frozen effect is ambiguous metadata, not a domain contract.

## Deferred Decisions

- EPISODIC memory model;
- procedural memory beyond existing Skills;
- ANN/HNSW for Cognitive Memory scale;
- Cognitive Memory Neo4j projection;
- advanced consolidation/decay/importance;
- semantic contradiction detection/resolution;
- durable Cognitive Recall audit inside Memory;
- hard deletion/retention window for tombstones;
- multi-user ACL/tenancy;
- offline synchronization.

## References

- `docs/product/Sofias_Memory_Feature_Contract_v0.7.0_Native_Cognitive_Memory.md`.
- `docs/product/Sofias_Memory_Integration_Contract_Sofias_Assistant_v1.md`.
- ADR-0002 — PostgreSQL Source of Truth and Neo4j Rebuildable Projection.
- ADR-0003 — Single Static API Key.
- ADR-0006 — pgvector 3072-Dimension Storage and Halfvec ANN.
- ADR-0007 — PostgreSQL enums/FK/delete policies.
- ADR-0008 — Neo4j projection and rebuild contract.
- ADR-0009 — worker queue / pipeline lifecycle and idempotency precedent.
- ADR-0012 — First-Class Durable Sessions.
- ADR-0013 — First-Class Durable Procedural Skills.
- ADR-0014 — First-Class Durable Agent Management.
- ADR-0015 — Automatic Serialized Migration Bootstrap.
