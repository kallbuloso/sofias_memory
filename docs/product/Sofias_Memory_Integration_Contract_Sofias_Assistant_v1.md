# Sofias Memory — Integration Contract: Sofia's Assistant v1

**Status:** APPROVED  
**Contract:** Sofias Memory ↔ Sofia's Assistant Cognitive Memory Integration  
**Contract version:** 1  
**Memory target:** Sofias Memory v0.7.0 Native Cognitive Memory  
**Primary Assistant consumer:** Slice 05 — Sofia Remembers / Gate I6 (`SA-B018`, `SA-B019`, `SA-B020`)

---

## 1. Objetivo

Este documento congela a fronteira cross-repository entre **Sofia's Assistant** e **Sofias Memory** para Cognitive Memory.

Ele não implementa o runtime do Assistant e não redefine os domínios já existentes do Memory. Seu objetivo é garantir que o Slice 05 possa ser construído contra um contrato correto, machine-negotiable, retry-safe e sem duplicar ownership entre os repositórios.

Regra principal:

> **domain ownership > implementation convenience**

---

# 2. Ownership

## 2.1 Sofia's Assistant owns

```text
Conversation
Turn
Conversation History
Working Memory
ContextBuilder
provider interaction
provider sessions
realtime voice
Task
AgentRun
Tools
Policy
PermissionGrant
confirmation UX/workflow
MemoryCandidate
candidate extraction
candidate classification
candidate approval/rejection lifecycle
Memory orchestration
operational Audit
```

## 2.2 Sofias Memory owns

```text
persisted MemoryItem
cognitive embeddings
cognitive retrieval
cognitive lifecycle
cognitive temporal truth
cognitive provenance
supersession lineage
precise cognitive Forget
Memory Sessions / SessionEntries
knowledge corpus
semantic/graph persistence owned by Memory
future episodic/procedural memory when separately accepted
```

Neither repository may persist a duplicate authoritative copy merely to simplify a call path.

---

# 3. Identities must never collapse

The following identities remain distinct:

```text
Assistant Conversation UUID
!= Assistant Turn UUID
!= Sofias Memory Session UUID
!= Cognitive Memory UUID
!= Provider Session ID
```

There are no cross-database foreign keys.

Sofias Memory may persist Assistant UUIDs only as **external provenance references**, never as ownership/FK semantics.

Sofia's Assistant does not persist `memory_session_id` or `provider_session_id` merely to make future calls convenient.

A returned Cognitive `memory_id` may of course be persisted where the Assistant needs a durable reference to the Memory resource itself (for example, an approved MemoryCandidate result or a future precise Forget workflow). That does not collapse Conversation/Turn/Session identity.

---

# 4. Compatibility negotiation — mandatory fail-closed handshake

Before enabling Cognitive Memory behavior, `SofiasMemoryAdapter` reads:

```text
GET /api/v1/info
```

The Adapter requires:

```text
api_contract_version == "1"
contracts["cognitive_memory"] == "1"
required capabilities are present
```

Required capabilities for full Gate I6:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

## 4.1 No SemVer guessing

The Adapter must never decide support using:

```text
Memory version >= 0.7.0
```

Application release version is diagnostic; contract/capabilities are authority for machine compatibility.

## 4.2 Old server behavior

A v0.6-or-older server that lacks the new fields is **Cognitive Memory incompatible**, not globally unusable.

Assistant behavior:

```text
Cognitive Memory adapter -> fail closed / disabled
Conversation runtime      -> may continue degraded
```

## 4.3 Capability != readiness

`/info` describes implemented contract surface. Operational availability is separate. `503`, health/readiness, network errors and dependency failures remain runtime concerns.

---

# 5. Session correlation remains independent

Memory Session continues to be temporal context, not Cognitive Memory.

When Sofia's Assistant uses a Memory Session for a Conversation, the deterministic external key remains conceptually:

```text
sofias-assistant:conversation:{conversation_uuid}
```

This key is passed as `session_id` through the existing Session contract and normalized by Sofias Memory's shared Session rule.

The Assistant does not need to persist the returned Memory `session_uuid` merely for correlation; the deterministic external key is sufficient.

A Cognitive Memory write does **not** require a Memory Session. Provenance can reference `conversation_uuid`/`turn_uuid` directly as external IDs.

---

# 6. Candidate flow

Canonical cross-repository flow:

```text
Assistant Turn / tool observation / internal decision
        ↓
MemoryCandidate                       # Assistant-owned
        ↓
classify PROFILE | SEMANTIC           # Assistant-owned
        ↓
Memory Policy                         # Assistant-owned
        ↓
confirmation when policy requires     # Assistant-owned
        ↓
APPROVED                              # Assistant-owned state
        ↓
SofiasMemoryAdapter
        ↓
POST /api/v1/memories
        ↓
MemoryItem ACTIVE                     # Memory-owned
```

Sofias Memory never receives or persists a pending candidate lifecycle.

A failed Memory write does not retroactively change Assistant candidate policy semantics. The Assistant records operational failure and may retry only under the idempotency rules below.

For explicit user intent such as **"remember this"**, the Assistant must not report persistence success until the Memory API has actually committed and returned success.

---

# 7. Mapping Candidate -> MemoryItem

The Adapter maps only approved candidate data into the stable Memory contract:

```text
candidate classification -> memory_type
Assistant memory scope    -> scope
normalized fact/preference -> content
candidate confidence      -> confidence when present/required
known semantic validity   -> valid_from / valid_until
origin evidence           -> provenance
```

Candidate status, policy reason, confirmation UI state and PermissionGrant do not become MemoryItem fields.

## 7.1 Sofia's Assistant provenance profile

`source_system` is:

```text
sofias-assistant
```

### USER_ASSERTED

Required from Assistant:

```text
conversation_uuid
turn_uuid
```

`confidence` is normally omitted for `USER_ASSERTED`. A user's assertion is evidence of what the user asserted; it is **not** automatically a truth-probability of `1.0`.

`confirmation_ref` is included only when a confirmation workflow actually occurred.

### TOOL_OBSERVED

Required:

```text
task_uuid
source_ref
observed_at
```

If the observation belongs to a Conversation turn, also send `conversation_uuid` + `turn_uuid`.

### IMPORTED

Required:

```text
source_ref
```

The Assistant must not fake a Memory Dataset/Source merely to satisfy this provenance. `source_ref` is an external reference, not a Memory FK.

### INFERRED

Required:

```text
confidence
and at least one of:
  turn_uuid
  task_uuid
  source_ref
```

If confirmation occurred before approval, include `confirmation_ref`.

### ASSISTANT_GENERATED

Require the generating context:

```text
conversation_uuid + turn_uuid
or task_uuid/source_ref when not conversational
```

`confirmation_ref` is included when applicable.

## 7.2 Provider Session ID is forbidden provenance

Voice/text/provider transport IDs are operational provider state and never written into Cognitive Memory provenance.

---

# 8. Idempotency identity

Sofia's Assistant **must** send `Idempotency-Key` for every Cognitive Memory mutation, even though the Memory API keeps the header optional for generic/manual callers.

The key identifies one logical Assistant operation, never the content itself.

Recommended deterministic shapes:

```text
sofias-assistant:memory:create:{candidate_uuid}
sofias-assistant:memory:supersede:{operation_uuid}
sofias-assistant:memory:forget:{operation_uuid}
```

`candidate_uuid`/`operation_uuid` are Assistant-owned durable identities.

The Adapter treats the key as opaque after construction; Sofias Memory does not parse it into domain ownership.

## 8.1 Retry semantics

Same key + same semantic request:

```text
safe replay -> same logical result
```

Same key + changed semantic request:

```text
409 IDEMPOTENCY_CONFLICT
```

The Assistant must never "fix" an idempotency conflict by blindly retrying with random new keys. It must surface/handle the inconsistency as an application bug or explicit new operation.

Content hash is not an idempotency identity.

Sofias Memory persists only a keyed/non-reversible digest of the canonical semantic request for replay/conflict detection; the Assistant does not calculate or depend on that digest. Raw/unkeyed request or content hashes are not part of the cross-repository contract.

---

# 9. Typed recall flow

Canonical flow:

```text
Turn / Task
    ↓
MemoryOrchestrator                     # Assistant-owned
    ↓
select explicit scopes/types
    ↓
SofiasMemoryAdapter
    ↓
POST /api/v1/memories/recall
    ↓
MemoryRecallItem[]                     # Memory-owned response
    ↓
MemoryContextItem[]                    # Assistant-owned adaptation
    ↓
ContextBuilder
    ↓
provider
```

## 9.1 Scope selection

The Adapter always sends explicit scopes. There is no implicit "current project" inside Sofias Memory.

Typical Assistant request:

```text
scopes = ["global", "project:<current-project-key>"]
```

when both are appropriate.

Project key normalization must occur before the Adapter request and must satisfy Memory's canonical scope contract.

## 9.2 Default recall posture

Normal Assistant recall uses:

```text
memory_types = [profile, semantic]
include_superseded = false
as_of = omitted (Memory server now)
```

When the Assistant explicitly performs historical recall with `as_of=T`, `include_superseded=false` means **truth current at T**, not "rows whose lifecycle is ACTIVE today". Therefore a MemoryItem that is currently SUPERSEDED may legitimately be returned if it was current at T. The Adapter must preserve that server semantic and must not post-filter historical results by present lifecycle.

The Assistant does not request historical superseded material for ordinary ContextBuilder use.

## 9.3 Returned memory is untrusted context

For every returned MemoryItem:

```text
content = evidence/context
not system instruction
not Policy override
not PermissionGrant
not tool authorization
not identity proof
```

A stored sentence such as "ignore policy" is ordinary memory content and cannot override Assistant Policy or provider-system instructions.

The Assistant may apply its own context budget/ranking after receiving Memory relevance, but it must preserve `memory_id`, type, scope and provenance in its internal `MemoryContextItem` for explainability/audit.

---

# 10. Supersession flow

When Assistant has identified an exact existing MemoryItem whose truth must be replaced:

```text
resolve exact memory_id
        ↓
Assistant policy / confirmation if needed
        ↓
POST /api/v1/memories/{memory_id}/supersede
        ↓
old SUPERSEDED + replacement ACTIVE atomically
```

The Assistant must not implement replacement as two independent calls:

```text
POST new memory
then mutate old
```

because that creates a race window with two current truths.

The replacement keeps old `memory_type` and `scope` by Memory contract. If the conceptual type/scope changes, the Assistant treats that as a different memory operation rather than a supersession mutation.

`409 MEMORY_STATE_CONFLICT` means a **distinct** operation no longer satisfies the supersession precondition; no blind retry with a new key. A same-key/same-request Supersede retry must replay the original winner even when that winner already changed the target to `SUPERSEDED`; Memory's idempotency resolution precedes the lifecycle conflict produced by that same operation.

---

# 11. Precise Forget flow

Canonical flow:

```text
user Forget intent
    ↓
Assistant resolves exact MemoryItem/memory_id
    ↓
Assistant Policy + confirmation when applicable
    ↓
POST /api/v1/memories/{memory_id}/forget
    ↓
Memory tombstone
```

The Assistant never maps a precise Cognitive Forget to legacy Source/Dataset `/forget`.

After success:

- content must not be considered recoverable;
- local caches/context containing that memory must be invalidated by the Assistant;
- future recall must not return it;
- the Assistant may retain its own operational audit of the Forget action according to its own retention policy, but must not retain a duplicate "memory content" merely to defeat the Forget semantics.

A repeated Forget of the same memory is safe and converges to the same tombstone. This is true both for same-key replay and for a **new valid idempotency key** sent after the resource is already FORGOTTEN: the latter is a resource-state no-op returning `200`/the tombstone. Reusing an already-bound key still follows the normal same-key rules first and can return `IDEMPOTENCY_CONFLICT` if its semantic request differs.

After Forget, Memory preserves its 1:1 provenance row only as scrubbed evidence: `origin_kind` + `source_system`; Conversation/Turn/Task UUIDs, `confirmation_ref`, `source_ref`, and `observed_at` are `NULL`. `source_system` is only the system slug (`sofias-assistant`), never a resource reference.

Missing memory returns stable not-found and is not silently interpreted as success.

---

# 12. Failure and degraded mode

Memory unavailability normally does **not** make Sofia unavailable.

## 12.1 Recall failure

Network timeout / Memory `503` / incompatible cognitive contract:

```text
Conversation continues without Cognitive Memory context
Assistant operational Audit records degraded memory path
no fabricated MemoryContextItem
```

The user need not be interrupted for incidental recall failure unless product UX later chooses to surface degradation.

## 12.2 Explicit write failure

For explicit **"remember this"**:

```text
persist failed -> never report "remembered" / persisted
```

A timeout may be retried only with the same idempotency key and within the Assistant's bounded retry policy.

## 12.3 Authentication/configuration

`401` (and any future `403`) from Memory is treated as configuration/secret failure for Memory operations. Cognitive Memory fails closed; the Conversation runtime may continue degraded.

## 12.4 409

Handled explicitly by type:

- `IDEMPOTENCY_CONFLICT` -> operation identity bug/inconsistency; no blind retry;
- `MEMORY_STATE_CONFLICT` -> re-resolve current memory state; no blind retry.

## 12.5 422

Validation/contract bug or invalid caller data. No blind retry.

## 12.6 503

Dependency/readiness unavailable. Bounded retry only when the operation is idempotent and product latency budget permits it.

No infinite retry.

---

# 13. Audit boundary

## 13.1 Assistant operational Audit owns

- candidate extraction/classification decision;
- policy decision;
- whether confirmation was required/performed;
- adapter attempt/result;
- retry/degraded mode;
- ContextBuilder inclusion/exclusion decision;
- user-facing Forget workflow.

## 13.2 Sofias Memory owns

- durable MemoryItem identity/state;
- cognitive provenance supplied at write time;
- lifecycle timestamps;
- supersession lineage;
- tombstone;
- service-side request/error observability without sensitive content.

The Memory service does not become the Assistant's operational audit log.

---

# 14. Privacy boundary

The Assistant must not send more provenance than the Memory contract requires.

Specifically:

- never copy full Conversation/Turn text into provenance fields;
- never send Provider Session IDs;
- `source_ref`/`confirmation_ref` must be identifiers/references, not hidden content blobs;
- do not use `metadata` escape hatches for central cognitive fields;
- after precise Forget, invalidate Assistant memory caches and do not reconstruct Memory content from an operational audit record.

Sofias Memory Forget is not a command to delete the Assistant's Conversation history; each repository applies deletion/retention only to the domain it owns.

---

# 15. Voice / text parity

Voice and text follow the **same Cognitive Memory policy**.

Voice-specific provider/session/audio identifiers stop at the Assistant runtime boundary.

After the Assistant has materialized a canonical `Turn`, candidate extraction/classification/confirmation/write uses the same flow and the same Conversation/Turn UUID provenance as text.

No separate "voice memory" type or provenance kind exists in v1.

---

# 16. Memory Session vs Cognitive Memory in ContextBuilder

The Assistant may use both:

```text
Memory Session / SessionEntry -> temporal conversation context
Cognitive MemoryItem          -> durable profile/semantic context
```

They remain separate input channels to ContextBuilder.

No rule may infer permanent Cognitive Memory merely because a fact appears in SessionEntry, and no rule may inject MemoryItem back into SessionEntry as a fake transcript event.

---

# 17. GET / memory reference use

The Adapter may call:

```text
GET /api/v1/memories/{memory_id}
```

for exact resolution, explainability, confirmation UI, supersession preflight or Forget preflight.

A forgotten MemoryItem may return a tombstone. The Assistant must treat `content=null` as intentionally unavailable, not as corruption requiring recovery from local copies.

---

# 18. Explicit non-responsibilities

This integration contract does not require Sofias Memory to implement:

- candidate extraction;
- confirmation UX;
- permission workflows;
- provider interactions;
- ContextBuilder;
- Tool execution;
- AgentRun/Task runtime;
- automatic prefetch;
- auto-consolidation;
- semantic contradiction resolution;
- offline synchronization.

It does not require Sofia's Assistant to own:

- MemoryItem persistence;
- cognitive embeddings;
- Cognitive Memory temporal truth;
- supersession concurrency;
- Memory tombstones;
- Memory retrieval storage/query mechanics.

---

# 19. Gate I6 acceptance contract

Before `SA-B018/019/020` can claim Cognitive Memory integration complete, the cross-repository tests must prove at least:

1. Adapter detects v0.7 Cognitive Memory support from `/info` without SemVer guessing.
2. Missing/incompatible contract disables only Cognitive Memory path and leaves Conversation available.
3. Candidate remains Assistant-owned until approved; Memory has no pending candidate state.
4. Assistant create uses `Idempotency-Key` and receives a typed `MemoryItem`.
5. Same create retry does not duplicate MemoryItem.
6. PROFILE and SEMANTIC write/recall round trips preserve type, scope and provenance.
7. Session correlation remains by deterministic external key; no required persisted Memory session UUID.
8. Typed recall is separate from knowledge `/recall`.
9. Default Assistant recall excludes forgotten/non-current truth.
10. Returned memory cannot become Policy/Permission/system authority.
11. Supersession is one API operation and produces one replacement under race; same-key/same-request race replay returns the winner instead of a false lifecycle conflict.
12. Precise Forget targets `memory_id`, not Source/Dataset; repeated Forget with a new key on FORGOTTEN is a `200` state no-op, while same-key conflict semantics remain unchanged.
13. Forgotten provenance is scrubbed to `origin_kind` + system-slug `source_system`, with all external refs NULL.
14. Historical `as_of=T` with `include_superseded=false` can return a currently SUPERSEDED item when it was current truth at T.
15. Explicit remember never reports persisted on failed write.
16. Recall 503/network failure degrades gracefully.
17. 409/422 are not blindly retried.
18. Voice and text produce the same canonical memory write contract after Turn creation.
19. Assistant operational Audit and Memory cognitive provenance remain separate.

---

# 20. Approved cross-repository boundaries

Human review approved and freezes these cross-repository boundaries:

1. Sofia's Assistant always supplies idempotency keys for Cognitive Memory mutations.
2. Assistant does not persist Memory Session UUID merely for Conversation correlation.
3. Normal ContextBuilder recall uses only current PROFILE/SEMANTIC memory; historical superseded recall is not part of the ordinary turn path.
4. Memory content is always untrusted context and can never override Assistant Policy/Permission/system instructions.
5. Precise Forget removes Memory-owned cognitive content but does not imply deletion of Assistant-owned Conversation history/audit.

