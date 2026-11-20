# Knowledge Memory

Knowledge Memory is Sofias Memory's source-backed memory plane.

Use it when durable memory originates from text, files, URLs, documents, reference material, or other content where source provenance matters.

Knowledge Memory is intentionally separate from [Cognitive Memory](Cognitive-Memory). A document should not be modeled as a personal fact, and a durable fact should not need to pretend to be a document.

---

## 1. When to use Knowledge Memory

Knowledge Memory is a good fit for:

- manuals and documentation;
- PDFs and text files;
- web pages fetched from HTTPS URLs;
- research corpora;
- product or company knowledge bases;
- policies and reference material;
- support documentation;
- technical archives;
- source-backed RAG.

The defining property is that the information came from some source material whose identity and provenance should remain explicit.

---

## 2. Core model

The conceptual flow is:

```text
Dataset
  └── Source
        └── Document
              └── Chunks
                    ├── embeddings
                    ├── entities
                    ├── relations
                    ├── summaries
                    └── provenance
```

PostgreSQL + pgvector is authoritative for the durable knowledge model.

Neo4j is a rebuildable projection for graph-oriented workloads.

---

## 3. Dataset

A `Dataset` is a logical namespace for related source-backed knowledge.

Typical examples:

```text
product-docs
customer-support
research-papers
internal-policies
```

Datasets provide an explicit boundary for ingestion, rebuild, recall, statistics, and administrative lifecycle operations.

A Dataset is not an authentication tenant.

---

## 4. Source

A `Source` represents the original ingested material.

Sofias Memory can ingest:

- raw text;
- uploaded files;
- a single HTTPS URL.

The original source object is durable.

Filesystem storage is the default. S3-compatible storage is also supported for finalized source originals.

Source identity remains important because derived chunks, entities, relations, summaries, and provenance must be traceable back to authoritative material.

---

## 5. Remember

Remember is the main ingestion entry point for Knowledge Memory.

The exact request shape depends on the input type, but conceptually Remember can operate in two styles:

```text
mode=ingest
```

Store the source durably without immediately performing the full semantic processing pipeline.

```text
mode=full
```

Store the source and process it through the full knowledge pipeline.

Knowledge writes use durable `PipelineRun`s so long-running work remains observable.

For exact request schemas and examples, use the versioned [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 6. Cognify

Cognify turns pending or explicitly selected source material into retrievable semantic knowledge.

Depending on the operation, this includes work such as:

- normalization;
- chunking;
- embeddings;
- entity extraction;
- relation extraction;
- summaries;
- graph projection.

Dataset rebuilds can create a new authoritative generation while preserving the project's generation semantics.

Cognify is part of the source-processing pipeline and therefore uses durable runs.

---

## 7. Chunks

Chunks are retrievable semantic units derived from processed documents.

They carry embeddings and participate in vector and hybrid retrieval.

Chunks retain their relationship to the source/document generation that produced them, which lets Sofias Memory return provenance instead of detached text fragments with no origin.

---

## 8. Entities and relations

Knowledge processing can extract entities and relations from source-backed material.

PostgreSQL remains authoritative for that knowledge.

Neo4j receives a reconstructible graph projection through the durable graph outbox.

This authority model matters:

> If PostgreSQL and Neo4j ever disagree, PostgreSQL wins.

Neo4j can be rebuilt from PostgreSQL.

---

## 9. Knowledge Recall

The Knowledge Memory recall endpoint is:

```text
POST /api/v1/recall
```

It is separate from Cognitive Recall:

```text
POST /api/v1/memories/recall
```

Knowledge Recall is designed for source-backed retrieval and can use retrieval modes such as:

- vector;
- lexical;
- summaries;
- graph traversal;
- hybrid rank fusion;
- graph-grounded RAG.

The exact available request fields and response shapes are documented in the versioned [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).

---

## 10. Vector retrieval

Vector retrieval uses pgvector embeddings stored in PostgreSQL.

It is useful when the query and relevant source text are semantically similar even if they do not share identical words.

Vector search is one retrieval strategy inside Knowledge Memory; it is not the entire memory model.

---

## 11. Lexical retrieval

Lexical retrieval focuses on textual/term overlap.

It complements semantic vector retrieval when exact vocabulary, identifiers, product names, or rare terms matter.

Knowledge Recall can combine multiple retrieval strategies rather than forcing all queries through one ranking mechanism.

---

## 12. Hybrid retrieval

Hybrid modes combine signals from different retrieval paths.

Sofias Memory can use rank fusion to combine complementary retrieval strategies while keeping the source-backed knowledge model and provenance intact.

This is useful when neither pure lexical nor pure vector retrieval is sufficient by itself.

---

## 13. Graph retrieval

Graph-oriented retrieval uses the reconstructible Neo4j projection.

It is useful when context depends on relationships between entities rather than only similarity between text chunks.

The graph is a projection, not a second authority.

Graph reads must therefore remain grounded in the authoritative PostgreSQL-backed knowledge lifecycle.

---

## 14. Graph-grounded RAG

Knowledge Recall can produce graph-grounded, LLM-generated answers with references back to the supporting source evidence.

This combines:

```text
retrieval
+ graph context
+ generation
+ provenance
```

The goal is not merely to generate an answer, but to retain enough evidence for the caller to understand what source material supported it.

---

## 15. Provenance

Provenance is central to Knowledge Memory.

A returned chunk, relation, or generated answer should not become detached from the original source material that supports it.

Sofias Memory preserves the chain from derived knowledge back toward the authoritative source/document evidence.

This is one of the key differences between source-backed Knowledge Memory and free-floating Cognitive Memory.

---

## 16. Improve

Improve is the knowledge-maintenance family.

It can perform work such as:

- feedback-weighted ranking hygiene;
- entity deduplication;
- relation embeddings;
- summaries;
- graph reconciliation.

Improve is explicit background work. It is not a hidden autonomous process that silently rewrites user knowledge without an operation boundary.

---

## 17. Knowledge Forget

The source-backed Forget endpoint is:

```text
POST /api/v1/forget
```

It operates on Knowledge Memory scopes such as source or dataset according to the API contract.

This is **not** the same operation as Cognitive Forget:

```text
POST /api/v1/memories/{memory_id}/forget
```

Knowledge Forget removes derived/source-backed memory according to its own lifecycle and provenance rules.

Cognitive Forget destructively forgets one exact `MemoryItem`.

Do not treat the two APIs as aliases.

---

## 18. Dataset delete vs. Forget

Administrative dataset deletion and memory Forget are intentionally distinct concepts.

A Dataset is a managed namespace with its own lifecycle.

Forget is a memory-erasure operation.

Keeping those semantics separate avoids turning every destructive action into one ambiguous "delete" endpoint.

---

## 19. PipelineRun

Knowledge Memory mutations use durable `PipelineRun`s.

A run makes asynchronous work observable and allows callers to inspect, retry, or cancel supported operations.

This is appropriate for source processing because ingestion, embeddings, extraction, graph projection, reconciliation, and storage effects can take meaningful time.

Cognitive Memory mutations are intentionally different: Create, Supersede, and Forget are synchronous and do not create PipelineRuns.

---

## 20. Storage

Original source objects are durable.

Supported storage modes include:

```text
filesystem
S3-compatible object storage
```

PostgreSQL remains authoritative for source metadata and application state.

When S3-compatible storage is enabled, the configured object namespace becomes part of the operator's durability responsibility alongside PostgreSQL and the required local source staging volume.

For operational details, use the [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).

---

## 21. Neo4j projection

Neo4j is used only for the knowledge-graph projection.

It is deliberately reconstructible.

The practical consequence is important for operations:

- PostgreSQL must be backed up;
- source originals must be protected;
- Neo4j can be rebuilt from PostgreSQL when needed.

The repository includes a graph rebuild script and documented recovery procedures.

---

## 22. Sessions and Knowledge Memory

Sessions are a separate temporal-context primitive.

Remember and Recall can associate work with Sessions and may use bounded Session Context where supported.

A Session is not itself a Dataset, Source, or permanent Cognitive Memory.

This allows an integration to combine source-backed knowledge with temporal interaction context without collapsing the two models.

---

## 23. Knowledge Memory vs. Cognitive Memory

| Question | Knowledge Memory | Cognitive Memory |
|---|---|---|
| Does this come from source material? | Usually yes | Not required |
| Dataset required? | Yes for source-backed knowledge | No |
| Source/Document/Chunk model? | Yes | No |
| Main retrieval | knowledge recall modes | exact cosine typed recall |
| Neo4j projection | Yes, reconstructible | No |
| Durable facts/preferences directly? | Not the intended model | Yes |
| Historical current-truth lifecycle | Not the Cognitive model | Yes |
| Precise forget by `memory_id` | No | Yes |

---

## 24. Typical integration flow

A common Knowledge Memory integration looks like:

```text
1. Create/select Dataset
2. Remember source material
3. Observe PipelineRun
4. Cognify/process as needed
5. Recall source-backed context
6. Inspect provenance
7. Improve/reconcile explicitly when useful
8. Forget/delete through the correct lifecycle operation when required
```

The caller remains responsible for deciding when each operation makes sense in its own product workflow.

---

## Go deeper

- [Core Concepts](Core-Concepts)
- [Cognitive Memory](Cognitive-Memory)
- [Integration Guide](Integration-Guide)
- [API Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md)
- [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
- [Architecture Decision Records](https://github.com/kallbuloso/sofias_memory/tree/main/docs/adr)

This Wiki page is explanatory. The versioned API and architecture documents in the repository remain authoritative.