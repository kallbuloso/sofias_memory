# ADR-0002: PostgreSQL Source of Truth and Neo4j Rebuildable Projection

## Status

accepted

## Context

Sofias Memory stores semantic memory, provenance, pipeline state, and graph-derived
knowledge. The product must be able to audit, delete, recover, and rebuild knowledge
from authoritative records.

Neo4j is valuable for graph traversal, but allowing it to become an independent source
of truth would create dual writes, incomplete provenance, and difficult recovery.

## Decision

PostgreSQL is the authoritative data store for the MVP.

Neo4j is a rebuildable projection derived from committed PostgreSQL state:

- no knowledge, evidence, or provenance may exist exclusively in Neo4j;
- PostgreSQL stores everything required for rebuild, audit, deletion, and recovery;
- PostgreSQL to Neo4j integration will use a transactional outbox stored in
  PostgreSQL;
- the application commits authoritative state and outbox records in the same
  PostgreSQL transaction;
- a worker projects outbox events to Neo4j after commit;
- there will be no distributed transaction between PostgreSQL and Neo4j;
- Neo4j may be destroyed and rebuilt from PostgreSQL and source storage.

Neo4j queries should return identifiers for graph traversal results; content and
evidence are hydrated from PostgreSQL.

## Consequences

Recovery favors correctness over immediate dual-database consistency. Projection lag is
acceptable when it is visible and recoverable through outbox status and rebuild tools.

Every feature that writes graph knowledge must define the authoritative PostgreSQL
state before adding Neo4j projection behavior.

## Alternatives Rejected

- Treating Neo4j as a source of truth, because it would break rebuildability and
  provenance.
- Writing knowledge only to Neo4j, because deletion and audit would become incomplete.
- Distributed transactions across PostgreSQL and Neo4j, because they add operational
  complexity and are unnecessary when the outbox is the recovery boundary.
- A selectable database provider abstraction, because the MVP stack is fixed.
- Replacing PostgreSQL or Neo4j with another database without a future accepted ADR.
- Cloud sync or external replication as a projection mechanism in the MVP.

## References

- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 13, "Modelo de grafo Neo4j".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 14, "Arquitetura de software".
- `AGENTS.md`, sections 5 and 15.
