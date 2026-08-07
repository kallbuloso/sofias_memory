# ADR-0001: Modular Monolith

## Status

accepted

## Context

Sofias Memory is a focused, single-user service. The MVP needs one coherent codebase
and one deployable application version while still keeping internal module boundaries
clear enough for API, services, domain, pipelines, loaders, and infrastructure.

The system still depends on external infrastructure services for persistence and graph
traversal. Calling the application a monolith must not imply embedding PostgreSQL or
Neo4j inside the Python process or image.

## Decision

Sofias Memory will be implemented as a modular monolith:

- one Python application in the `sofias_memory/` package;
- API, pipelines, and the internal worker belong to the same application and version;
- PostgreSQL and Neo4j are external services, not embedded application components;
- the MVP runs a single application replica;
- modules must preserve the dependency flow defined by the repository architecture.

The MVP must not introduce user accounts, roles, permissions, ACLs, tenants,
organizations, cloud sync, a plugin system, or a frontend to split responsibilities
that belong in the single application.

## Consequences

The codebase stays easier to reason about and deploy during the foundation phase. API
and worker behavior can share domain contracts without cross-service version skew.

Scaling by multiple application replicas, splitting the worker into a separate service,
or moving to a distributed architecture requires a future ADR.

## Alternatives Rejected

- Separate API and worker services in the MVP, because that adds coordination and
  deployment complexity before the persistence and pipeline contracts are stable.
- Multiple application replicas in the MVP, because dataset-level write coordination is
  not yet part of the foundation.
- A microservice architecture, because it would create speculative boundaries.
- Embedding PostgreSQL or Neo4j in the application container, because they are external
  services with their own backup, upgrade, and recovery needs.
- Reintroducing user, permission, tenant, ACL, cloud/sync, frontend, or plugin concepts
  to justify extra service boundaries.

## References

- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.1, "Tipo de aplicacao".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.2, "Single-user".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 14, "Arquitetura de software".
- `AGENTS.md`, sections 3, 6, and 7.
