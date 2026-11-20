# Skills & Agents

Sofias Memory provides two durable management primitives that help applications organize reusable procedural knowledge and agent configuration:

- **Skills** — versioned procedural memory;
- **Agent Profiles** — durable declarative agent configuration with explicit Skill and Session associations.

Sofias Memory stores, resolves, versions, and associates these resources. It does **not** execute Skills or run Agents on behalf of the caller.

---

## 1. Skills: versioned procedural memory

A Skill represents a reusable procedure with a stable logical identity.

Typical examples:

```text
triage-support-ticket
prepare-weekly-report
extract-invoice-fields
review-release-checklist
summarize-customer-history
```

A Skill has a stable `name`, while its procedural content lives in immutable `SkillRevision`s.

That separation gives you:

- stable identity;
- immutable historical revisions;
- explicit current revision;
- rollback by selecting an older existing revision;
- semantic discovery without executing the Skill.

---

## 2. Skill lifecycle and endpoints

Management surface:

```text
POST  /api/v1/skills
GET   /api/v1/skills
GET   /api/v1/skills/{skill_uuid}
PATCH /api/v1/skills/{skill_uuid}
POST  /api/v1/skills/{skill_uuid}/archive
POST  /api/v1/skills/{skill_uuid}/restore
```

Revision surface:

```text
POST /api/v1/skills/{skill_uuid}/revisions
GET  /api/v1/skills/{skill_uuid}/revisions
GET  /api/v1/skills/{skill_uuid}/revisions/{revision}
```

Interoperability/discovery surface:

```text
POST /api/v1/skills/import
POST /api/v1/skills/{skill_uuid}/revisions/import
GET  /api/v1/skills/{skill_uuid}/revisions/{revision}/export
POST /api/v1/skills/resolve
```

There is intentionally no mutation endpoint for an existing `SkillRevision`. Revisions are immutable once created.

---

## 3. Create a Skill

Creating a Skill creates its first revision atomically.

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/skills" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "triage-support-ticket",
    "description": "Classify and prepare a support ticket for handling.",
    "procedure": "# Procedure\n\n1. Read the ticket.\n2. Identify urgency.\n3. Extract the affected product.\n4. Return a structured triage summary.",
    "tags": ["support", "triage"],
    "declared_tools": ["ticket-read"],
    "metadata": {}
  }'
```

`name` is the portable, immutable logical identity. It uses a canonical slug-like form and is globally unique inside the service.

---

## 4. SkillRevision content

Revisionable Skill content includes:

```text
description
procedure
license?
compatibility?
metadata
tags
declared_tools
```

`procedure` is Markdown text.

`declared_tools` is **descriptive only**. It does not grant authorization, install tools, or cause Sofias Memory to execute anything.

---

## 5. Current revision and rollback

The Skill resource points to one `current_revision`.

Creating a new revision can advance that pointer. Historical revisions remain immutable.

`PATCH /api/v1/skills/{skill_uuid}` is intentionally narrow: it only changes the `current_revision` pointer to an already-existing revision.

Conceptually:

```json
{
  "current_revision": 2
}
```

This is rollback/selection, not content mutation.

---

## 6. Skill listing vs revision detail

Sofias Memory uses progressive disclosure.

General Skill/list responses do not include the full procedure body.

To retrieve executable/readable procedure text, explicitly request a revision:

```text
GET /api/v1/skills/{skill_uuid}/revisions/{revision}
```

This keeps collection/discovery responses bounded even when a procedure is large.

---

## 7. Skill semantic resolve

Use:

```text
POST /api/v1/skills/resolve
```

with a natural-language query to discover relevant active Skills.

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/skills/resolve" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "I need to classify a customer support request",
    "top_k": 5
  }'
```

Resolve returns ranked metadata candidates with a similarity `score`.

It does not return the full `procedure`, and it does not select or execute a Skill on the caller's behalf.

The caller remains responsible for:

```text
candidate selection
policy checks
authorization
tool availability
procedure retrieval
execution
```

---

## 8. SKILL.md import/export

Skills support standalone `SKILL.md` interoperability.

The public surface allows:

```text
import a new Skill from SKILL.md text
import a new revision into an existing Skill
export one exact revision as SKILL.md
```

The transport is standalone text, not a zip/package/filesystem path abstraction.

This makes Skills portable while preserving Sofias Memory's canonical validation and revision model.

---

# Agent Profiles

## 9. What an Agent Profile is

An Agent Profile is durable declarative configuration.

It can store:

```text
name
display_name?
description?
instructions?
metadata
status
```

Typical use:

```text
support-agent
research-agent
release-reviewer
customer-success-helper
```

The `name` is stable and immutable. Human-facing labels and declarative configuration can be updated.

---

## 10. What an Agent Profile is not

Sofias Memory is **not an agent runtime**.

An Agent Profile does not imply that Sofias Memory will:

- call an LLM;
- execute instructions;
- invoke tools;
- manage a provider session;
- decide which Skill to run;
- own an agent loop;
- manage permission/confirmation UX.

The caller reads the configuration and decides how to use it.

This boundary is intentional.

---

## 11. Agent management endpoints

```text
POST  /api/v1/agents
GET   /api/v1/agents
GET   /api/v1/agents/{agent_uuid}
PATCH /api/v1/agents/{agent_uuid}
POST  /api/v1/agents/{agent_uuid}/archive
POST  /api/v1/agents/{agent_uuid}/restore
```

There is no Agent hard-delete route and no execution-shaped route such as:

```text
/run
/execute
/chat
/invoke
/tools
```

---

## 12. Create an Agent Profile

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/agents" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "support-agent",
    "display_name": "Support Agent",
    "description": "Declarative profile for support workflows.",
    "instructions": "Keep responses concise and ground answers in available evidence.",
    "metadata": {
      "team": "support"
    }
  }'
```

`instructions` are opaque declarative text. Sofias Memory stores them but does not interpret or execute them.

---

## 13. Updating an Agent Profile

`PATCH /agents/{agent_uuid}` can update:

```text
display_name
description
instructions
metadata
```

It cannot mutate:

```text
name
status
```

Archive/restore are explicit lifecycle operations.

`metadata` replacement is whole-object replacement rather than a deep merge.

---

# Agent ↔ Skill associations

## 14. Associate Skills with an Agent

Public surface:

```text
GET    /api/v1/agents/{agent_uuid}/skills
PUT    /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
DELETE /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
```

The association can either:

```text
follow the Skill's current revision live
```

or:

```text
pin one exact revision
```

Example pin:

```json
{
  "pinned_revision": 3
}
```

Example follow-current:

```json
{
  "pinned_revision": null
}
```

Omitting `pinned_revision` has the same follow-current meaning.

The association response exposes both:

```text
current_revision
pinned_revision
effective_revision
```

so the caller can see what will actually be used.

---

## 15. Association does not execute a Skill

An Agent↔Skill association means:

> "this Skill is explicitly available in this Agent Profile's managed configuration"

It does not mean:

> "Sofias Memory will execute this Skill automatically"

The caller still owns Skill discovery/admission/execution policy.

---

# Agent ↔ Session associations

## 16. Associate Sessions with an Agent

Public surface:

```text
GET    /api/v1/agents/{agent_uuid}/sessions
PUT    /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
DELETE /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
```

The `PUT` association carries no request body.

The relation is management metadata only.

The response deliberately exposes only a limited Session summary and the association timestamp. It does not return Session entries, transcripts, queries, metadata, or run content.

---

## 17. Archived resources

Archiving is a lifecycle/discovery concept, not destructive erasure.

For example:

- an archived Skill remains historically present;
- an Agent↔Skill association can remain present when the Skill is archived;
- archived Skills are excluded from semantic `resolve` discovery;
- an archived Agent keeps its explicit associations;
- archived Sessions may remain visible in Agent association management.

This preserves durable configuration/history without pretending archive means delete.

---

## 18. Choosing the right primitive

Use:

### Cognitive Memory

for durable facts/preferences/truth:

```text
"Customer prefers email."
```

### Session

for temporal contextual history:

```text
"The current support case is about invoice 4821."
```

### Skill

for a reusable procedure:

```text
"How to triage a support ticket."
```

### Agent Profile

for durable declarative agent configuration:

```text
"Support Agent uses these instructions and is associated with these Skills/Sessions."
```

Keeping these concepts separate prevents a generic metadata blob from becoming the system's entire memory model.

---

## 19. Canonical references

- [`docs/api.md`](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [ADR-0013 — First-class durable procedural Skills](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0013-first-class-durable-procedural-skills.md)
- [ADR-0014 — First-class durable Agent Management](https://github.com/kallbuloso/sofias_memory/blob/main/docs/adr/0014-first-class-durable-agent-management.md)
- [Skills Feature Contract](https://github.com/kallbuloso/sofias_memory/blob/main/docs/product/Sofias_Memory_Feature_Contract_v0.4.0_Skills.md)
- [Agent Management Feature Contract](https://github.com/kallbuloso/sofias_memory/blob/main/docs/product/Sofias_Memory_Feature_Contract_v0.5.0_Agent_Management.md)

For Session semantics, continue with [Sessions](Sessions).
