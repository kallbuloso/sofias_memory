# Operations Guide

This is the canonical operational contract for Sofias Memory: first
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

Sofias Memory does **not** promise zero-downtime upgrades. The model above requires
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

The supported backup model is a **maintenance-window, quiesced backup**
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
  sofias-memory:0.1.2 uv run --no-sync python scripts/rebuild_graph.py \
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

## 12. Production deployment

This section closes the gap between "the image exists" and "an operator can
actually deploy it and prove it works" (REL-006). It does not introduce a new
architecture: `compose.yaml` is still the only canonical stack definition,
and Portainer/EasyPanel both consume it directly — neither platform gets its
own Compose file.

### A. Deployment paths

There are exactly two supported ways to get the application running, and
both end at the same `compose.yaml`:

1. **Source build** (§2 above): clone the repository, `docker compose build
   sofias-memory` (or let `docker compose up` build it implicitly), and
   proceed with the rest of §2.
2. **Published GHCR image** (§C below): point `compose.yaml`'s
   `sofias-memory.image` at a published `ghcr.io/kallbuloso/sofias-memory`
   tag or digest instead of building locally, and skip the build step.

Both paths use the identical migration, readiness, security, and smoke
procedure — only how the image gets onto the host differs.

### B. First production start

Follow §2 (First start) exactly, substituting the target image (built or
pulled per §A) for `sofias-memory:0.1.2`. Do not skip the migration step
(§3) or the readiness check — a deployment is not "up" until
`/health/ready` reports `ready` and a production smoke run (§H) has passed.

### C. Published GHCR image

Stable releases (REL-005's release workflow) are published to GHCR at the
exact version tag. The current stable image is available at:

```text
ghcr.io/kallbuloso/sofias-memory:0.1.1
```

**For a release candidate** (used only to validate this very procedure, never
recommended to end users as a production target), the equivalent is:

```text
ghcr.io/kallbuloso/sofias-memory:0.1.0-rc.1
```

Pulling requires no authentication — GHCR packages for this repository are
public; anonymous `docker pull` works.

**Note on `0.1.0-rc.1` specifically:** it was built from commit `607fb8d`,
before `scripts/production_smoke.py` existed (REL-006) — that image does
**not** contain the script. This does not affect §H: the production smoke
run there is always executed from a source checkout on the *host*, as a
standalone HTTP client against the deployed API; it never needs to run
*inside* the target container, so it works identically regardless of what
that image's `/app/scripts` contains. Any image built from the REL-006
commit onward (including the published `v0.1.0` and every later release)
contains `production_smoke.py` automatically, since `Dockerfile` already
does `COPY scripts ./scripts`.

To run the published image via the canonical `compose.yaml` without a local
build, override the `image` value for the `sofias-memory` service at deploy
time (Portainer/EasyPanel both expose a way to do this through their UI, and
a source checkout can do it with a local, untracked override file passed via
`-f`, or a one-line temporary edit of a copy of `compose.yaml` that is never
committed) — the point is that `compose.yaml` itself stays platform-neutral
and build-capable; deployments that want the published image supply the tag
externally rather than the repository maintaining a second Compose file.

### D. Version/digest pinning

Production deployments must reference an exact, immutable identity — never a
floating tag:

- Prefer an exact version tag: `ghcr.io/kallbuloso/sofias-memory:0.1.1`.
- For maximum reproducibility (e.g. verifying exactly what was validated
  before a rollout), pin by digest instead:
  `ghcr.io/kallbuloso/sofias-memory@sha256:...`.
- **Never use `latest`.** No `latest` tag is published for this image (see
  the release workflow) precisely so this mistake is not available.

Rollback (§5) must use the same discipline: roll back to the exact previous
version or digest, never to an unpinned reference, and remember that
application rollback and schema rollback are two different operations.

### E. Production security checklist

- **TLS / reverse proxy**: putting TLS in front of the application is the
  operator's responsibility — this project ships no TLS termination of its
  own. Use Traefik, Nginx, Caddy, or the hosting platform's built-in layer
  (Portainer and EasyPanel both expect this to come from outside the stack).
  Never expose the application directly to the internet without TLS.
- **Firewall / network**: only the application's HTTP port should be
  reachable externally. PostgreSQL and Neo4j must stay on the internal
  Compose network — `compose.yaml` does not publish ports for either by
  default; do not add `ports:` entries for them in a production deployment.
- **Secrets**:
  - Generate `API_KEY` with `scripts/generate_api_key.py` (never hand-type
    one).
  - Use a strong, unique `DB_PASSWORD` and `DB_NEO4J_PASSWORD`.
  - Treat the LLM/embedding provider key as a secret like any other.
  - Never commit `.env` or any file containing a real secret value.
- **Rotation**: rotating `API_KEY` or a provider key is changing the
  environment variable and restarting the application — Settings are loaded
  once at startup (§10 of `AGENTS.md`); there is no runtime key-management
  endpoint, and none is planned. A rotation is not complete until the
  application has been restarted with the new value.
- **Volumes**: grant the smallest access necessary to whatever manages the
  three named volumes (`sofias_memory_postgres_data`, `sofias_memory_neo4j_data`,
  `sofias_memory_sources`). Back up PostgreSQL and the sources volume per §6;
  Neo4j is reconstructible and not required to be backed up.
- This project does not provide a secrets manager, external observability
  agent, or automated certificate management — do not assume one exists
  where it hasn't been explicitly added.

### F. Portainer

Portainer consumes `compose.yaml` directly as a Stack — there is no
`compose.portainer.yaml` and none should be added.

1. Create a new Stack from the repository (Git-based deploy) or by pasting
   `compose.yaml`'s contents.
2. Provide the required environment variables through the Stack's
   environment variable UI — see the minimum list below; audit `.env.example`
   for the full set before relying on defaults for anything you care about.
3. Deploy. Portainer preserves the three named volumes declared in
   `compose.yaml` across redeploys of the same Stack; confirm this in the
   Volumes view before trusting it with real data.
4. The container healthcheck already defined in `compose.yaml` (and in the
   image itself) shows up natively in Portainer's container view — no
   additional Portainer-specific healthcheck configuration is needed.
5. Portainer does not provide TLS termination or a public domain by itself;
   put a reverse proxy in front of it per §E, or use a proxy already managed
   by your Portainer environment.
6. Run the migration step (§B/§2 step 3) as a one-off `docker compose run`
   equivalent — Portainer's "Execute command" / one-off container feature
   against the `sofias-memory` service image — before starting the
   long-running service for the first time or after any upgrade.
7. Upgrade by changing the pinned image tag/digest (§D) to the new exact
   version and redeploying the Stack, or by triggering a rebuild if using the
   source-build path; always run the migration and a production smoke (§H)
   immediately after.

Minimum required environment variables (already enforced at the
`compose.yaml` level via `:?...` interpolation — the stack will not start
without them):

```text
API_KEY
LLM_API_KEY
DB_PASSWORD
DB_NEO4J_PASSWORD
```

Audit `.env.example` for the rest before assuming any other variable's
default is appropriate for your deployment.

### G. EasyPanel

EasyPanel also consumes `compose.yaml` directly — there is no
`compose.easypanel.yaml` and none should be added.

1. Create the app/stack from this repository's `compose.yaml` (EasyPanel's
   Compose/Docker Compose service type).
2. Set the same required environment variables as the Portainer list above
   through EasyPanel's UI; audit `.env.example` for anything else you need.
3. Configure persistent volumes for the three named volumes exactly as
   declared in `compose.yaml` — do not let EasyPanel substitute ephemeral
   storage for PostgreSQL, Neo4j, or the sources volume.
4. EasyPanel's own domain/reverse-proxy/TLS layer satisfies §E's TLS
   requirement; use it (or an external proxy) rather than exposing the
   application's port directly.
5. Run the migration manually (§B/§2 step 3) via EasyPanel's one-off
   command/console feature before the first start and after every upgrade —
   this project does not migrate automatically at startup, on this platform
   or any other.
6. Upgrade by pointing at a new exact image tag/digest (§D) or triggering a
   rebuild, then migrate and run a production smoke (§H) before considering
   the upgrade complete.

Nothing about EasyPanel's behavior is documented here beyond what was
verified necessary for this deployment (image/env/volume/migration/smoke) —
this is deliberately not a general EasyPanel manual.

### H. Production smoke

`scripts/production_smoke.py` proves a **deployed** instance is functional —
it never starts Compose, runs migrations, or touches Docker itself; it only
speaks HTTP to an already-running API. It is run from a source checkout on
the operator's own machine (or CI runner), as a standalone HTTP client — it
does not need to exist inside the target container, and works the same way
whether the deployed image contains a copy of it or not. Run it after every
first start and every upgrade, against every deployment path (source-build,
Portainer, EasyPanel, or a raw `compose.yaml` with the GHCR image
substituted).

```bash
SOFIAS_MEMORY_API_KEY=sf-... uv run python scripts/production_smoke.py \
  --base-url https://your-deployment.example
```

(`API_KEY` is accepted as a fallback environment variable name for
consistency with the rest of the project's configuration. The key is never
accepted as a CLI argument, so it never appears in a process listing, and the
script never prints it.)

The flow: `/health/live` → `/health/ready` → `GET /info` → create an isolated
`production-smoke-<uuid>` dataset → `POST /remember` (`mode=full`,
`wait=false`, exercising the real provider-backed pipeline) → poll the run to
`succeeded` → `POST /recall` (`mode=chunks`) to confirm the just-remembered
content is retrievable with correct provenance → delete the smoke dataset
(awaiting its own async completion if the API returns `202`) in a `finally`
block that runs even if an earlier stage failed. It exits `0` only when the
whole flow (including cleanup) succeeded, and prints one `[PASS]`/`[FAIL]`
line per stage plus a final `PRODUCTION SMOKE PASS`/nonzero exit. It never
deletes anything but the dataset it just created, and it refuses (via an
explicit guard checked immediately before the delete call) to ever touch the
`main` dataset or a dataset not created by that same run.

### I. Post-deploy verification

After any first start or upgrade, before considering the deployment done:

1. `/health/live` returns `200`.
2. `/health/ready` returns `200` with all checks (`postgres`, `neo4j`,
   `worker`) `ready`.
3. `GET /api/v1/info` reports the version you expect to have deployed.
4. `scripts/production_smoke.py` (§H) exits `0`.
5. For an upgrade specifically: `alembic current` matches the target
   version's expected head (§3), and the release notes for that version have
   been read.

If any of these fail, treat the deployment as not done — do not route
production traffic to it — and consult §10 (Failure / abort conditions) for
what to check before retrying.
