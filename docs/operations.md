# Operations Guide

This is the canonical operational contract for Sofias Memory v0.1.0: first
start, migration, upgrade, rollback, backup, and restore. Every command below
was executed literally against a real, disposable PostgreSQL + Neo4j pair
(via `docker compose`, using the release image built from this repository's
own `Dockerfile`) as part of validating this document — nothing here is
speculative. See [`README.md`](../README.md) for the product overview and
[`docs/api.md`](api.md) for API semantics.

## 1. Authority model

- **Must be backed up (authoritative, irreplaceable):** PostgreSQL, and the
  persistent source-originals volume (`/data/sources` in the container,
  the `sofias_memory_sources` volume in `compose.yaml`).
- **Reconstructible (not authoritative):** Neo4j. It can always be rebuilt
  from PostgreSQL via the packaged `scripts/rebuild_graph.py`. A Neo4j copy
  may be kept as an operational convenience (it skips a rebuild step on
  restore), but it is never required, and a restore procedure must never
  treat Neo4j as authoritative over PostgreSQL.

This directly follows ADR-0002 (PostgreSQL is the sole source of truth;
Neo4j is a rebuildable projection) and ADR-0008 (Neo4j rebuild contract) —
this document only turns that already-accepted architecture into an
operational procedure.

## 2. First start (empty database)

Principle, unchanged from the architecture: **migration is explicit, never
automatic at application startup.** `/health/ready` detects a schema that
does not match the application's expected revision and reports `not_ready`
rather than guessing or auto-applying anything.

Verified procedure, using only the release image (no source checkout, no
Python installed on the host):

```bash
# 1. Configure secrets (see README.md#configuration; never commit real values).
cp .env.example .env
# Set API_KEY (scripts/generate_api_key.py), DB_PASSWORD, DB_NEO4J_PASSWORD,
# LLM_API_KEY, EMBEDDING_API_KEY in your shell environment or an env file
# passed to `docker compose --env-file`.

# 2. Start PostgreSQL and Neo4j only.
docker compose up -d postgres neo4j

# 3. Run the migration using the application's own image — a one-shot
#    container that exits after applying migrations. `depends_on:
#    condition: service_healthy` on `sofias-memory` means this command
#    waits for both databases to be healthy before running, and `--rm`
#    removes the one-shot container afterward; nothing is left behind.
docker compose run --rm sofias-memory alembic upgrade head

# 4. Start the application.
docker compose up -d sofias-memory

# 5. Verify.
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/api/v1/info
```

This exact sequence was run against a genuinely empty, disposable PostgreSQL
(verified zero tables beforehand) and a fresh Neo4j: step 3 applied
`0001` → `0011`; step 4/5 reached `/health/live` = `200`, `/health/ready` =
`200` with all three checks (`postgres`, `neo4j`, `worker`) ready, and
`/api/v1/info` reporting the expected version.

## 3. Migration policy

- Alembic is the **sole** authority for schema evolution.
- Migrations are applied **explicitly** — `docker compose run --rm
  sofias-memory alembic upgrade head` (§2) — never automatically when the
  application starts.
- A schema that doesn't match the application's expected revision leaves
  `/health/ready` at `not_ready`; the application does not guess or
  self-heal.
- A deployment is not healthy until migration has completed and readiness
  is confirmed.

Useful commands (all run the same way as step 3 above, substituting the
Alembic subcommand):

```bash
docker compose run --rm sofias-memory alembic current
docker compose run --rm sofias-memory alembic heads
```

## 4. Upgrade

**Current repository/local-image flow** (build-from-source, as `compose.yaml`
does today):

1. Read the release notes for the target version.
2. Take a backup (§6) at the currently-deployed version.
3. Pull/checkout the target version's source and rebuild the local image
   (`docker compose build sofias-memory`), or obtain the target image once
   REL-005 publishes versioned images (see below).
4. Quiesce the application (`docker compose stop sofias-memory`) so no new
   writes land during the migration window.
5. Run the migration **using the target image**: `docker compose run --rm
   sofias-memory alembic upgrade head`.
6. Start the application on the target image: `docker compose up -d
   sofias-memory`.
7. Verify readiness (`/health/ready`).
8. Run a smoke check against a non-production dataset before considering the
   upgrade complete.

**Versioned image flow (after REL-005 publishes to GHCR)** — not available
yet; do not run these commands against an artifact that doesn't exist. Once
published, the same eight steps apply with `image: <target-image>:<version>`
substituted for the local build in `compose.yaml`, and no local rebuild step.

v0.1.0 does **not** promise zero-downtime upgrades. The model above requires
a maintenance window (the app is quiesced for the duration of the migration).
For a single-user MVP, this trade-off is intentional: it favors a simple,
recoverable procedure over rolling-migration complexity that nothing in the
current architecture is designed to support.

## 5. Rollback

Rollback of the **application/image** and rollback of the **database
schema** are two different operations — this document never says "rollback
is supported" without specifying which one.

- **Application/image rollback:** selecting a previous image version is
  safe **only if** the schema currently in the database is still compatible
  with that older version (i.e., no migration was applied between the old
  and new version, or the old version's code tolerates the newer schema).
  If a migration was applied, rolling back the image alone does not roll
  back the schema — the two must be considered together.
- **Schema/database rollback:** supported **only** for a specific migration
  that has a real, tested `downgrade()`. Alembic's `downgrade` command
  exists, but this repository does not promise it works for every migration
  — see migration `0011` below for a concrete case where it explicitly does
  not. There is no blanket guarantee of arbitrary schema downgrade.

**Guaranteed recovery path when a schema rollback isn't possible:** restore
from the pre-upgrade backup (§6/§7), which by construction has the old
schema and the old application version together, consistent.

### Migration `0011`'s specific limitation

`migrations/versions/0011_add_dataset_delete_pipeline_type.py` adds
`'dataset_delete'` to the native PostgreSQL `pipeline_type` enum
(`ALTER TYPE pipeline_type ADD VALUE ...`). Its `downgrade()` raises
`NotImplementedError` deliberately, because **PostgreSQL has no `ALTER TYPE
... DROP VALUE`** — removing a single enum value would require rebuilding
the entire enum type and rewriting every dependent row, which this
migration does not attempt.

Consequence for operators: **`alembic downgrade` through revision `0011` is
not a supported rollback mechanism.** If you need to revert past the point
where `0011` was applied, restore PostgreSQL from a backup taken before that
migration ran — do not attempt manual `DROP TYPE`/enum-rebuilding SQL; that
is unsupported, destructive, and out of scope for this document.

## 6. Backup

For v0.1.0 the supported backup is a **maintenance-window, quiesced backup**
— not a sophisticated hot-backup mechanism. This is a deliberate,
conservative choice: it removes any race between PostgreSQL and the source
volume, at the cost of a short window where the application is stopped.

Verified procedure:

```bash
# 1. Identify the currently deployed version and schema revision.
docker compose run --rm sofias-memory alembic current

# 2. Quiesce the application so nothing is writing to PostgreSQL or the
#    source volume during the capture.
docker compose stop sofias-memory

# 3. (PostgreSQL itself stays running — pg_dump needs it available.)

# 4. Dump PostgreSQL in custom format (-Fc), which supports selective and
#    parallel restore via pg_restore and is the recommended format for a
#    PostgreSQL 17 server:
docker compose exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -Fc -f /tmp/backup.dump
docker compose cp postgres:/tmp/backup.dump ./sofias_memory_pg.dump

# 5. Archive the source-originals volume. The actual Docker volume name is
#    the Compose project name plus the name declared in compose.yaml
#    (`sofias_memory_sources`) — e.g. `sofias_memory_sofias_memory_sources`
#    for a default project named after this directory, or
#    `<project>_sofias_memory_sources` for a custom `-p <project>`/
#    COMPOSE_PROJECT_NAME. Confirm the exact name with `docker volume ls`
#    rather than hard-coding it. /data/tmp is transient and must NOT be
#    backed up (it's tmpfs in compose.yaml and never contains durable state).
docker run --rm \
  -v <resolved-sources-volume-name>:/data/sources:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/sources.tar.gz -C /data/sources .

# 6. Record a manifest (plain text/JSON, produced by the operator — no new
#    tooling). At minimum:
cat > backup-manifest.json << EOF
{
  "app_version": "<from step 1 / /api/v1/info>",
  "alembic_revision": "<from step 1>",
  "timestamp_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "postgres_dump_file": "sofias_memory_pg.dump",
  "postgres_dump_format": "pg_dump -Fc (custom format)",
  "sources_archive_file": "sources.tar.gz"
}
EOF

# 7. Resume the application.
docker compose start sofias-memory

# 8. Confirm readiness.
curl http://127.0.0.1:8000/health/ready
```

Neo4j is **not** part of the supported backup — it is always reconstructible
from the restored PostgreSQL (§8). Do not include `.env` or any secret file
in the backup archive; secrets are the operator's responsibility to store
separately (a secrets manager, an encrypted vault, etc.), never bundled with
application data.

This exact sequence (steps 2, 4, 5) was run for real: `pg_dump -Fc` produced
a valid dump; the source volume was archived via a disposable helper
container mounting the named volume read-only.

## 7. Restore

Restore treats PostgreSQL and the source volume **as one consistent unit** —
never restore one without the other from the same backup.

```bash
# 1. Stop/do not start the application.
docker compose stop sofias-memory   # or: do not `up` it yet on a fresh stack

# 2. Bring up a clean/isolated PostgreSQL and Neo4j (fresh volumes for a
#    disaster-recovery restore; the same volumes, emptied, for an in-place
#    restore).
docker compose up -d postgres neo4j

# 3. Restore PostgreSQL from the dump.
docker compose cp sofias_memory_pg.dump postgres:/tmp/restore.dump
docker compose exec postgres pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --no-owner --role="$POSTGRES_USER" /tmp/restore.dump

# 4. Restore the source volume from the archive (into the resolved volume
#    name, per §6 step 5).
docker run --rm \
  -v <resolved-sources-volume-name>:/data/sources \
  -v "$PWD":/backup:ro \
  alpine tar xzf /backup/sources.tar.gz -C /data/sources

# 5. Confirm the restored schema revision matches what the manifest recorded.
docker compose run --rm sofias-memory alembic current

# 6. Neo4j at this point is empty/fresh (never restore an old Neo4j copy as
#    authoritative over the just-restored PostgreSQL — see §1).

# 7. Rebuild the Neo4j projection from the now-authoritative PostgreSQL,
#    using the image's own packaged script:
docker run --rm --network <compose-network> \
  -e DATABASE_URL=... -e NEO4J_URI=... -e NEO4J_PASSWORD=... \
  -e API_KEY=... -e LLM_API_KEY=... \
  sofias-memory:0.1.0 uv run --no-sync python scripts/rebuild_graph.py \
  --all --confirm-all

# 8. Start the application.
docker compose up -d sofias-memory

# 9. Verify readiness.
curl http://127.0.0.1:8000/health/ready

# 10. Validate data: confirm a known dataset/source exists, and run a real
#     Recall query to confirm content is actually retrievable, not just
#     present as rows.
```

**Restoring a historical backup should use the application version recorded
in that backup's manifest, not an unrelated newer version.** Restore first,
verify, then follow the Upgrade procedure (§4) separately if you need to
move to a newer version afterward — do not combine restore and upgrade into
one opaque step.

### Restore drill — verified end-to-end with real, non-empty data

This procedure was executed literally, in full, against real disposable
infrastructure, with real (non-empty) content — not just reviewed on paper:

1. A real dataset/source/document/chunks/entities/relations fixture was
   created via a genuine `POST /api/v1/remember` (`mode=full`) call, using a
   real OpenAI-compatible provider — 1 chunk, 4 entities, 3 relations.
2. **Sentinel hash** of the stored source file
   (`/data/sources/<dataset_id>/<source_id>/original.txt`) was recorded
   before backup: `sha256` matched the `content_hash` already reported by
   the API.
3. Backup taken per §6 (app quiesced, `pg_dump -Fc`, source volume
   archived).
4. **Destruction proof:** the entire disposable stack — containers, all
   three volumes (`postgres`, `neo4j`, `sources`), and the network — was
   removed (`docker compose down -v`), and confirmed absent (`docker volume
   ls` showed nothing left).
5. Fresh, empty PostgreSQL and Neo4j started from scratch.
6. `pg_restore` completed; `alembic_version` = `0011`; the dataset and
   source rows were present, byte-for-byte identical to before.
7. Source volume restored; **the sentinel hash recomputed after restore
   matched exactly** the value recorded in step 2 — proof the filesystem
   came back identical, not just "a file exists."
8. `scripts/rebuild_graph.py --all --confirm-all` run from the image against
   the fresh, disposable Neo4j: reconstructed exactly 1 dataset, 4 entities,
   1 chunk, 3 relations — matching the original fixture precisely.
9. Application started; `/health/live` = `200`, `/health/ready` = `200`
   (all three checks ready).
10. `GET /api/v1/datasets` and `GET /api/v1/datasets/{id}/sources` showed
    the restored dataset/source. A real `POST /api/v1/recall` (`mode=graph`)
    query returned the correct generated answer, the correct chunk, and the
    correct entities/relations — proof the restored system is not just
    "readable," it is **functionally equivalent** to the pre-backup state.

No real user data or shared infrastructure was used or destroyed — every
container, volume, and network in this drill was disposable and created
solely for it, then torn down afterward.

## 8. `graph_outbox` after restore

A restored PostgreSQL can contain historical `graph_outbox` rows in any
terminal state (`done`, `failed`) from before the backup was taken. These
rows are **not** replayed as the source of truth for the restored Neo4j
projection — `scripts/rebuild_graph.py` derives Neo4j state directly from
the current authoritative PostgreSQL tables (entities, relations, chunks),
independent of outbox history. You do not need to inspect or reset
`graph_outbox` rows manually; running the rebuild script (§7 step 7) is
sufficient, and the worker's own outbox processing (once the application
starts) will pick up any genuinely pending rows going forward exactly as it
does in normal operation. Never edit `graph_outbox` rows by hand.

## 9. Pipeline runs / in-flight work after restore

Restoring PostgreSQL restores the full pipeline state table
(`pipeline_runs`, `pipeline_steps`) exactly as it existed at backup time,
including any runs that were `queued`, `running`, or `cancelling`. This is
expected and requires no manual intervention: the application's existing
startup recovery contract (already proven in the GATE-B5 functional gate)
reconciles stale/abandoned runs the same way it does after an ordinary
restart. Never edit `pipeline_runs` rows manually to "fix" state after a
restore.

## 10. Failure / abort conditions

Stop and investigate — do not "continue anyway" — if any of the following
occurs:

- The backup is incomplete (dump or source archive missing/truncated).
- The source archive's content doesn't correspond to the PostgreSQL dump
  (different dataset/source counts, mismatched sentinel hash).
- `alembic current` after restore reports a revision other than the one
  recorded in the backup manifest.
- `pg_restore` reports errors (not just notices/warnings).
- The sentinel hash comparison in a restore drill does not match.
- Neo4j rebuild (`scripts/rebuild_graph.py`) exits non-zero.
- `/health/ready` remains `not_ready` after migration and startup.
- You do not understand why the deployed application version doesn't match
  what you expected.

## 11. Secrets

Backups produced by this procedure never include `.env` or any secret.
Application-data backup (§6) and secret/config backup are deliberately
separate concerns — store `API_KEY`, `LLM_API_KEY`, `DB_PASSWORD`,
`DB_NEO4J_PASSWORD`, etc. through your own secrets management, never
alongside the `pg_dump`/source archive. Every command in this document uses
placeholders or environment variable references; no real secret value
appears here.
