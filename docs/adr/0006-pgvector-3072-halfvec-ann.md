# ADR-0006: pgvector 3072-Dimension Storage and Halfvec ANN

## Status

accepted

## Context

Sofias Memory uses PostgreSQL as the source of truth and pgvector for vector storage
and retrieval. The product baseline is:

- `EMBEDDING_MODEL=text-embedding-3-large`
- `EMBEDDING_DIMENSIONS=3072`

The planned PostgreSQL schema stores embeddings on chunks, summaries, memory entries,
and relation embeddings. The PRD explicitly models these columns as `VECTOR(3072)` and
states that a model or dimension change requires a migration and reindexing.

The official Compose image for PostgreSQL is `pgvector/pgvector:0.8.1-pg17`. That
image supplies the PostgreSQL extension and therefore governs SQL types, casts,
operator classes, index methods, and dimensional limits. The Python package `pgvector`
declared in `pyproject.toml` and resolved in `uv.lock` only provides Python adapter
integration. It does not define PostgreSQL SQL semantics.

Official pgvector v0.8.1 documentation confirms:

- the `vector` type and `CREATE EXTENSION vector`;
- HNSW and IVFFlat approximate nearest-neighbor indexes;
- HNSW indexed `vector` supports up to 2,000 dimensions;
- HNSW indexed `halfvec` supports up to 4,000 dimensions;
- half-precision expression indexing is supported with casts such as
  `embedding::halfvec(n)`;
- HNSW operator classes exist for `halfvec` L2, inner product, cosine, and L1.

The product baseline uses OpenAI-compatible embeddings with
`text-embedding-3-large`. OpenAI documentation recommends cosine similarity for
embeddings and states that OpenAI API embedding outputs are L2-normalized, so cosine
similarity and Euclidean distance produce identical rankings for this baseline.

## Decision

The authoritative storage representation for embeddings is the full-precision
PostgreSQL column:

```text
embedding VECTOR(3072)
```

The 3072 components are the source-of-truth representation. PostgreSQL remains the
authoritative store for embeddings, provenance, and rebuildable state.

Future ANN candidate retrieval will use an expression index derived from the
authoritative vector:

```text
embedding::halfvec(3072)
```

This half-precision representation is only an index/search projection for candidate
retrieval. It is not the authoritative embedding and must not replace or truncate the
stored `VECTOR(3072)`.

The official vector metric for Sofias Memory is cosine distance.

Future ANN indexing must use HNSW with the `halfvec_cosine_ops` operator class over the
`embedding::halfvec(3072)` expression. Future candidate retrieval must use the matching
cosine distance operator and compatible expression:

```text
embedding::halfvec(3072) <=> query_embedding::halfvec(3072)
```

Optional reranking with the authoritative full vector must also use cosine distance:

```text
embedding <=> query_embedding::vector(3072)
```

SM-201 does not create the migration, table, column, index, query, SQLAlchemy model, or
PostgreSQL connection. Those belong to later B2 tasks.

## Consequences

Sofias Memory preserves the full embedding dimensions defined by the product baseline.
The system does not silently reduce vectors to 1536, truncate them, switch embedding
models, or use custom dimensionality reduction to satisfy index limits.

ANN can still be implemented with pgvector 0.8.1 because `halfvec(3072)` is within the
documented HNSW indexed limit. Candidate retrieval may be faster and smaller than a
full-precision index, at the cost of approximate half-precision ordering.

Retrieval implementations may optionally rerank ANN candidates with the full
`VECTOR(3072)` value when the selected retrieval contract needs higher precision.

Cosine is an explicit Sofias Memory contract. This prevents migrations, ANN indexes,
queries, and tests from selecting metrics independently. Any future metric change
requires a deliberate architecture decision and a coordinated update to indexes,
operator classes, queries, tests, and, when applicable, retrieval fingerprint or
versioning semantics.

## Rejected Alternatives

- Reducing `EMBEDDING_DIMENSIONS` to 1536 or another value to fit an index limit.
- Storing only `halfvec(3072)` as the source of truth.
- Truncating embeddings, applying PCA, or using custom quantization as an implicit
  storage strategy.
- Switching away from `text-embedding-3-large` only to accommodate indexing.
- Replacing PostgreSQL/pgvector with a separate vector database.
- Treating Neo4j as a vector source of truth.
- Selecting cosine, inner product, or L2 independently inside individual migrations or
  query implementations.

## Implementation Constraints

Later migration work must install or verify these PostgreSQL extensions before schema
objects depend on them:

- `vector`
- `pg_trgm`
- `citext`

`vector` is required for pgvector types and indexes. `pg_trgm` is required by the PRD
for lexical/fuzzy retrieval support. `citext` is required because the planned schema
uses case-insensitive text fields such as `datasets.name`.

Future migrations must keep the authoritative embedding column compatible with the
configured architecture. `Settings.EXPECTED_EMBEDDING_DIMENSIONS` remains a project
default, not a runtime hard lock; schema migrations are responsible for enforcing the
actual database column shape.

Future ANN queries must use the same expression as the index:

```text
embedding::halfvec(3072) <=> query_embedding::halfvec(3072)
```

This expression and the `halfvec_cosine_ops` operator class must remain compatible so
PostgreSQL can use the HNSW expression index. Filtering by dataset and active
generation must remain part of retrieval semantics.

SM-208 must preserve that expression/operator compatibility when implementing the real
index and query plan.

## Verification and Source Notes

Local project verification:

- `sofias_memory/config.py` keeps `EXPECTED_EMBEDDING_DIMENSIONS = 3072`;
- `.env.example` keeps `EMBEDDING_DIMENSIONS=3072`;
- `compose.yaml` uses `pgvector/pgvector:0.8.1-pg17`;
- `uv.lock` resolves the Python adapter package `pgvector` to `0.5.0`;
- no migration, SQLAlchemy model, PostgreSQL engine, or database connection is created
  by this ADR.

Official source verification:

- OpenAI Embeddings FAQ:
  `https://help.openai.com/en/articles/6824809-embeddings-faq`
- pgvector v0.8.1 README:
  `https://github.com/pgvector/pgvector/blob/v0.8.1/README.md`
- pgvector v0.8.1 SQL extension definition:
  `https://raw.githubusercontent.com/pgvector/pgvector/v0.8.1/sql/vector.sql`

The pgvector SQL extension, not the Python adapter package, governs SQL behavior for
`vector`, `halfvec`, HNSW, casts, operator classes, and dimensional index limits.

## References

- `AGENTS.md`, sections 5, 14, and 22.
- `docs/product/Sofias_Memory_PRD_SPECS.md`, sections 10, 12.4, 17, and deployment
  dependencies.
- `docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B0_B2.md`, SM-201.
- ADR-0002, PostgreSQL Source of Truth and Neo4j Rebuildable Projection.
