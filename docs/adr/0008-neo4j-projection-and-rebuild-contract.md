# ADR-0008: Neo4j Projection and Rebuild Contract

## Status

Accepted.

## Context

Sofias Memory has completed the PostgreSQL B2 foundation. PostgreSQL is the source
of truth for datasets, sources, documents, chunks, entities, relations, evidence,
pipeline state, and the transactional `graph_outbox`.

The PRD defines a small Neo4j graph model for recall and graph traversal, but it
does not fully freeze the projection contract: outbox aggregate types, payload
shape, relationship identity, delete behavior after PostgreSQL rows are removed,
replay semantics, rebuild semantics, and the boundary between the B3 projection
consumer and the later B5 worker.

This ADR freezes those decisions before introducing the Neo4j driver or any
production Cypher.

## Decision

Neo4j is a rebuildable projection of confirmed PostgreSQL state. The B3 work will
add the minimal infrastructure needed to connect to Neo4j, install the required
Neo4j schema, check readiness, and apply one projection command at a time. It will
not implement a polling worker, queue claiming, pipeline scheduling, graph-RAG, or
business workflows.

Projection writes are driven by `graph_outbox` rows created in the same
PostgreSQL transaction as the authoritative change. The outbox payload is a
versioned projection-command snapshot with enough identity data to apply idempotent
upserts and deletes without relying on a PostgreSQL row that may already have been
deleted.

## PostgreSQL Authority

PostgreSQL remains authoritative for all persisted knowledge, evidence,
provenance, lifecycle state, and recovery data.

Neo4j must not contain knowledge that cannot be reconstructed from PostgreSQL and
source storage. Neo4j query paths may return identifiers, but API responses and
evidence hydration must resolve authoritative content from PostgreSQL.

There is no distributed transaction between PostgreSQL and Neo4j. The durable
boundary is:

1. persist authoritative PostgreSQL state;
2. persist `graph_outbox` event in the same PostgreSQL transaction;
3. commit PostgreSQL;
4. later apply the event to Neo4j;
5. mark the event status in PostgreSQL.

## Neo4j Graph Model

The only projected node labels for the MVP are:

```text
Entity
Chunk
```

The only projected relationship types for the MVP are:

```text
RELATES_TO
MENTIONED_IN
NEXT
```

Projected `Entity` node properties:

```text
id
dataset_id
name
entity_type
description
importance_weight
generation
```

Projected `Chunk` node properties:

```text
id
dataset_id
source_id
document_id
ordinal
generation
```

Projected `RELATES_TO` properties:

```text
relation_id
predicate
description
confidence
importance_weight
generation
```

Projected `MENTIONED_IN` properties:

```text
mention_id
confidence
```

Each PostgreSQL `entity_mentions.id` represents one distinct persisted mention
occurrence. The current PostgreSQL schema intentionally has no
`UNIQUE(entity_id, chunk_id)`, so multiple mentions of the same entity in the same
chunk are valid and must not be collapsed accidentally.

`mention_id` is a technical projection property on `MENTIONED_IN` used for
identity and idempotency. It corresponds exactly to PostgreSQL
`entity_mentions.id`. This extends the PRD's minimal `MENTIONED_IN {confidence}`
example operationally; it does not make Neo4j authoritative for mentions and does
not add a second source of truth. Complete mention evidence remains in
PostgreSQL.

Replay must be able to identify the exact mention occurrence by `mention_id`.
Delete of one mention uses the frozen payload identity and endpoints:
`mention_id`, `entity_id`, and `chunk_id`.

Projected `NEXT` relationships have no business properties. A valid `NEXT` links
only consecutive projected chunks in the same dataset, document, and generation:

```text
from.dataset_id = to.dataset_id
from.document_id = to.document_id
from.generation = to.generation
from.is_active = true
to.is_active = true
to.ordinal = from.ordinal + 1
```

`NEXT` never crosses dataset, document, or generation boundaries. Its identity is
derived from `(from_chunk_id, to_chunk_id)`.

The projection does not create Neo4j nodes for `dataset`, `source`, `document`,
`summary`, `memory_entry`, `query`, `feedback`, `pipeline_run`, or
`pipeline_step`.

## Projection Identities

Projection identity is always derived from PostgreSQL identifiers:

```text
Entity node      -> entities.id
Chunk node       -> chunks.id
RELATES_TO       -> relations.id, stored as relation_id
MENTIONED_IN     -> entity_mentions.id, stored as mention_id
NEXT             -> (from_chunk_id, to_chunk_id)
```

The projection must not create Neo4j-only identifiers for these objects.

`graph_outbox.aggregate_id` stores the same stable id for row-backed aggregate
types:

```text
entity          -> entities.id
chunk           -> chunks.id
relation        -> relations.id
entity_mention  -> entity_mentions.id
```

For `chunk_next`, `graph_outbox.aggregate_id` stores `from_chunk_id`. The full
projection identity remains `(from_chunk_id, to_chunk_id)` in the payload because
`NEXT` is a deterministic edge between two persisted chunks, not a PostgreSQL row
with its own id.

`RELATES_TO`, `MENTIONED_IN`, and `NEXT` upserts require endpoint node identities
in the payload. A relationship event must not invent placeholder nodes that have
not been projected from PostgreSQL state.

## Outbox Aggregate Types

`graph_outbox.aggregate_type` is constrained by application contract, not by the
B2 PostgreSQL schema. The only valid projection aggregate types for the MVP are:

```text
entity
chunk
relation
entity_mention
chunk_next
```

These five types cover the entire PRD Neo4j graph model:

- `entity` projects `(:Entity)`;
- `chunk` projects `(:Chunk)`;
- `relation` projects `[:RELATES_TO]`;
- `entity_mention` projects `[:MENTIONED_IN]`;
- `chunk_next` projects `[:NEXT]`.

No aggregate type is defined for `document`, `source`, `summary`,
`memory_entry`, or `dataset` because those are not Neo4j graph objects in the PRD
model.

## Outbox Payload Contract

`graph_outbox.payload` is a projection command snapshot. It is not merely a
rehydration reference.

Every payload must be JSONB and include:

```text
schema_version
aggregate_type
operation
dataset_id
aggregate_id
identity
```

`schema_version` is currently `1`.

For `upsert`, the payload must also include the minimal `properties` and, for
relationships, `endpoints` needed to apply the projection without querying
business state from Neo4j.

For `delete`, the payload must include enough `identity` and `endpoints` data to
delete the projected object even if the authoritative PostgreSQL row has already
been removed by the transaction that wrote the outbox event.

Payloads must not include source content, chunk text, embeddings, LLM prompts, raw
documents, secrets, database URLs, or other data not required by the Neo4j
projection.

Minimum payload shapes:

```text
entity upsert:
  identity: {id}
  properties: {id, dataset_id, name, entity_type, description, importance_weight, generation}

chunk upsert:
  identity: {id}
  properties: {id, dataset_id, source_id, document_id, ordinal, generation}

relation upsert:
  identity: {relation_id}
  endpoints: {source_entity_id, target_entity_id}
  properties: {relation_id, predicate, description, confidence, importance_weight, generation}

entity_mention upsert:
  identity: {mention_id}
  endpoints: {entity_id, chunk_id}
  properties: {mention_id, confidence}

chunk_next upsert:
  identity: {from_chunk_id, to_chunk_id}
  endpoints: {from_chunk_id, to_chunk_id}
  properties: {}
```

Delete payloads use the same `identity` and `endpoints` keys, with `properties`
omitted unless a future explicit contract requires them.

For `entity_mention` deletes, `identity.mention_id` must equal the original
`entity_mentions.id`, and `endpoints` must include the original `entity_id` and
`chunk_id`.

For `chunk_next`, `aggregate_id` is `from_chunk_id`, while the complete identity
and delete target is always the pair in the payload:
`identity.from_chunk_id` and `identity.to_chunk_id`.

## Upsert Semantics

Upsert semantics are equivalent to Cypher `MERGE` on the stable projection
identity.

Replaying the same upsert must not duplicate a node or relationship. It may update
the existing projection properties to match the event payload.

Relationship upserts require both endpoint nodes to already exist. If an endpoint
is missing, the projection operation must fail safely and remain retryable through
the outbox lifecycle. It must not create partial placeholder nodes.

## Delete Semantics

Delete semantics are idempotent. Deleting an already absent projected object is a
successful no-op.

Deletes must use only the identity and endpoint data carried by the outbox payload.
The consumer must not require rehydrating a PostgreSQL row that may already have
been deleted.

Relationship deletes remove the matching projected relationship only.

Entity and chunk node deletes are allowed to remove remaining projected
relationships attached to that node as a final projection cleanup step. PostgreSQL
remains authoritative, so rebuild can restore any projection that should still
exist.

## Idempotency and Replay

The projection applier must be safe under:

- duplicate delivery of the same outbox row;
- retry after transient Neo4j failure;
- crash after PostgreSQL commit and before Neo4j write;
- crash after Neo4j write and before marking the outbox row done;
- rebuild after Neo4j data loss.

Idempotency is based on the stable identities above. The Neo4j projection must
converge to the state represented by PostgreSQL and the applied payloads.

Outbox status transitions remain PostgreSQL state. The projection applier does not
own polling, leasing, heartbeat, or queue scheduling.

## Projection Port Boundary

Future B3 infrastructure may introduce a concrete Neo4j projection component that
applies one projection command to Neo4j.

The boundary is intentionally small:

```text
apply one projection command
ensure minimal Neo4j schema
delete/rebuild projection scope when explicitly invoked
check Neo4j readiness
```

The domain layer must not import the `neo4j` package, SQLAlchemy models, or Neo4j
infrastructure. No graph database provider abstraction is introduced; Neo4j is the
only graph database supported by the MVP.

## B3 Consumer vs B5 Worker Boundary

B3 may implement the mechanics for applying one already selected
`graph_outbox` event to Neo4j.

B3 must not implement:

- polling loops;
- `FOR UPDATE SKIP LOCKED` queue claiming;
- worker lifecycle;
- heartbeat;
- stale recovery;
- retry scheduling;
- cancellation workflow;
- dataset concurrency controls;
- pipeline orchestration.

Those responsibilities belong to the later worker and pipeline phases.

## Rebuild Contract

Rebuild does not depend on historical `graph_outbox` rows. It reads the current
authoritative PostgreSQL state and recreates Neo4j from that state.

Global rebuild:

1. remove the existing Neo4j projection;
2. install/verify Neo4j constraints and indexes;
3. project all eligible entities and chunks;
4. project eligible `MENTIONED_IN`, `RELATES_TO`, and `NEXT` relationships.

Dataset rebuild:

1. remove projected Neo4j objects for one `dataset_id`;
2. project eligible entities and chunks for that dataset;
3. project eligible relationships for that dataset.

Eligible projected records are:

- Dataset: project only datasets with `datasets.status = 'active'`.
- Chunk: project only chunks where `chunks.generation =
  datasets.active_generation` and `chunks.is_active IS TRUE`.
- Entity: project only entities where `entities.generation =
  datasets.active_generation` and `entities.is_active IS TRUE`.
- Relation: project only relations where `relations.generation =
  datasets.active_generation`, `relations.is_active IS TRUE`, and both source and
  target entities are in the projectable entity set.
- Entity mention: `entity_mentions` has no `generation` or `is_active` column.
  Project a mention only when its referenced entity and chunk are both in their
  projectable sets. Do not invent mention generation or mention active state.
- NEXT: derive only from projectable chunks where both chunks are in the same
  dataset, same document, same generation, both are active, and
  `to.ordinal = from.ordinal + 1`.

Rebuild never depends on historical `chunk_next` outbox events. It derives `NEXT`
again from the PostgreSQL chunks that are projectable at rebuild time.

The rebuild order is:

1. `Entity` nodes;
2. `Chunk` nodes;
3. `MENTIONED_IN` relationships;
4. `RELATES_TO` relationships;
5. `NEXT` relationships.

This order avoids orphan relationships without creating placeholder nodes. Rebuild
must be idempotent and must tolerate an empty Neo4j database.

## Constraints and Index Names

Neo4j schema names are deterministic:

```text
entity_id_unique
chunk_id_unique
entity_dataset_id_index
chunk_dataset_id_index
entity_name_index
```

Required constraints:

```text
Entity.id unique
Chunk.id unique
```

Required indexes:

```text
Entity.dataset_id
Chunk.dataset_id
Entity.name
```

No composite Neo4j indexes are frozen in this ADR. The PRD leaves composite
indexes conditional on version support and query planning, so they require a later
explicit decision.

## Readiness Contract

`GET /health/live` must never connect to Neo4j.

Future `GET /health/ready` Neo4j readiness must be lightweight and read-only. It
must verify:

- connectivity to the configured Neo4j database;
- the configured database is reachable;
- the required constraints and indexes above exist.

Readiness must not create constraints, create indexes, write data, run projection
commands, repair schema, or rebuild Neo4j.

Neo4j unavailability makes the Neo4j component not ready and contributes to
overall readiness being not ready. It must not break liveness and must not leak
credentials, connection URLs, host internals, query payloads, or tracebacks in the
public response.

Constraint and index installation belongs to explicit bootstrap/lifespan
infrastructure, not to the read-only readiness query.

## Standard Cypher Only

Core MVP Neo4j operations must use standard Cypher and the official Neo4j driver.

The core projection must not depend on APOC, GDS, a graph database provider
registry, arbitrary Cypher endpoints, or user-provided Cypher execution.

## Failure and Recovery Model

If PostgreSQL commits and Neo4j is unavailable, the outbox row remains durable and
retryable.

If Neo4j applies an event and PostgreSQL fails to mark it done, replaying the event
must converge to the same projection state.

If a relationship event references missing endpoint nodes, the event must fail
safely and remain recoverable by retry or rebuild. The applier must not create
placeholder nodes with incomplete knowledge.

If Neo4j is corrupted or lost, it may be discarded and rebuilt from PostgreSQL.

Deletes remain possible after PostgreSQL row deletion because delete payloads carry
the projection identity needed to remove the Neo4j object.

## Consequences

Application code that writes graph-affecting PostgreSQL state must also write a
projection command snapshot to `graph_outbox` in the same transaction.

`graph_outbox.aggregate_type` remains `TEXT` in the current PostgreSQL schema; the
allowed values are enforced by application contract and tests until a future ADR
chooses a database-level constraint.

The payload contract is intentionally minimal to avoid duplicating content,
embeddings, or evidence text in Neo4j while still supporting idempotent deletes and
replay.

## Explicit Non-Goals

This ADR does not implement:

- Neo4j driver lifecycle;
- production Cypher;
- Neo4j readiness;
- Neo4j schema bootstrap code;
- graph outbox polling;
- graph outbox claiming;
- worker lifecycle;
- retry scheduler;
- graph-RAG;
- arbitrary Cypher endpoint;
- Neo4j vector search;
- APOC or GDS integration;
- PostgreSQL schema changes;
- new PostgreSQL migrations.
