# ADR-0014: First-Class Durable Agent Management

## Status

accepted

## Context

The original Sofias Memory PRD baseline excluded `agents/skills/proposals` entirely, treating them as Cognee capabilities that belong to a multi-tenant agent platform rather than a single-user memory service. `/agents` has been an explicitly forbidden route prefix since the initial contract.

ADR-0012 introduced first-class durable Sessions and explicitly promised that "future Agent Management can associate Agents with Sessions without redesigning query or pipeline provenance." ADR-0013 introduced first-class durable procedural Skills and explicitly promised that "future Agent Management (v0.5) can associate Agents with Skills via an explicit join, without altering Skill identity, revision numbering, or content immutability." Both ADRs sketch the same forward-looking shape: `Agent Profile ↕ Skill` and `Agent ↕ Session`, with the same forbidden-runtime boundary drawn for both preceding concepts.

Sofias Memory now needs to serve Sofia's Assistant and similar agentic callers that maintain a persistent, nameable identity — an "Agent" — across restarts and across conversations, associate that identity with reusable Skills and with the Memory Sessions it participates in, and administer that identity (create, describe, archive) independently of any single running process. None of this requires Sofias Memory to become the thing that runs an agent loop, selects a model/provider, executes tools, or orchestrates a conversation. Those remain explicitly out of scope, exactly as ADR-0012 and ADR-0013 already drew the same line for Session and Skill.

An architecture discovery pass (read-only, this session, HEAD `a11ec9eddb92b00b75e6e94661df2c2a2d036ee5`) evaluated the material design questions — Agent identity shape, profile mutability vs. revisioning, Agent↔Skill association shape, Agent↔Session cardinality and semantics, Query/PipelineRun impact, lifecycle semantics, Neo4j/outbox boundary — against the two preceding ADRs' precedents and this product's stated invariants. This ADR freezes the outcome of that discovery.

## Decision

Sofias Memory will introduce **first-class durable Agent Management** beginning in v0.5.0.

### PRD amendment: `/agents` narrowed, not reopened

The original PRD's blanket `agents/skills/proposals` exclusion is amended a second time, narrowly. `/agents` becomes permitted **only** for durable Agent Profile management and the two explicit associations (Agent↔Skill, Agent↔Session) defined in the v0.5.0 Feature Contract. `/proposals` remains forbidden, unchanged. Agent runtime, agent execution, tool execution, tool authorization, multi-agent orchestration, and any self-improvement/proposal loop remain forbidden, exactly as before. This is the same shape of amendment ADR-0013 made for `/skills`: a narrow re-opening of one prefix, not a reversal of the PRD's rejection of an agent runtime or a multi-tenant agent platform.

The forbidden-route contract test and `AGENTS.md`/`CLAUDE.md` are amended only when `/agents` is actually implemented (SM-802). Until then, `/agents` correctly remains listed as forbidden — this ADR is the normative permission to remove it later, not an instruction to remove it now.

### An Agent is a durable profile identity, not a runtime

An Agent in Sofias Memory is a durable declarative Agent Profile identity and management resource. It is not an executing agent process. Sofias Memory stores, versions-by-mutation (never by immutable revision), associates, and returns Agent Profiles. It never runs an agent loop, never selects a model or provider, never manages a provider session, never executes or authorizes a tool, and never orchestrates a conversation. The caller — Sofia's Assistant or an equivalent external runtime — owns all of that, exactly the same boundary ADR-0012 drew for Sessions and ADR-0013 drew for Skills.

### Identity: UUID + immutable portable name, not a caller-supplied key

Each Agent has exactly two identities: an internal UUID primary key (`Agent.id`), serialized publicly as `agent_uuid` with no separate duplicating column, and a `name` — a required, globally unique, immutable, portable identifier following the same charset discipline as `Skill.name` (lowercase `a-z0-9-`, 1–64 chars, no leading/trailing/doubled hyphen). This mirrors Skill's identity shape, not Session's: an Agent is a resource Sofias Memory itself originates and names, not a correlation handle for something that already exists externally the way a caller-supplied `session_id` is. Rename is not a supported operation; a new `name` means a new Agent. Duplicate `name` on create is always `409`, never a silent upsert — the same discipline already applied to Session and Skill creation.

### Mutable Agent Profile, deliberately no AgentRevision

Agent Profile fields (`display_name`, `description`, `instructions`, `metadata`) are mutable in place via `PATCH`. There is no `AgentRevision`. This is a deliberate divergence from Skill's immutable-append-only-revision model, not an oversight: Skill needed revisioning because procedural content, once selected by a caller, must never silently change under them, and safe-replay-by-hash matters for import/export determinism. Neither concern applies to Agent Profile — it is small, low-churn administrative configuration with no equivalent "in-flight execution reading a pinned version" invariant.

This choice has an explicit cost, which this ADR names rather than leaves implicit: Sofias Memory cannot reconstruct which historical `display_name`/`description`/`instructions`/`metadata` values were active on an Agent during a past Session, Query, PipelineRun, or external conversation turn. Historical Agent Profile/runtime reproducibility is not a v0.5 guarantee. Any caller needing that guarantee is responsible for its own snapshot, the same way it already owns provider/model/conversation state Sofias Memory never stores.

`instructions` is stored and returned as opaque declarative text. Sofias Memory never interprets it, never injects it into Recall or Session Context, never builds a prompt from it, and never executes it — the same non-interpretation posture ADR-0013 established for Skill `procedure`.

### Lifecycle: discovery/availability filter, not admission barrier

Agent lifecycle is `active <-> archived`. Unlike Session archive (an admission barrier that blocks new contextual activity), Agent archive is a discovery/availability filter: Sofias Memory never executes an Agent, so it has no inbound activity of its own to block. While archived, every management and association operation remains available (`GET`, `PATCH`, associate/update-pin/remove Skill, associate/remove Session, restore). Archive and restore are both idempotent and never touch existing associations. This mirrors the rationale ADR-0013 gave for Skill archive being a discovery filter rather than an admission barrier, for the same underlying reason: Sofias Memory does not control the runtime that would need to be blocked.

There is no public hard delete for Agent, consistent with Session and Skill.

### Agent↔Skill: explicit association with an optional, publicly-integer, database-enforced pin

`agent_skills` is an explicit join table (`agent_id`, `skill_id`, nullable `pinned_revision_id`, `created_at`) with `PRIMARY KEY (agent_id, skill_id)`. A `NULL` pin means the association tracks `Skill.current_revision_id` live; a non-`NULL` pin freezes the association to one specific, named `SkillRevision`. Publicly, a pin is always addressed by the same 1-based monotonic `revision` integer the rest of the Skills API already uses (`pinned_revision`) — the internal `SkillRevision.id` UUID is never exposed, exactly as ADR-0013 §3.3 already established for every other Skill revision reference.

The pin is enforced with a single composite foreign key, `(skill_id, pinned_revision_id) REFERENCES skill_revisions(skill_id, id) ON DELETE RESTRICT`, reusing the candidate key ADR-0013 already created for `Skill.current_revision_id`'s own deferred FK. This makes two failure modes structurally impossible at the database level rather than merely application-validated: a pin can never reference a revision belonging to a different Skill (cross-Skill pinning), and deleting a pinned `SkillRevision` is rejected outright rather than silently converting a deliberately pinned association into follow-current behavior (which `ON DELETE SET NULL` would do). This association does not require, and does not receive, any change to `Skill.id`, `Skill.name`, `SkillRevision`, revision numbering, `Skill.current_revision_id`, or Skill content immutability — the exact non-redesign guarantee ADR-0013 promised.

Skill archive, and Agent archive, both leave `agent_skills` rows untouched — association survives lifecycle changes on either side, discoverable via management reads even when the Skill or the Agent is archived.

### Agent↔Session: a current explicit management association, not provenance

`agent_sessions` is an explicit M:N join table (`agent_id`, `session_id`, `created_at`) with `PRIMARY KEY (agent_id, session_id)` and `ON DELETE CASCADE` on both foreign keys. Its semantics are frozen precisely: it records a **current explicit management association** — that an Agent is (or has been, per its `created_at`) administratively linked to a Session — never historical provenance, an audit event, or per-operation attribution. `DELETE` on this table is permitted and idempotent precisely because it is association state, not an audit log; removing a row destroys only a current-state fact, never a historical record anything else depends on.

This ADR explicitly rejects treating `Query`/`PipelineRun` → `Session` → `agent_sessions` as identifying which Agent originated an operation. `Query.agent_id` and `PipelineRun.agent_id` do not exist in v0.5, and neither table gains any Agent-related column. When a Session has more than one associated Agent — which the M:N cardinality explicitly allows — no inference about which specific Agent produced any given Query, PipelineRun, or SessionEntry is possible from persisted data. This is a structural limitation, named as an explicit non-guarantee, not an implementation gap silently left for a future ticket to "discover" as a bug. Exact per-operation Agent attribution is deferred to a future, separately-scoped requirement if one is ever needed.

This satisfies ADR-0012's own promise without over-claiming: Agent can be associated with Session "without redesigning query or pipeline provenance" precisely because no such redesign is attempted — `Query`/`PipelineRun`'s existing `session_id` relationship (ADR-0012) is untouched, and no new Agent-shaped provenance column is added anywhere.

### PostgreSQL authority, no Neo4j, no outbox

PostgreSQL is the sole authority for `agents`, `agent_skills`, and `agent_sessions`, following ADR-0002. None of the three tables are ever projected to Neo4j: no `graph_outbox` event type is created for any of them, and no `(:Agent)` node or Agent-related relationship type is ever created. Agent state is operational/identity/association state, not semantic graph knowledge — the same reasoning ADR-0012 applied to Session and ADR-0013 applied to Skill.

### No security principal semantics

Sofias Memory retains its single static application access key (ADR-0003) unchanged. An Agent is never a user, tenant, role, security principal, or credential holder. No per-Agent credential, permission, or ACL is introduced. `agent_skills` association never authorizes a Skill's `declared_tools` — that field remains descriptive metadata regardless of which Agents are associated, exactly as ADR-0013 established.

### Forget / Dataset Delete never touch Agent

Forget (source, dataset, or everything) and Dataset Delete never remove `agents`, `agent_skills`, or `agent_sessions` rows. Agent has no `dataset_id`; it is not Dataset-owned, structurally outside the scope of any Forget/Dataset Delete workflow, the same way ADR-0012 and ADR-0013 already placed Session and Skill outside that scope.

## Consequences

Sofias Memory gains a durable Agent identity and management layer that Sofia's Assistant and similar callers can rely on across restarts, associate with reusable Skills (with optional revision pinning) and with the Memory Sessions they participate in, without Sofias Memory becoming an agent runtime, a provider-session manager, or a tool executor.

`/agents` is narrowly reopened for Agent Profile management and the two defined associations; `/proposals`, agent runtime, tool execution, and tool authorization remain forbidden. This does not implement Agent Management itself — it authorizes the SM-801..SM-806 backlog to do so incrementally, each ticket independently gated.

The schema gains `agents`, `agent_skills`, and `agent_sessions` (planned migrations `0015`/`0016`/`0017`, each revising the prior), with no new Neo4j responsibility, no new `graph_outbox` event type, and no new external infrastructure dependency.

Agent Profile history and exact per-operation Agent attribution are both explicit non-guarantees of v0.5. A caller needing either must maintain its own snapshot/attribution state; Sofias Memory does not silently claim a guarantee its schema cannot back.

Future work that needs per-operation Agent attribution, Agent Profile history, or Agent-scoped/semantic resolve is additive and does not require redesigning Agent identity, `agent_skills`, or `agent_sessions` as defined here.

## Alternatives Rejected

- **A Cognee-style full agent runtime (execution, tool orchestration, provider-session management inside Sofias Memory).** Rejected because it would turn a single-user memory service into a multi-tenant agent platform, exactly what the original PRD excluded and what ADR-0012/ADR-0013 both already refused to become.

- **`AgentRevision` in v0.5.** Rejected because Agent Profile has no equivalent of Skill's replay-safety/in-flight-execution invariant; adding it would be complexity for symmetry's sake, not for a concrete need, and would still not solve historical attribution for Query/PipelineRun (see the next rejected alternative).

- **`sessions.agent_id` as a single-owner nullable FK (one Agent per Session).** Rejected because it hard-codes "no" to the question of whether a Session can be used by more than one Agent over its lifetime, directly inside the `sessions` table — exactly the kind of Session redesign ADR-0012 promised a future Agent Management layer would not require.

- **Direct `Query.agent_id` / `PipelineRun.agent_id` columns.** Rejected because no concrete invariant in this product currently requires per-operation Agent attribution, and adding it would duplicate provenance without justification while creating a false impression of precision that M:N `agent_sessions` cannot actually back for existing Session-only correlation.

- **Projecting Agent into Neo4j.** Rejected because Agent state is operational/identity/association state, not semantic graph knowledge — the same reasoning already applied twice.

- **Agent embeddings / semantic Agent resolve.** Rejected because a single-user product has no proven need for similarity search over a management-scale number of Agents; list/get by identity is sufficient, and adding it speculatively would be an unused pgvector column and embedding-provider dependency with no caller need.

- **Always-current `agent_skills` with no pin option (Skill's `current_revision_id` always implicitly followed).** Rejected because it makes every Skill rollback/new-revision silently change behavior for every associated Agent with no audit trail at the association level — surprising and unauditable.

- **Always-pinned `agent_skills` (no follow-current option).** Rejected because it removes the common-case convenience of an Agent automatically picking up Skill improvements, forcing every caller to manually re-pin after every Skill revision even when no behavioral guarantee is actually needed.

- **Provider/model/tool-credential fields inside Agent Profile (`model`, `provider`, `temperature`, `max_tokens`, `provider_session_id`, `tool_credentials`).** Rejected because these are runtime/provider configuration owned by the external caller, not durable Agent identity — storing them would make Sofias Memory a provider-session manager, a boundary ADR-0003 and this ADR both explicitly refuse to cross.

## References

- v0.5.0 Agent Management Feature Contract.
- `docs/product/Sofias_Memory_PRD_SPECS.md` — original `agents/skills/proposals` exclusion, § Fora de escopo and § Rotas proibidas.
- `docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md`.
- `docs/adr/0003-single-static-api-key.md`.
- `docs/adr/0007-postgresql-enums-fk-delete-policies.md`.
- `docs/adr/0012-first-class-durable-sessions.md` — Agent↔Session non-redesign promise; the Session archive/admission-barrier precedent this ADR deliberately diverges from.
- `docs/adr/0013-first-class-durable-procedural-skills.md` — Agent↔Skill non-redesign promise, `skill_revisions(skill_id, id)` candidate key reused by the pin FK, and the Skill archive/discovery-filter precedent this ADR follows.
