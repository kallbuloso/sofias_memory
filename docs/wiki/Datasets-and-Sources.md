# Datasets & Sources

Datasets organize source-backed Knowledge Memory. Sources are the durable ingested inputs that belong to a Dataset.

Use this page when you need to understand namespaces, source lifecycle, statistics, administrative deletion, or the difference between clearing memory and retiring a Dataset itself.

For the broader ingestion/retrieval model, see [Knowledge Memory](Knowledge-Memory).

---

## 1. Dataset as a logical namespace

A Dataset is a first-class logical namespace for Knowledge Memory.

Typical uses include separating:

- documentation sets;
- customers or projects;
- research corpora;
- product knowledge;
- imported reference material;
- test and production-like corpora inside one Sofias Memory instance.

A Dataset is not an authorization boundary. Sofias Memory is currently single-user and protected by one static `X-API-Key`.

---

## 2. Dataset lifecycle

Dataset status is one of:

```text
active
deleting
deleted
```

`active` datasets can be used normally.

`deleting` means an administrative Dataset deletion has been accepted and its durable delete PipelineRun has not yet completed successfully.

`deleted` is a tombstoned namespace. Its identity remains reserved.

---

## 3. Create a Dataset

```text
POST /api/v1/datasets
```

Example:

```bash
curl -X POST "$SOFIAS_URL/api/v1/datasets" \
  -H "X-API-Key: $SOFIAS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Product Manuals",
    "description": "Reference documentation for supported devices",
    "slug": "product-manuals"
  }'
```

If `slug` is omitted, Sofias Memory derives one from the Dataset name.

The slug `main` is reserved.

Dataset names and slugs must be unique according to the public contract.

---

## 4. Dataset identity

A Dataset result exposes:

```text
dataset_id
name
slug
description
status
active_generation
created_at
updated_at
```

Use `dataset_id` as the stable structural identity.

`name` is human-readable and can change.

`slug` is the logical external identifier and does not change when a Dataset is renamed.

---

## 5. List and inspect Datasets

```text
GET /api/v1/datasets
GET /api/v1/datasets/{dataset_id}
```

The collection is paginated with:

```text
limit
offset
```

The current public maximum page size is 100.

---

## 6. Rename a Dataset

```text
PATCH /api/v1/datasets/{dataset_id}
```

Only the human-readable `name` changes.

The Dataset slug remains stable.

Example:

```json
{
  "name": "Updated Product Manuals"
}
```

A Dataset that is no longer active cannot be renamed.

---

## 7. What is a Source?

A Source is one durable ingestion input inside a Dataset.

Supported source kinds are:

```text
text
file
url
```

A Source exposes metadata such as:

```text
source_id
dataset_id
kind
name
mime_type
original_uri
content_sha256
normalized_sha256
byte_size
metadata
status
version
created_at
updated_at
```

The original bytes/text and the derived knowledge are related but distinct concerns. Source identity and metadata live in PostgreSQL; source-original storage is handled by the configured filesystem/S3 storage layer.

---

## 8. Source lifecycle

Source status is one of:

```text
pending
processing
active
failed
deleting
deleted
```

A typical successful ingestion moves through processing into `active`.

A `failed` Source or its associated PipelineRun should be investigated through the Runs API rather than repaired by manually editing database rows.

`deleting` and `deleted` represent destructive lifecycle states used by Forget/administrative workflows.

---

## 9. List Sources in a Dataset

```text
GET /api/v1/datasets/{dataset_id}/sources
```

This is a paginated operational view of the Sources belonging to one Dataset.

Example:

```bash
curl \
  -H "X-API-Key: $SOFIAS_KEY" \
  "$SOFIAS_URL/api/v1/datasets/$DATASET_ID/sources?limit=50&offset=0"
```

Use this endpoint when you need inventory and lifecycle metadata.

Use [Provenance & Feedback](Provenance-and-Feedback) when you need evidence lineage for one specific Source.

---

## 10. Dataset statistics

```text
GET /api/v1/datasets/{dataset_id}/stats
```

The statistics endpoint is PostgreSQL-backed and reports stable operational counters for:

```text
sources
  total
  active

documents
  total
  active

chunks
  total
  active

entities
  active_current_generation

relations
  active_current_generation

summaries
  total
```

These counters describe authoritative state. They are not reconstructed from Neo4j.

This endpoint is useful for dashboards, ingestion verification, maintenance checks, and post-Forget validation.

---

## 11. `active_generation`

Datasets expose an `active_generation` integer.

Knowledge derived from a Dataset is generation-aware. This lets Sofias Memory distinguish current authoritative derived state from prior generations during rebuild/reprocessing workflows.

For normal API consumers, the important rule is:

> Prefer the public Dataset statistics, Recall, Graph, and Provenance APIs instead of trying to interpret generation internals directly.

---

## 12. Clearing Dataset memory vs deleting the Dataset

These are intentionally different operations.

### Forget Dataset contents

The legacy/source-backed Forget API can clear memory associated with a Dataset while leaving the Dataset namespace available for future use.

### Administrative Dataset deletion

```text
DELETE /api/v1/datasets/{dataset_id}
```

This permanently retires the Dataset namespace.

Its name/slug are tombstoned and cannot simply be reused.

The `main` Dataset cannot be administratively deleted.

Do not treat these two operations as aliases.

---

## 13. Administrative delete is asynchronous

Dataset deletion creates a durable PipelineRun.

The response exposes:

```text
run_id
dataset_id
status
counters
```

Poll:

```text
GET /api/v1/runs/{run_id}
```

until the run reaches a terminal state.

When deletion succeeds, terminal counters can include:

```text
sources_deleted
documents_deactivated
chunks_deactivated
entities_deactivated
relations_deactivated
summaries_deactivated
storage_deleted
storage_already_absent
graph_events_processed
```

Read [Runs & Reliability](Runs-and-Reliability) for retry and cancellation semantics.

---

## 14. A Dataset stuck in `deleting`

A prior administrative delete may have failed after moving the Dataset to `deleting`.

Do not start a second destructive repair path manually.

Inspect the associated failed run and use:

```text
POST /api/v1/runs/{run_id}/retry
```

when the run is retryable.

The API deliberately returns a `DATASET_DELETING` conflict in cases where manual run retry is required.

---

## 15. `main`

`main` is a reserved Dataset identity used by the project’s compatibility behavior.

Important rules:

- the slug `main` cannot be created by callers as an ordinary Dataset;
- the `main` Dataset cannot be administratively deleted;
- callers should not build authorization logic around Dataset identity;
- Dataset behavior remains a knowledge namespace concern, not a Cognitive Memory scope.

Cognitive Memory uses its own independent scopes such as `global` and `project:<key>`.

---

## 16. Source storage

With the default filesystem backend, durable source-original state is backed by the configured persistent data directory/volume.

With `STORAGE_BACKEND=s3`, finalized Source original bytes live in the configured S3-compatible namespace, while PostgreSQL remains authoritative for metadata and derived state.

Operationally, protect:

- PostgreSQL;
- the persistent source data directory/volume;
- the configured S3 namespace when S3 mode is enabled.

Neo4j remains reconstructible.

See [Operations & Deployment](Operations-and-Deployment) for backup and restore guidance.

---

## 17. Recommended Dataset workflow

```text
Create/select Dataset
        ↓
Remember text/file/URL
        ↓
Observe PipelineRun
        ↓
Inspect Sources / Stats
        ↓
Cognify / Recall
        ↓
Use Provenance when evidence matters
        ↓
Forget content or administratively delete Dataset when explicitly required
```

---

## 18. Related pages

- [Knowledge Memory](Knowledge-Memory)
- [Runs & Reliability](Runs-and-Reliability)
- [Provenance & Feedback](Provenance-and-Feedback)
- [Troubleshooting](Troubleshooting)
- [Operations & Deployment](Operations-and-Deployment)
- [API Guide](API-Guide)

For exact request/response schemas, use the versioned [API semantics](https://github.com/kallbuloso/sofias_memory/blob/main/docs/api.md).