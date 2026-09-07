# ADR-0013: First-Class Durable Procedural Skills

## Status

accepted

## Context

The original Sofias Memory PRD baseline excluded `agents/skills/proposals` entirely, treating them as Cognee capabilities that belong to a multi-tenant agent platform rather than a single-user memory service. `/skills` has been an explicitly forbidden route prefix since the initial contract.

That exclusion was correct for a service whose only job was semantic memory: Datasets, Sources, Documents, Chunks, Entities, Relations, and — since ADR-0012 — Sessions. None of those concepts represent reusable procedural knowledge about *how to perform a task*.

Sofias Memory now needs to serve Sofia's Assistant and similar agentic callers that must select, read, and apply reusable procedures at runtime, in a way that is durable, versioned, auditable, and independent of any single agent process or filesystem checkout. The industry has converged on a portable format for this (the Agent Skills `SKILL.md` convention: YAML frontmatter with `name`/`description`/optional fields, plus a Markdown procedure body). Sofias Memory should store and serve that shape without becoming the thing that executes it.

At the same time, nothing about this need requires Sofias Memory to become an agent runtime, a tool executor, or an authorization system. Those remain explicitly out of scope, exactly as the original PRD intended — this ADR narrows the exclusion, it does not reverse it.

## Decision

Sofias Memory will introduce **first-class durable procedural Skills** beginning in v0.4.0.

### Skill vs semantic memory

A Skill is **procedural memory**: reusable knowledge about how to execute a task. It is not semantic memory. It never becomes a Document, Chunk, Entity, or Relation, is never produced by Remember/Cognify, and never has a Source. Forget and Dataset Delete never touch it.

### Skill vs runtime

Sofias Memory stores, versions, indexes, resolves, and returns Skills. It is not:

- an agent runtime;
- a tool executor;
- an authorization principal;
- a scheduler of anything a Skill describes.

The caller decides when to select a Skill, interprets its instructions, and executes any tools the procedure implies. This mirrors the same boundary ADR-0012 drew for Sessions: Sofias Memory owns durable state, never the agentic loop around it.

### Identity and immutable revisions

A `Skill` is the durable logical identity: a single internal UUID primary key (`Skill.id`), serialized publicly as `skill_uuid` with no separate duplicating column — the same one-UUID pattern ADR-0012 already established for `Session.id`/`session_uuid` — plus one globally unique, immutable, case-sensitive-by-contract `name` (the portable Agent Skills identifier subset — lowercase `a-z0-9-`, 1–64 chars, no leading/trailing/doubled hyphen). Renaming a Skill creates a new identity; `name` is never derived from or tied to a filesystem path.

A Skill is never publicly observable without content: creating a Skill and creating its first `SkillRevision` (with `current_revision_id` pointing at it) happen atomically in one transaction. Procedural content lives in that separate, append-only `SkillRevision` per Skill, numbered `1, 2, 3, ...` monotonically per Skill — never by timestamp, never by an externally supplied `metadata.version` — with creation and rollback of the `current_revision_id` pointer serialized per-Skill so concurrent writers never produce colliding ordinals or a lost pointer update. A published revision is never edited in place; any change to its content fields produces a new revision, except when the new content is semantically identical to an existing revision of the same Skill, which resolves as a safe replay rather than a duplicate. `Skill.current_revision_id` is an explicit, atomically-updated pointer; rolling back means repointing to a historical, unmodified revision, never copying or mutating content — and a safe replay never moves that pointer on its own. This is the same content-addressed, append-only discipline Sofias Memory already applies to `SessionEntry` and to Source/Document immutability. Skill/SkillRevision identity, content hashing, and concurrency behavior are specified in full in the v0.4.0 Skills Feature Contract; this ADR freezes only the shape of the decision.

### PostgreSQL authority, pgvector discovery, no Neo4j

PostgreSQL remains the sole authority for Skill and SkillRevision state, following ADR-0002. Semantic resolution uses pgvector inside PostgreSQL, reusing the existing embedding provider configuration — no new vector database. Skills and SkillRevisions are never projected into Neo4j: they are not graph knowledge, graph reconciliation gains no new responsibility, and `graph_outbox` never carries a Skill event.

### Discovery embedding is metadata-only

The resolution embedding indexes *when to use* a Skill (`name` + `description` + `tags`), never the full `procedure`. This keeps discovery cheap, keeps the embedded text auditable, and enforces the same separation Session Context already established between compact selection metadata and full content fetched only after explicit selection.

### Global, non-owned scope

Skills belong to the single Sofias Memory instance, not to a Dataset, Session, or Agent. No `dataset_id`, `session_id`, `agent_id`, `owner_id`, `tenant_id`, or `user_id` is added to `skills` or `skill_revisions`. Sessions gain no automatic Skill association. Agent↔Skill association is deferred to v0.5 Agent Management and must not require redesigning Skill identity or the revision model — the same non-redesign guarantee ADR-0012 gave Query/PipelineRun for a future Agent Management layer.

### Progressive disclosure

Discovery and listing return bounded selection metadata only (`skill_uuid`, `name`, `description`, `current_revision`, `tags`, `declared_tools`, `compatibility`, and a similarity score where applicable). The full `procedure` is only returned by an explicit detail/revision read. This bounds context for callers doing discovery and keeps the decision to fetch and execute a full procedure an explicit, auditable caller action.

### SKILL.md is an interoperability format, not the source of truth

The public structured API (`Skill` + `SkillRevision`) is the authoritative resource model. Standalone `SKILL.md` import/export is a compatibility surface on top of it, not a second, parallel architecture — the filesystem package layout is never authoritative. v0.4.0 supports only the standalone-file portable subset (`name`, `description`, optional `license`/`compatibility`/`metadata`, Markdown body as `procedure`); bundled `scripts/`, `references/`, `assets/`, and zip packages are out of scope and are not routed through Source object storage in this release.

### Declared tools are never authorization

The Agent Skills format's `allowed-tools` field, if present on import, is preserved only as descriptive/declarative metadata. Sofias Memory's own `declared_tools` field is the same: a hint, never an authorization grant, never a bypass of caller-side permissions, and never interpreted by Sofias Memory as permission to execute anything — consistent with this service never being a tool executor.

### No execution, no SkillRun

v0.4.0 introduces no `SkillRun`, no execution tracking, no tool invocation, and no automatic Skill selection inside Recall or anywhere else. Resolution returns a ranking; it never chooses on the caller's behalf, never loads the procedure automatically, and never creates a `SessionEntry` or Recall `Query` as a side effect.

## Consequences

Sofias Memory gains a durable, versioned procedural-knowledge layer that Sofia's Assistant and similar callers can rely on across restarts and across agent processes, without Sofias Memory becoming an agent runtime.

`/skills` is removed from the forbidden route prefix list; `/agents` and `/proposals` remain forbidden. This is a deliberate, narrow amendment of the original PRD's blanket `agents/skills/proposals` exclusion — the PRD's rejection of an agent runtime, tool execution, and multi-tenant agent platform stands unchanged.

The schema gains `skills` and `skill_revisions` tables plus a pgvector column on the revision-adjacent resolution surface, but no new Neo4j responsibility and no new external infrastructure dependency.

Future Agent Management (v0.5) can associate Agents with Skills via an explicit join, without altering Skill identity, revision numbering, or content immutability.

## Alternatives Rejected

- **Store Skills as Documents/Chunks via Remember/Cognify.** Rejected because procedural knowledge is not extracted semantic knowledge; forcing it through Cognify would tie its lifecycle to Dataset/Source deletion semantics that do not apply to it.

- **Make the filesystem `SKILL.md` package the authoritative resource.** Rejected because it would create a second, parallel identity/versioning model (path-based) alongside the structured API, and would tie Sofias Memory to a filesystem layout it does not own.

- **Embed the full `procedure` for resolution.** Rejected to keep discovery embeddings small, cheap, and focused on *when to use* a Skill, mirroring the metadata/procedure split progressive disclosure requires.

- **Treat `allowed-tools`/`declared_tools` as an authorization grant.** Rejected because Sofias Memory has no tool executor and no per-caller permission model; treating a declarative hint as a grant would silently create an authorization surface that does not otherwise exist in this service (ADR-0003's single static API key).

- **Version Skills by timestamp or by an externally supplied `metadata.version`.** Rejected because neither is guaranteed monotonic, unique, or free of clock/import skew; an internal per-Skill integer ordinal is the same discipline Sofias Memory already uses for PipelineRun `attempt` numbering.

- **Support bundled `scripts/`/`references/`/`assets/` in v0.4.0.** Rejected to keep this release's storage boundary to PostgreSQL text content only; bundled binary/script resources would require a Source-object-storage-shaped feature this release does not need.

- **Project Skills into Neo4j.** Rejected because Skill state is procedural/operational, not semantic graph knowledge — the same reasoning ADR-0012 applied to Sessions.

- **Allow archived Skills to keep participating in semantic resolution.** Rejected because archive's entire purpose is to remove a Skill from discovery while preserving its history for audit/rollback, mirroring Dataset and Session archive/soft-delete semantics elsewhere in the product.

## References

- v0.4.0 Skills Feature Contract.
- `docs/product/Sofias_Memory_PRD_SPECS.md` — original `agents/skills/proposals` exclusion, § Fora de escopo and § Rotas proibidas.
- `docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md`.
- `docs/adr/0003-single-static-api-key.md`.
- `docs/adr/0012-first-class-durable-sessions.md` — the immediately preceding precedent for a durable, non-runtime, non-Neo4j-projected first-class concept with an immutable-append-only child entity.
- Agent Skills format (`SKILL.md` frontmatter convention) — external interoperability reference only, not an architectural dependency.
