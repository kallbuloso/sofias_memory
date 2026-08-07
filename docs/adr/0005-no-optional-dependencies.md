# ADR-0005: No Optional Dependencies

## Status

accepted

## Context

Sofias Memory v1 has a fixed stack and a narrow product scope. Optional dependency
groups and plugin registries would make the installed behavior vary by environment and
would encourage unsupported combinations such as alternate database providers or
ad-hoc ingestion extensions.

The foundation should produce one reproducible installation for the supported MVP.

## Decision

All supported runtime dependencies for v1 belong to the main installation:

- do not create `[project.optional-dependencies]`;
- do not add extras such as `[postgres]`, `[neo4j]`, `[docs]`, `[scraping]`,
  `[llm]`, or `[embeddings]`;
- do not add a plugin registry or dynamic plugin loading system;
- do not use optional dependencies to hide provider selection, database selection, or
  cloud/sync behavior;
- future features enter as explicit versioned releases, not dynamic plugins.

Development tooling may be organized separately in the project dev dependency group
when that task is implemented, but that does not create optional runtime feature sets.

## Consequences

The installed application has predictable capabilities and fewer runtime branches.
Images may include all supported dependency families, but the tradeoff is acceptable
for the MVP because correctness and reproducibility matter more than a minimal install
matrix.

Any future optional feature mechanism requires a new accepted ADR.

## Alternatives Rejected

- Optional extras for PostgreSQL, Neo4j, documents, scraping, LLMs, embeddings, or
  deployment profiles, because they fragment the supported runtime.
- A plugin system or plugin registry, because features should be normal versioned
  product code in v1.
- Community adapter loading, because it reintroduces unsupported provider and database
  combinations.
- Cloud/sync packages hidden behind extras, because sync is outside the MVP.
- Provider abstraction packages used to make optional backends appear interchangeable.

## References

- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.5, "Stack unica e sem opcionais".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.7, "Sem Sync".
- `AGENTS.md`, sections 3 and 4.
