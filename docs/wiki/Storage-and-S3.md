# Storage & S3

Sofias Memory stores source originals through a storage abstraction with two first-party backends:

- local filesystem;
- S3-compatible object storage.

The storage model is intentionally conservative: PostgreSQL remains authoritative for metadata and identity, while `storage_uri` records where each source original actually lives.

---

## The key rule

`STORAGE_BACKEND` answers only this question:

> Where should **new finalized source originals** be written now?

It does **not** decide where an existing Source is read from or deleted from.

Existing Sources carry their own persisted `storage_uri`, and read/delete/verify operations follow that URI's scheme:

```text
file://...  → filesystem adapter
s3://...    → S3 adapter
```

This makes mixed filesystem/S3 states safe during migration and rollback scenarios.

---

## Filesystem mode

Default:

```text
STORAGE_BACKEND=filesystem
DATA_DIRECTORY=/data/sources
TEMP_DIRECTORY=/data/tmp
```

New finalized Source originals are written under `DATA_DIRECTORY`.

`DATA_DIRECTORY` is durable application state and must be persisted across container recreation.

Do not treat it as disposable just because a container can be rebuilt.

---

## S3 mode

To write new finalized Source originals to S3-compatible object storage:

```text
STORAGE_BACKEND=s3
STORAGE_S3_BUCKET=my-bucket
```

Optional settings include:

```text
STORAGE_S3_PREFIX=sofias-memory
STORAGE_S3_REGION=us-east-1
STORAGE_S3_ENDPOINT_URL=https://s3-compatible.example
STORAGE_S3_ACCESS_KEY_ID=...
STORAGE_S3_SECRET_ACCESS_KEY=...
STORAGE_S3_SESSION_TOKEN=...
STORAGE_S3_MAX_CONCURRENCY=4
```

For standard AWS S3, `STORAGE_S3_ENDPOINT_URL` should normally remain unset.

For MinIO or another S3-compatible provider, set the provider's absolute HTTP(S) endpoint.

---

## Credentials

Static credentials are optional even in S3 mode.

When they are omitted, the standard AWS credential provider chain can be used, including environment credentials, shared configuration, container credentials, instance roles, and other provider-supported mechanisms.

For production, temporary role credentials are generally preferable to long-lived static keys.

---

## `DATA_DIRECTORY` is still required with S3

This is important.

Even with:

```text
STORAGE_BACKEND=s3
```

`DATA_DIRECTORY` remains durable state because it can contain:

- Remember ingress state;
- crash-recovery inputs;
- in-transit filesystem→S3 convergence state.

Do not remove its persistent volume merely because finalized objects live in S3.

---

## Filesystem → S3 convergence

When S3 becomes the active write backend, existing filesystem-backed source originals can be converged to S3 automatically during startup according to the accepted storage lifecycle.

The migration is designed around persisted Source identity and integrity checks rather than blind file copying.

During storage convergence, the application can remain live while business traffic is gated until the process is safe to admit normal operations.

Use:

```text
GET /health/live
GET /health/ready
```

to distinguish process existence from operational readiness.

---

## No automatic reverse migration

Switching:

```text
STORAGE_BACKEND=s3
```

back to:

```text
STORAGE_BACKEND=filesystem
```

does **not** automatically copy existing `s3://` Sources back to the filesystem.

Those Sources remain S3-backed and continue to be read/deleted through their persisted S3 URIs.

The backend selector controls new writes; it is not a global rewrite of historical object locations.

---

## S3 prefix ownership

`STORAGE_S3_PREFIX` is an application-managed namespace.

Sofias Memory must be the **exclusive writer** inside its configured prefix.

It is safe to share a bucket with other applications when each owns a different prefix. It is not safe to let multiple systems mutate the same Sofias Memory prefix independently.

Example:

```text
bucket: company-platform-data

sofias-memory/     ← Sofias Memory owns this prefix
other-service/     ← another system owns this one
```

The configured prefix is normalized and rejects ambiguous segments such as:

```text
.
..
empty path segments
backslash segments
```

---

## Deterministic object identity

Source object locations are deterministic from the relevant Dataset/Source identity and file extension.

This supports:

- idempotent recovery;
- integrity verification;
- convergence;
- precise deletion;
- reproducible object lookup.

Application code should not build S3 keys itself. The storage adapter/router owns that contract.

---

## Integrity verification

Reads are not treated as arbitrary object downloads.

The storage boundary can verify expected properties such as:

- Source identity;
- byte size;
- content SHA-256;
- maximum allowed bytes.

This protects the processing pipeline from silently consuming a different object than PostgreSQL says belongs to that Source.

---

## S3 startup probe

In S3 mode, startup can exercise real object-store capability through a probe that verifies the configured backend can perform its required operations.

A deployment should not be considered operational merely because credentials parsed successfully; actual storage capability matters.

If storage is unavailable during bootstrap, readiness should remain false rather than admitting work that cannot be persisted safely.

---

## Concurrency

`STORAGE_S3_MAX_CONCURRENCY` bounds concurrent object-store operations.

The same storage boundary is used by ordinary traffic and maintenance/convergence work so object-store activity remains bounded instead of allowing migrations and normal requests to independently saturate the provider.

Default:

```text
STORAGE_S3_MAX_CONCURRENCY=4
```

Tune only after observing real workload/provider behavior.

---

## Source lifecycle and deletion

Knowledge Memory deletion is storage-aware.

A Source Forget or administrative Dataset deletion can remove the original object through the adapter that corresponds to the Source's persisted URI.

That means a mixed dataset can legitimately contain:

```text
Source A → file://...
Source B → s3://...
```

and each object is deleted through the correct backend.

Do not delete objects manually and assume PostgreSQL will automatically reconcile itself afterward.

---

## Backup strategy

A complete Knowledge Memory backup must consider both:

1. PostgreSQL authoritative metadata/content state;
2. source-original storage.

For filesystem mode, back up the persistent `DATA_DIRECTORY` volume together with PostgreSQL.

For S3 mode, use your object-storage provider's backup/versioning/replication strategy together with PostgreSQL backups.

Neo4j is different: it is a rebuildable projection and does not carry the same backup authority.

---

## Restore strategy

Restore PostgreSQL and source storage as a consistent set.

A restored PostgreSQL database that references missing `file://` or `s3://` objects is not a complete source restore.

After restore:

1. verify PostgreSQL is current;
2. verify source objects are reachable;
3. check `/health/ready`;
4. inspect affected Sources/provenance;
5. only then resume write traffic.

---

## Filesystem deployment checklist

- [ ] `DATA_DIRECTORY` is on persistent storage;
- [ ] `TEMP_DIRECTORY` has sufficient working space;
- [ ] container UID/GID can read/write the volume;
- [ ] backup covers the source volume;
- [ ] storage paths are not mounted read-only.

---

## S3 deployment checklist

- [ ] bucket exists;
- [ ] prefix is dedicated to Sofias Memory;
- [ ] credentials/role are least-privilege;
- [ ] endpoint/region are correct;
- [ ] `DATA_DIRECTORY` is still persistent;
- [ ] network/DNS access to S3 works;
- [ ] object-store probe succeeds;
- [ ] backup/versioning policy is understood;
- [ ] lifecycle rules do not unexpectedly delete live objects.

---

## Troubleshooting S3

If S3-backed operations fail:

1. check `/health/ready`;
2. inspect application logs using the request/run correlation IDs;
3. verify bucket/prefix/region;
4. verify endpoint URL for S3-compatible providers;
5. verify role/static credential permissions;
6. confirm outbound DNS/TLS connectivity;
7. confirm the Source's persisted `storage_uri` scheme;
8. do not switch backends blindly as a repair mechanism.

Remember: changing `STORAGE_BACKEND` does not relocate already-finalized `s3://` Sources.

---

## Related documentation

- [Configuration Reference](Configuration-Reference)
- [Datasets & Sources](Datasets-and-Sources)
- [Operations & Deployment](Operations-and-Deployment)
- [Troubleshooting](Troubleshooting)
- [Security Model](Security-Model)
- [Canonical Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md)
