# ADR-0007: PostgreSQL Enums and Foreign Key Delete Policies

## Status

accepted

## Context

Sofias Memory needs to freeze the persistent state machines and relationship
lifecycle rules before Alembic migrations and SQLAlchemy models are introduced.

PostgreSQL is the source of truth. Neo4j is a rebuildable projection derived
from confirmed PostgreSQL state and graph outbox events. The schema must
preserve provenance, support explicit `forget` workflows, and avoid accidental
destructive cascades.

The planned PostgreSQL schema contains only these B2 tables:

- `datasets`
- `sources`
- `documents`
- `chunks`
- `entities`
- `entity_mentions`
- `relations`
- `relation_evidence`
- `summaries`
- `memory_entries`
- `queries`
- `feedback`
- `pipeline_runs`
- `pipeline_steps`
- `graph_outbox`

The schema must not introduce users, user ownership, tenants, organizations,
roles, permissions, API key persistence, or auth-related database tables.

## Decision

Persistent states that are stable and closed for the MVP will be implemented as
PostgreSQL ENUM types in future migrations. Open-ended classification fields
will remain `TEXT` or receive simple `CHECK` constraints when the allowed set is
small but not architecturally closed.

Foreign keys must use explicit lifecycle policies. `ON DELETE CASCADE` is
allowed only for strict owned children that have no semantic value without their
parent. Parent records that represent memory, provenance, audit history, or
outbox work must not be deletable as an accidental shortcut for product-level
forget behavior.

Physical deletion is not the default product mechanism. `forget` is the
coordinated workflow that marks records as deleting when applicable, removes or
deactivates derived state in the correct order, enqueues graph projection
deletes, cleans up storage, and only then permits final physical cleanup where
safe.

## Enum Policy

Use PostgreSQL ENUM for stable MVP lifecycle state machines:

- dataset status;
- source kind;
- source status;
- summary target type;
- memory entry type;
- pipeline type;
- pipeline run status;
- pipeline step status;
- graph outbox operation;
- graph outbox status.

Do not create PostgreSQL ENUM types for open-ended or product-evolving values:

- `entities.entity_type` remains `TEXT`;
- `relations.predicate` remains `TEXT`;
- `queries.mode` remains `TEXT` with application/API validation;
- `feedback.target_type` remains `TEXT`;
- `graph_outbox.aggregate_type` remains `TEXT`;
- MIME type, language, worker ID, step name, model name, and similar labels
  remain textual values.

`feedback.score` should be a numeric column with a `CHECK` constraint for the
allowed range, not a PostgreSQL ENUM.

Python enums are intentionally not created in this ADR. They may be introduced
with the domain/model layer when SM-203 and later tasks need code-level
contracts. Future Python enums must use stable persisted lowercase snake_case
values and must not import SQLAlchemy from domain modules.

## Selected Enums and Exact Values

| PostgreSQL enum | Values |
| --- | --- |
| `dataset_status` | `active`, `deleting`, `deleted` |
| `source_kind` | `text`, `file`, `url` |
| `source_status` | `pending`, `processing`, `active`, `failed`, `deleting`, `deleted` |
| `summary_target_type` | `document`, `entity`, `dataset`, `cluster` |
| `memory_entry_type` | `text`, `qa`, `feedback`, `note` |
| `pipeline_type` | `remember`, `cognify`, `improve`, `forget` |
| `pipeline_run_status` | `queued`, `running`, `succeeded`, `failed`, `cancelling`, `cancelled` |
| `pipeline_step_status` | `queued`, `running`, `succeeded`, `failed`, `cancelling`, `cancelled` |
| `graph_outbox_operation` | `upsert`, `delete` |
| `graph_outbox_status` | `pending`, `processing`, `done`, `failed` |

`source_status`, `pipeline_type`, and `pipeline_step_status` were not fully
enumerated in the PRD. SM-202 resolves them as part of the schema gate:

- `source_status` covers ingest, processing, active material, failed processing,
  and explicit forget lifecycle;
- `pipeline_type` covers product workflows that create durable pipeline runs in
  the MVP and excludes request-time recall and feedback persistence;
- `pipeline_step_status` mirrors the run lifecycle so cancellation and failure
  semantics stay consistent across runs and steps.

## FK/Delete Policy Principles

- Use `RESTRICT` as the default blocking policy for required relationships that
  must be deleted through an explicit workflow.
- Use `CASCADE` only for strict owned children that are meaningless without the
  parent.
- Use `SET NULL` only when the child record remains semantically valid after the
  parent is gone and the parent reference is optional provenance.
- Prefer explicit deletion order inside `forget` over broad database cascades.
- Do not use `SET DEFAULT`.
- Do not create generic soft-delete columns beyond the status and active
  generation fields already planned by the PRD.

Between `RESTRICT` and `NO ACTION`, future migrations should prefer `RESTRICT`
unless a specific deferrable constraint is required. `RESTRICT` makes accidental
physical deletion fail immediately and documents the intended lifecycle clearly.

## Complete FK Matrix

| Child table | FK | Parent | Nullable | ON DELETE | Rationale |
| --- | --- | --- | --- | --- | --- |
| `sources` | `dataset_id` | `datasets.id` | false | `RESTRICT` | A source belongs to a dataset. Dataset deletion must go through `forget`, not remove sources accidentally. |
| `documents` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Documents are dataset-scoped provenance and must be removed in a coordinated delete flow. |
| `documents` | `source_id` | `sources.id` | false | `RESTRICT` | A document cannot exist without its source, but source deletion must first handle chunks, mentions, evidence, storage, and graph outbox work. |
| `chunks` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Chunks are retrievable memory/provenance and must not disappear from a dataset delete shortcut. |
| `chunks` | `document_id` | `documents.id` | false | `RESTRICT` | Chunk deletion must be coordinated with mentions, relation evidence, embeddings, lexical indexes, and active generation state. |
| `chunks` | `source_id` | `sources.id` | false | `RESTRICT` | Source-level forget must explicitly handle all derived chunks before a source can be physically purged. |
| `entities` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Entities are canonical dataset knowledge and may be shared by multiple sources. Dataset deletion must be explicit. |
| `entity_mentions` | `entity_id` | `entities.id` | false | `CASCADE` | A mention has no independent meaning after its entity is explicitly removed. Entity removal remains blocked by relations first. |
| `entity_mentions` | `chunk_id` | `chunks.id` | false | `CASCADE` | A mention cannot exist without the chunk text that evidenced it. Chunk deletion is already controlled by upstream `RESTRICT` relationships. |
| `relations` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Relations are dataset knowledge and must be removed by explicit graph/memory lifecycle operations. |
| `relations` | `source_entity_id` | `entities.id` | false | `RESTRICT` | Entity deletion must not silently remove relations. Relations must be removed or rewired deliberately. |
| `relations` | `target_entity_id` | `entities.id` | false | `RESTRICT` | Entity deletion must not silently remove relations. Relations must be removed or rewired deliberately. |
| `relation_evidence` | `relation_id` | `relations.id` | false | `CASCADE` | Evidence has no meaning after its relation is explicitly removed. |
| `relation_evidence` | `chunk_id` | `chunks.id` | false | `RESTRICT` | Chunk deletion must first evaluate relation evidence so relations do not silently survive without valid evidence. |
| `summaries` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Summaries are generated memory artifacts and must be handled by generation/forget workflows. |
| `summaries` | `target_id` | polymorphic target | true | no database FK | Target may refer to document, entity, dataset, or cluster. Application/repository logic must validate target consistency. |
| `memory_entries` | `dataset_id` | `datasets.id` | false | `RESTRICT` | Memory entries are user-visible memory artifacts and must not be removed by accidental dataset deletion. |
| `memory_entries` | `source_id` | `sources.id` | true | `SET NULL` | Source provenance is optional. Entries that remain semantically valid can survive source purge, while `forget` must explicitly delete entries containing forgotten content. |
| `queries` | `dataset_ids` | `datasets.id` | n/a | no database FK | The PRD models this as a UUID array for multi-dataset query audit. Application logic validates datasets at query time. |
| `feedback` | `query_id` | `queries.id` | false | `RESTRICT` | Feedback belongs to query audit history. Query deletion must explicitly handle feedback first. |
| `feedback` | `target_id` | polymorphic target | true | no database FK | Feedback targets are polymorphic and may reference answer artifacts or retrieved objects. Application logic validates supported targets. |
| `pipeline_runs` | `dataset_id` | `datasets.id` | true | `SET NULL` | Runs are audit records and can outlive a dataset. The run input/payload keeps safe historical context when needed. |
| `pipeline_runs` | `source_id` | `sources.id` | true | `SET NULL` | Runs are audit records and can outlive a source. Source-specific purge must not delete run history. |
| `pipeline_steps` | `run_id` | `pipeline_runs.id` | false | `CASCADE` | Steps are strictly owned by a run and have no independent lifecycle. Run physical deletion remains an explicit maintenance action. |
| `graph_outbox` | `dataset_id` | `datasets.id` | false | no database FK | Outbox events must survive source/aggregate deletion, especially pending delete events. Store the UUID and payload independently. |
| `graph_outbox` | `aggregate_id` | aggregate table ID | false | no database FK | Aggregate rows may be removed before or during projection cleanup. The outbox event is the durable projection instruction. |

## Dataset Lifecycle

Datasets use `dataset_status` values `active`, `deleting`, and `deleted`.

Physical `DELETE FROM datasets` is not the normal product operation. A dataset
forget workflow marks the dataset as `deleting`, coordinates source/document/
chunk cleanup, removes or deactivates derived memory and graph artifacts,
enqueues Neo4j delete work through `graph_outbox`, and only then may mark the
dataset `deleted` or perform a controlled physical purge.

Required child relationships use `RESTRICT` so an operator or future repository
method cannot delete a dataset and accidentally remove large parts of memory,
provenance, pipeline audit, and projection work.

## Source, Document, and Chunk Lifecycle

A source belongs to exactly one dataset. A document belongs to exactly one
source and dataset. A chunk belongs to exactly one document, source, and dataset.
None of these records should exist outside that chain.

Despite the ownership chain, source/document/chunk relationships use `RESTRICT`
instead of broad cascades. Forget and reprocessing must delete in a deliberate
order so that entity mentions, relation evidence, embeddings, lexical indexes,
storage, active generation state, and graph outbox events remain consistent.

Entity mentions may cascade from a chunk because they are direct annotations of
that chunk text. Relation evidence does not cascade from a chunk because relation
lifecycle depends on whether other active evidence remains.

## Entity and Relation Provenance Lifecycle

Entities are canonical per dataset and can be supported by more than one source.
Source-level forget must not delete a shared entity merely because one source was
forgotten. Orphan entity cleanup is an explicit future operation of forget or
improve workflows.

Relations depend on source and target entities and must keep valid evidence. The
entity-to-relation FKs use `RESTRICT` so deleting an entity requires an explicit
decision about affected relations.

`relation_evidence.relation_id` may cascade because evidence is owned by the
relation. `relation_evidence.chunk_id` uses `RESTRICT` because deleting chunk
evidence may change whether a relation is still valid or active.

No separate relation evidence status enum is selected in SM-202. Active evidence
is determined by relation, chunk, source, and generation lifecycle rules.

## Pipeline Run and Step Lifecycle

Pipeline runs are durable operational audit records. They may reference a dataset
and source, but those references are nullable and use `SET NULL` so run history
can survive controlled physical deletion of memory data.

Pipeline steps are strict children of pipeline runs. `pipeline_steps.run_id` uses
`CASCADE` because a step has no meaning without its run. This does not imply runs
are freely deleted; run deletion is a deliberate maintenance/retention action,
not part of normal request handling.

Run and step statuses share the same lifecycle values:

```text
queued -> running -> succeeded
queued -> running -> failed
queued/running -> cancelling -> cancelled
```

## Graph Outbox Lifecycle

`graph_outbox` is part of the transactional bridge from PostgreSQL to Neo4j. It
must remain durable even when the aggregate row is being deleted.

Therefore `graph_outbox.dataset_id` and `graph_outbox.aggregate_id` intentionally
do not have database foreign keys. They store stable UUID values and a safe JSON
payload that is sufficient for the projection worker to perform idempotent
upsert/delete work or for a rebuild process to reason about pending projection
state.

This avoids the failure mode where deleting a source, document, entity, relation,
or dataset removes an unprocessed graph delete event before Neo4j has been made
consistent.

## Forget/Delete Implications

`forget` is the only product-level deletion workflow for memory data. Database
foreign keys are guardrails, not the orchestration mechanism.

Future forget implementation must:

- mark affected dataset/source records as `deleting` when applicable;
- identify derived documents, chunks, mentions, relation evidence, summaries,
  memory entries, entities, and relations affected by the target;
- preserve shared entities and relations when still supported by other active
  evidence;
- enqueue graph delete/upsert events in `graph_outbox` in the same PostgreSQL
  transaction as source-of-truth changes;
- remove or detach optional provenance only when semantically safe;
- avoid using a parent `DELETE` as a shortcut for the workflow.

The project does not introduce generic `deleted_at` or `is_deleted` columns in
SM-202. It keeps the PRD's explicit status and generation fields.

## Rejected Alternatives

- **Cascade from dataset to all children.** Rejected because it can erase memory,
  provenance, run history, and graph projection work outside the explicit forget
  workflow.
- **Cascade the entire source/document/chunk chain.** Rejected because relation
  evidence and shared graph knowledge need coordinated cleanup.
- **Use `NO ACTION` everywhere by default.** Rejected because `RESTRICT` makes the
  intended immediate blocking behavior clearer for this schema.
- **Use PostgreSQL ENUM for every categorical text field.** Rejected because
  entity types, predicates, query modes, feedback target types, and graph
  aggregate types evolve with product behavior.
- **Use only `TEXT` for all state machines.** Rejected because the MVP has stable
  closed lifecycle values where database-level validation is useful.
- **Foreign keys from graph outbox to aggregate tables.** Rejected because delete
  projection events must survive aggregate deletion.
- **Introduce soft delete generically.** Rejected because the PRD requires
  explicit forget behavior and already defines targeted status/generation fields.
- **Introduce users, tenants, owners, roles, permissions, or API key persistence.**
  Rejected by the single-user MVP architecture and ADR-0003.

## Constraints for SM-206..SM-211

Future schema and migration tasks must:

- create only the PostgreSQL ENUM values selected in this ADR;
- avoid SQLAlchemy models or Alembic migrations that invent new states without a
  follow-up architectural decision;
- implement the FK matrix exactly unless a later ADR updates it;
- use `RESTRICT` as the default required relationship policy;
- limit `CASCADE` to the explicitly approved child relationships;
- use `SET NULL` only for the nullable audit/provenance relationships listed
  here;
- keep `graph_outbox` independent from aggregate-row FKs;
- use `CHECK` constraints for bounded scalar values such as `feedback.score`;
- avoid user, tenant, role, permission, API key, settings, plugin, or provider
  tables;
- keep PostgreSQL as source of truth and Neo4j as a rebuildable projection;
- keep pgvector storage/index decisions aligned with ADR-0006.

## References

- `AGENTS.md`
- `docs/product/Sofias_Memory_PRD_SPECS.md`
- `docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md`
- `docs/adr/0003-single-static-api-key.md`
- `docs/adr/0006-pgvector-3072-halfvec-ann.md`
- `docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B0_B2.md` SM-202
